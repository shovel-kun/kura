from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from croniter import croniter

from . import paths, state
from .archive import MEDIA_EXTENSIONS, append_archive_entries, archive_key, read_archive, remove_archive_entry
from .config import (
    DEFAULT_CONFIG_TEXT,
    DEFAULT_PROFILE_TEXT,
    NICONICO_PROFILE_TEXT,
    ConfigError,
    append_source,
    load_config,
    set_source_enabled,
)
from .locks import source_lock
from .runner import build_command, run_source


SYSTEMD_INSTRUCTIONS = """Systemd user timer example:

~/.config/systemd/user/kura.service
[Unit]
Description=kura archive check
[Service]
Type=oneshot
ExecStart=%h/.local/bin/kura run --due
Nice=10
IOSchedulingClass=idle

~/.config/systemd/user/kura.timer
[Unit]
Description=kura schedule tick
[Timer]
OnCalendar=*:0/15
Persistent=true
[Install]
WantedBy=timers.target

Enable it with:
systemctl --user enable --now kura.timer
loginctl enable-linger $USER

Unit files are also shipped in contrib/systemd/ inside the kura source tree.
"""


def init_command(_args) -> int:
    config_dir = paths.config_dir()
    profile_dir = paths.profiles_dir()
    for directory in (config_dir, profile_dir, paths.state_dir(), paths.locks_dir(), paths.logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = paths.config_file()
    if config_path.exists():
        print(f"exists: {config_path}")
    else:
        config_path.write_text(DEFAULT_CONFIG_TEXT)
        print(f"created: {config_path}")

    profile_path = paths.profile_file("av1-archive")
    if profile_path.exists():
        print(f"exists: {profile_path}")
    else:
        profile_path.write_text(DEFAULT_PROFILE_TEXT)
        print(f"created: {profile_path}")

    niconico_profile_path = paths.profile_file("niconico-archive", ext="nnconf")
    if niconico_profile_path.exists():
        print(f"exists: {niconico_profile_path}")
    else:
        niconico_profile_path.write_text(NICONICO_PROFILE_TEXT)
        print(f"created: {niconico_profile_path}")

    state.init_db()
    print(f"initialized: {paths.state_db()}")
    print()
    print(SYSTEMD_INSTRUCTIONS)
    return 0


def add_command(args) -> int:
    source = append_source(
        name=args.name,
        url=args.url,
        output_dir=Path(args.dir).expanduser(),
        profile=args.profile,
        schedule=args.schedule,
        strategy=args.strategy,
        cookies=args.cookies,
        downloader=getattr(args, "downloader", None),
        auth=getattr(args, "auth", None),
    )
    print(f"added: {source.name}")
    return 0


def list_command(_args) -> int:
    config = load_config()
    rows = [
        ("name", "enabled", "strategy", "schedule", "profile", "output_dir"),
        *[
            (
                source.name,
                "yes" if source.enabled else "no",
                source.update_strategy,
                source.schedule,
                source.profile,
                str(source.output_dir),
            )
            for source in config.sources
        ],
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths).rstrip())
    return 0


def import_command(args) -> int:
    """Adopt an existing yt-dlp archive directory into kura."""
    dry_run: bool = args.dry_run
    name: str = args.name

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
        return 1

    # Acquire lock to prevent concurrent run-vs-import on the same archive.
    with source_lock(name, paths.state_dir()) as acquired:
        if not acquired:
            print(f"skipped: {name} is already running; import aborted to avoid archive corruption")
            return 1
        return _import_locked(source, name, dry_run)


_NNDOWNLOAD_ID_RE = re.compile(r"^(?:sm|so|nm)\d+$")


def _nndownload_archive_key(path: Path) -> str | None:
    """Try to extract a niconico archive key from an nndownload metadata sidecar.

    Returns ``"niconico <id>"`` when *path* is a valid nndownload sidecar, or
    ``None`` when it is not (caller should silently ignore).

    Parse errors in generic ``.json`` files are silently swallowed — the file
    was never claimed to be nndownload evidence.  (Compare with ``.info.json``
    parse errors, which are warned about.)
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None  # silently skip; not our file

    if not isinstance(data, dict):
        return None

    video_id = data.get("id")
    if not isinstance(video_id, str) or not _NNDOWNLOAD_ID_RE.match(video_id):
        return None

    # Require at least one URL-ish field pointing at nicovideo.jp.
    for field in ("document_url", "url", "thumbnail_url"):
        val = data.get(field, "")
        if isinstance(val, str) and "nicovideo.jp" in val:
            return f"niconico {video_id}"

    return None


def _import_locked(source, name: str, dry_run: bool) -> int:
    archive_path = source.archive_file
    archive_set = read_archive(archive_path)

    # Scan output_dir recursively for .info.json files and nndownload sidecars.
    output_dir = source.output_dir
    info_json_paths: list[Path] = sorted(output_dir.rglob("*.info.json")) if output_dir.is_dir() else []

    # Collect candidate nndownload sidecar paths: *.json but NOT *.info.json
    # and NOT *.comments.json.
    nn_json_paths: list[Path] = []
    if output_dir.is_dir():
        for p in sorted(output_dir.rglob("*.json")):
            name_str = p.name
            if name_str.endswith(".info.json") or name_str.endswith(".comments.json"):
                continue
            nn_json_paths.append(p)

    scanned = 0
    already_in_archive = 0
    evidence_keys: set[str] = set()
    skipped_playlist = 0
    skipped_unparseable: list[Path] = []

    for info_path in info_json_paths:
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            skipped_unparseable.append(info_path)
            continue

        if not isinstance(info, dict):
            skipped_unparseable.append(info_path)
            continue

        # Skip playlist-level JSONs.
        top_type = info.get("_type", "")
        if top_type in ("playlist", "multi_video"):
            skipped_playlist += 1
            continue

        # Skip entries with no id/extractor evidence.
        if not info.get("id") and not info.get("extractor_key") and not info.get("ie_key"):
            skipped_playlist += 1  # treated as non-video / skipped
            continue

        key = archive_key(info)
        if key is None:
            skipped_playlist += 1
            continue

        scanned += 1
        evidence_keys.add(key)
        if key in archive_set:
            already_in_archive += 1

    # Process nndownload sidecar files.
    for nn_path in nn_json_paths:
        key = _nndownload_archive_key(nn_path)
        if key is None:
            continue  # not nndownload evidence — silently skip
        scanned += 1
        evidence_keys.add(key)
        if key in archive_set:
            already_in_archive += 1

    missing = sorted(evidence_keys - archive_set)

    # Detect unverifiable media files: a media file is unverifiable when it has
    # neither a sibling .info.json nor a sibling qualifying nndownload sidecar.
    unverifiable: list[Path] = []
    if output_dir.is_dir():
        for media_file in output_dir.rglob("*"):
            if not media_file.is_file():
                continue
            if media_file.suffix.lstrip(".").lower() not in MEDIA_EXTENSIONS:
                continue
            stem = media_file.stem
            sibling_info = media_file.parent / f"{stem}.info.json"
            if sibling_info.exists():
                continue
            # Check if there is ANY .info.json in the same directory.
            if any(media_file.parent.glob("*.info.json")):
                continue
            # Check for a qualifying nndownload sidecar in the same directory.
            sibling_nn = media_file.parent / f"{stem}.json"
            if sibling_nn.exists() and _nndownload_archive_key(sibling_nn) is not None:
                continue
            # Check if ANY qualifying nndownload sidecar exists in the directory.
            has_nn_sidecar = any(
                _nndownload_archive_key(p) is not None
                for p in media_file.parent.glob("*.json")
                if not p.name.endswith(".info.json") and not p.name.endswith(".comments.json")
            )
            if has_nn_sidecar:
                continue
            unverifiable.append(media_file)

    # Orphan archive entries: in archive but not evidenced by any info.json.
    orphan_count = len(archive_set - evidence_keys)

    # --- Report ---
    print(f"source: {name}")
    print(f"  evidence files scanned:   {scanned}")
    print(f"  already in archive:       {already_in_archive}")
    if dry_run:
        print(f"  would append:             {len(missing)}")
    else:
        print(f"  appended:                 {len(missing)}")

    if unverifiable:
        print()
        print(
            f"UNVERIFIABLE media files ({len(unverifiable)}) — "
            "these WILL be re-downloaded by future runs:"
        )
        for p in sorted(unverifiable):
            print(f"  {p}")

    if orphan_count:
        print()
        print(
            f"Orphan archive entries: {orphan_count} "
            "(kept on purpose — they suppress re-downloads of deleted items)"
        )

    if skipped_unparseable:
        print()
        print(f"Warnings — unparseable JSON files ({len(skipped_unparseable)}):")
        for p in skipped_unparseable:
            print(f"  {p}")

    if dry_run:
        return 0

    # --- Write ---
    if missing:
        append_archive_entries(archive_path, missing)

    # Upsert imported_at into source_state.
    imported_at = datetime.now(tz=timezone.utc).isoformat()
    db_path = paths.state_db()
    with state.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, imported_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET imported_at = excluded.imported_at
            """,
            (name, imported_at),
        )

    return 0


def _local_ts(utc_iso: str, width: int = 16) -> str:
    """Convert a UTC ISO-8601 string to a local-time display string.

    Parameters
    ----------
    utc_iso:
        UTC ISO-8601 timestamp string as stored in the database.
    width:
        How many characters to keep after converting (default 16 → ``YYYY-MM-DD HH:MM``).
    """
    try:
        dt = datetime.fromisoformat(utc_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")[:width]
    except Exception:
        return utc_iso[:width].replace("T", " ")


def _local_tz_name() -> str:
    """Return the local timezone abbreviation (e.g. 'JST', 'UTC')."""
    return datetime.now().astimezone().tzname() or "local"


def compute_next_due(schedule: str, last_check_at: str | None, now: datetime | None = None) -> datetime:
    """Return the next scheduled datetime after *last_check_at*.

    Parameters
    ----------
    schedule:
        A valid cron expression.
    last_check_at:
        ISO-8601 timestamp string of the last check, or *None* if the source
        has never been run (treated as immediately due).
    now:
        The current time for due-ness comparison; defaults to
        ``datetime.now(timezone.utc)``.  Pass an explicit value in tests.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if last_check_at is None:
        # Never run → return a sentinel that is always in the past.
        return now.replace(year=1970)
    base_dt = datetime.fromisoformat(last_check_at)
    return croniter(schedule, base_dt).get_next(datetime)


def is_source_due(schedule: str, last_check_at: str | None, now: datetime | None = None) -> bool:
    """Return True iff the source's next scheduled run is <= *now*."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if last_check_at is None:
        return True
    next_dt = compute_next_due(schedule, last_check_at, now)
    return next_dt <= now


def _query_last_check_at(db_path: Path, name: str) -> str | None:
    """Return the last_check_at value for *name*, or None."""
    if not db_path.exists():
        return None
    try:
        with state.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_check_at FROM source_state WHERE source = ?",
                (name,),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def run_command(args) -> int:
    """Run yt-dlp for a named source (or all/due sources) and persist the result."""
    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    db_path = paths.state_db()

    force: bool = getattr(args, "force", False)

    # --- kura run NAME [--force] ---
    if args.name is not None:
        name: str = args.name
        source = next((s for s in config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
            return 1

        if not source.enabled and not force:
            print(f"error: source {name!r} is disabled; use --force to run it anyway")
            return 1

        notify_enabled = bool(config.defaults.get("notify", True))
        exit_code = run_source(source, paths.config_dir(), paths.state_dir(), notify=notify_enabled)
        if exit_code is None:
            return 0  # locked → skip is not an error
        return 0 if exit_code in (0, 101) else 1

    # --- kura run --due / --all ---
    due_mode: bool = getattr(args, "due", False)
    all_mode: bool = getattr(args, "all", False)

    now = datetime.now(tz=timezone.utc)
    any_failure = False
    due_sources: list = []

    for source in config.sources:
        if not source.enabled:
            print(f"  {source.name}: disabled")
            continue

        if due_mode:
            last_check_at = _query_last_check_at(db_path, source.name)
            if not is_source_due(source.schedule, last_check_at, now):
                next_dt = compute_next_due(source.schedule, last_check_at, now)
                next_local = next_dt.astimezone().strftime("%Y-%m-%d %H:%M")
                tz_name = _local_tz_name()
                print(f"  {source.name}: not due (next {next_local} {tz_name})")
                continue

        due_sources.append(source)

    # Run due sources — serially or concurrently based on config.
    concurrency: int = config.defaults.get("concurrency", 1)
    notify_enabled = bool(config.defaults.get("notify", True))

    if concurrency <= 1 or len(due_sources) <= 1:
        # Serial path: stream output normally.
        for source in due_sources:
            exit_code = run_source(source, paths.config_dir(), paths.state_dir(), notify=notify_enabled)
            if exit_code is None:
                print(f"  {source.name}: skipped (already running)")
            elif exit_code in (0, 101):
                print(f"  {source.name}: ran (exit={exit_code})")
            else:
                print(f"  {source.name}: failed (exit={exit_code})")
                any_failure = True
    else:
        # Concurrent path: run up to `concurrency` sources at once, quiet mode.
        def _run_one(src):
            return src, run_source(
                src, paths.config_dir(), paths.state_dir(), quiet=True, notify=notify_enabled
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_run_one, src): src for src in due_sources}
            for fut in concurrent.futures.as_completed(futures):
                src, exit_code = fut.result()
                if exit_code is None:
                    print(f"  {src.name}: skipped (already running)")
                elif exit_code in (0, 101):
                    print(f"  {src.name}: ran (exit={exit_code})")
                else:
                    print(f"  {src.name}: failed (exit={exit_code})")
                    any_failure = True

    return 1 if any_failure else 0


def verify_command(args) -> int:
    """Dry-run for a named source to report pending downloads."""
    name: str = args.name

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
        return 1

    source.output_dir.mkdir(parents=True, exist_ok=True)

    if source.downloader == "nndownload":
        return _verify_nndownload(source)

    # yt-dlp path (unchanged).
    profile_path = paths.profile_file(paths.config_dir(), source.profile)
    cmd = build_command(source, profile_path, simulate=True)

    proc = subprocess.run(
        cmd,
        cwd=source.output_dir,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    count = len(lines)

    print(f"pending: {count}")
    for line in lines[:20]:
        print(f"  {line}")
    print("pending = items yt-dlp would download now (already-archived items are excluded)")

    if proc.returncode in (0, 101):
        return 0
    return 1


def _verify_nndownload(source) -> int:
    """Verify an nndownload source using --list-ids."""
    import re as _re
    from .archive import read_archive

    # Build: nndownload --non-interactive <auth> --list-ids <url>
    cmd = ["nndownload", "--non-interactive"]
    if source.auth:
        cmd.extend(shlex.split(source.auth))
    cmd += ["--list-ids", source.url]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Parse ids: non-empty token lines that look like video IDs.
    # Ignore log-noise lines containing spaces.
    all_lines = proc.stdout.splitlines()
    id_lines = [
        line.strip()
        for line in all_lines
        if line.strip() and " " not in line.strip()
    ]
    ids = set(id_lines)

    # Read archived niconico ids.
    archive_entries = read_archive(source.archive_file)
    archived_ids: set[str] = set()
    for entry in archive_entries:
        if entry.startswith("niconico "):
            archived_ids.add(entry.split(" ", 1)[1])

    # Zero-ID guard: if we parsed nothing but the archive is non-empty, treat as failure.
    if not ids and archived_ids:
        print(
            f"error: enumeration returned no ids — treating as failure, not an empty backlog"
        )
        return 1

    pending = ids - archived_ids
    count = len(pending)
    print(f"pending: {count}")
    for vid_id in sorted(pending)[:20]:
        print(f"  {vid_id}")
    print("pending = items nndownload would download now (already-archived items are excluded)")

    if proc.returncode in (0, 101):
        return 0
    return 1


def status_command(args) -> int:
    """Print a summary table of all sources, or detailed view for one source."""
    name: str | None = getattr(args, "name", None)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    db_path = paths.state_db()
    try:
        db = state.connect(db_path)
    except Exception:
        db = None

    if name is not None:
        # Detailed single-source view.
        source = next((s for s in config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
            if db is not None:
                db.close()
            return 1
        rc = _status_detail(name, db)
        if db is not None:
            db.close()
        return rc

    def query_source_state(src_name: str) -> dict:
        if db is None:
            return {}
        row = db.execute(
            "SELECT last_check_at, imported_at FROM source_state WHERE source = ?",
            (src_name,),
        ).fetchone()
        if row is None:
            return {}
        return {"last_check_at": row[0], "imported_at": row[1]}

    def query_last_run(src_name: str) -> dict:
        if db is None:
            return {}
        row = db.execute(
            """
            SELECT ended_at, exit_code, new_count
            FROM runs WHERE source = ?
            ORDER BY id DESC LIMIT 1
            """,
            (src_name,),
        ).fetchone()
        if row is None:
            return {}
        return {"ended_at": row[0], "exit_code": row[1], "new_count": row[2]}

    def query_permanent_fails(src_name: str) -> int:
        if db is None:
            return 0
        row = db.execute(
            """
            SELECT count(*) FROM video_failures
            WHERE source = ? AND kind = 'permanent' AND attempts >= 3
            """,
            (src_name,),
        ).fetchone()
        return row[0] if row else 0

    header = ("name", "enabled", "imported", "last_run", "exit", "new", "fails", "next_due")
    rows: list[tuple[str, ...]] = [header]

    any_permanent = False
    for source in config.sources:
        ss = query_source_state(source.name)
        lr = query_last_run(source.name)
        pf = query_permanent_fails(source.name)

        enabled_str = "yes" if source.enabled else "no"
        imported_str = "yes" if ss.get("imported_at") else "no"

        last_run_str = "-"
        exit_str = "-"
        new_str = "-"
        if lr.get("ended_at"):
            last_run_str = _local_ts(lr["ended_at"])
            exit_str = str(lr["exit_code"])
            new_str = str(lr["new_count"])

        fails_str = str(pf) if pf > 0 else "-"
        if pf > 0:
            any_permanent = True

        last_check_at = ss.get("last_check_at")
        if last_check_at:
            try:
                next_dt = compute_next_due(source.schedule, last_check_at)
                next_due_str = next_dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_due_str = "now"
        else:
            next_due_str = "now"

        rows.append((
            source.name,
            enabled_str,
            imported_str,
            last_run_str,
            exit_str,
            new_str,
            fails_str,
            next_due_str,
        ))

    if db is not None:
        db.close()

    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * w for w in widths).rstrip())

    tz_name = _local_tz_name()
    print(f"(times shown in {tz_name})")

    if any_permanent:
        print()
        print("permanent failures detected; inspect with `kura status NAME` and consider `kura suppress`")

    return 0


def _status_detail(name: str, db) -> int:
    """Print detailed view for a single source."""
    print(f"source: {name}")
    print()

    # Last 5 runs.
    if db is not None:
        runs = db.execute(
            """
            SELECT started_at, exit_code, new_count, failure_summary
            FROM runs WHERE source = ?
            ORDER BY id DESC LIMIT 5
            """,
            (name,),
        ).fetchall()
    else:
        runs = []

    tz_name = _local_tz_name()
    print(f"recent runs (times in {tz_name}):")
    if runs:
        run_header = ("time", "exit", "new", "failures")
        run_rows: list[tuple[str, ...]] = [run_header]
        for r in runs:
            started = _local_ts(r[0]) if r[0] else "-"
            run_rows.append((
                started,
                str(r[1]) if r[1] is not None else "-",
                str(r[2]) if r[2] is not None else "-",
                r[3] or "-",
            ))
        widths = [max(len(row[i]) for row in run_rows) for i in range(len(run_header))]
        for idx, row in enumerate(run_rows):
            print("  " + "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip())
            if idx == 0:
                print("  " + "  ".join("-" * w for w in widths).rstrip())
    else:
        print("  (no runs yet)")

    print()

    # Permanent failures.
    if db is not None:
        failures = db.execute(
            """
            SELECT video_id, attempts, last_error
            FROM video_failures
            WHERE source = ? AND kind = 'permanent'
            ORDER BY attempts DESC, video_id
            """,
            (name,),
        ).fetchall()
    else:
        failures = []

    print("permanent failures:")
    if failures:
        for vid_id, attempts, last_error in failures:
            trunc = (last_error or "")[:80]
            print(f"  {vid_id}  attempts={attempts}  {trunc}")
    else:
        print("  (none)")

    print()

    # Suppressions.
    if db is not None:
        suppressions = db.execute(
            """
            SELECT archive_key, suppressed_at, reason
            FROM suppressions WHERE source = ?
            ORDER BY suppressed_at
            """,
            (name,),
        ).fetchall()
    else:
        suppressions = []

    print("suppressions:")
    if suppressions:
        for key, suppressed_at, reason in suppressions:
            ts = (suppressed_at or "")[:16].replace("T", " ")
            reason_str = f"  ({reason})" if reason else ""
            print(f"  {key}  {ts}{reason_str}")
    else:
        print("  (none)")

    return 0


def pause_command(args) -> int:
    """Disable a source in config.yaml (pause scheduling)."""
    name: str = args.name
    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
        return 1

    was = source.enabled
    set_source_enabled(name, enabled=False, config_path=paths.config_file())
    if was:
        print(f"{name}: enabled → disabled")
    else:
        print(f"{name}: already disabled")
    return 0


def resume_command(args) -> int:
    """Enable a source in config.yaml (resume scheduling)."""
    name: str = args.name
    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
        return 1

    was = source.enabled
    set_source_enabled(name, enabled=True, config_path=paths.config_file())
    if not was:
        print(f"{name}: disabled → enabled")
    else:
        print(f"{name}: already enabled")
    return 0


def suppress_command(args) -> int:
    """Append a video to the archive (suppressing future downloads) and record suppression."""
    source_name: str = args.name
    video_id: str = args.video_id
    reason: str | None = getattr(args, "reason", None)
    explicit_extractor: str | None = getattr(args, "extractor", None) or None

    # Validate video_id: non-empty, no spaces.
    if not video_id or " " in video_id:
        print(f"error: invalid video_id {video_id!r}: must be non-empty and contain no spaces")
        return 1

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == source_name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {source_name!r}" + (f"; known: {known}" if known else ""))
        return 1

    # Derive default extractor from source downloader when not explicitly given.
    if explicit_extractor is not None:
        extractor = explicit_extractor
    elif getattr(source, "downloader", None) == "nndownload":
        extractor = "niconico"
    else:
        extractor = "youtube"

    db_path = paths.state_db()
    state.init_db(db_path)

    with source_lock(source_name, paths.state_dir()) as acquired:
        if not acquired:
            print(f"error: {source_name} is currently running; cannot suppress while lock is held")
            return 1

        archive_entry = f"{extractor} {video_id}"
        archive_path = source.archive_file

        # Check if already present.
        existing = read_archive(archive_path)
        if archive_entry in existing:
            print(f"warning: {archive_entry!r} is already in the archive; skipping append")
        else:
            append_archive_entries(archive_path, [archive_entry])

        # Insert/update suppressions row.
        suppressed_at = datetime.now(tz=timezone.utc).isoformat()
        with state.connect(db_path) as db:
            db.execute(
                """
                INSERT INTO suppressions (source, archive_key, suppressed_at, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, archive_key) DO UPDATE SET
                    suppressed_at = excluded.suppressed_at,
                    reason        = excluded.reason
                """,
                (source_name, archive_entry, suppressed_at, reason),
            )
            # Remove video_failures row if present.
            db.execute(
                "DELETE FROM video_failures WHERE source = ? AND video_id = ?",
                (source_name, video_id),
            )

    print(f"suppressed: {archive_entry}  (extractor: {extractor})")
    return 0


def unsuppress_command(args) -> int:
    """Remove a suppressed video from the archive and delete the suppression record."""
    source_name: str = args.name
    video_id: str = args.video_id
    extractor: str = getattr(args, "extractor", "youtube") or "youtube"

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == source_name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {source_name!r}" + (f"; known: {known}" if known else ""))
        return 1

    archive_entry = f"{extractor} {video_id}"
    db_path = paths.state_db()
    state.init_db(db_path)

    # Require a matching suppressions row (kura only removes lines IT added).
    with state.connect(db_path) as db:
        row = db.execute(
            "SELECT archive_key FROM suppressions WHERE source = ? AND archive_key = ?",
            (source_name, archive_entry),
        ).fetchone()

    if row is None:
        print(
            f"error: no suppression record for {archive_entry!r} in source {source_name!r}; "
            "kura can only unsuppress entries it added"
        )
        return 1

    with source_lock(source_name, paths.state_dir()) as acquired:
        if not acquired:
            print(f"error: {source_name} is currently running; cannot unsuppress while lock is held")
            return 1

        # Remove the line from the archive file.
        archive_path = source.archive_file
        try:
            remove_archive_entry(archive_path, archive_entry)
        except FileNotFoundError:
            print(f"error: archive file not found: {archive_path}")
            return 1
        except ValueError:
            print(f"error: {archive_entry!r} not found in archive; it may have been removed already")
            return 1

        # Delete suppressions row.
        with state.connect(db_path) as db:
            db.execute(
                "DELETE FROM suppressions WHERE source = ? AND archive_key = ?",
                (source_name, archive_entry),
            )

    print(f"unsuppressed: {archive_entry}")
    return 0


def logs_command(args) -> int:
    """Print recent log lines (or log path) for the most recent run of a source."""
    name: str = args.name
    n: int = getattr(args, "n", 50) or 50
    path_only: bool = getattr(args, "path", False)

    try:
        config = load_config()
    except ConfigError as error:
        print(f"error: {error}")
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(f"error: unknown source {name!r}" + (f"; known: {known}" if known else ""))
        return 1

    # Find the most recent log file for this source.
    log_source_dir = paths.logs_dir() / name
    if not log_source_dir.is_dir():
        print(f"no logs yet for {name!r}")
        return 0

    log_files = sorted(log_source_dir.glob("*.log"))
    if not log_files:
        print(f"no logs yet for {name!r}")
        return 0

    latest_log = log_files[-1]

    if path_only:
        print(str(latest_log.resolve()))
        return 0

    # Print the last n lines.
    try:
        content = latest_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read log {latest_log}: {exc}")
        return 1

    lines = content.splitlines()
    tail = lines[-n:] if len(lines) > n else lines
    for line in tail:
        print(line)
    return 0


def doctor_command(_args) -> int:
    """Run a series of health checks and report ok/warn/FAIL for each.

    Exits 0 when no FAIL lines were emitted, 1 otherwise.
    """
    has_fail = False

    def ok(msg: str) -> None:
        print(f"ok: {msg}")

    def warn(msg: str) -> None:
        print(f"warn: {msg}")

    def fail(msg: str) -> None:
        nonlocal has_fail
        has_fail = True
        print(f"FAIL: {msg}")

    def info(msg: str) -> None:
        print(f"info: {msg}")

    # 1. yt-dlp on PATH.
    ytdlp_path = shutil.which("yt-dlp")
    if ytdlp_path is None:
        fail("yt-dlp not found on PATH")
    else:
        try:
            ver_result = subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            ytdlp_version = ver_result.stdout.strip()
            ok(f"yt-dlp found: {ytdlp_version}")
            # Warn if the version date is older than 90 days.
            # yt-dlp versions are YYYY.MM.DD (optionally with .N suffix).
            import re as _re
            m = _re.match(r"(\d{4})\.(\d{2})\.(\d{2})", ytdlp_version)
            if m:
                ver_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                age_days = (date.today() - ver_date).days
                if age_days > 90:
                    warn(
                        f"yt-dlp version is {age_days} days old ({ytdlp_version}); "
                        "consider updating — extractors rot"
                    )
        except Exception as exc:
            fail(f"yt-dlp --version failed: {exc}")

    # 2. ffmpeg on PATH.
    if shutil.which("ffmpeg") is None:
        fail("ffmpeg not found on PATH (needed for embedding/merging)")
    else:
        ok("ffmpeg found")

    # 3. Config loads cleanly.
    try:
        config = load_config()
        ok("config.yaml loads cleanly")
    except (ConfigError, Exception) as exc:
        fail(f"config.yaml: {exc}")
        return 1  # skip per-source checks

    # 3a. notify-send / libnotify (only checked when notifications are enabled).
    if config.defaults.get("notify", True):
        if shutil.which("notify-send") is not None:
            ok("notify-send found")
        else:
            warn("notify-send not found — desktop notifications disabled (install libnotify)")

    # 3b. nndownload checks (only when at least one nndownload source exists).
    has_nndownload_sources = any(s.downloader == "nndownload" for s in config.sources)
    if has_nndownload_sources:
        nn_path = shutil.which("nndownload")
        if nn_path is None:
            fail("nndownload not found on PATH (required for nndownload sources)")
        else:
            try:
                nn_ver_result = subprocess.run(
                    ["nndownload", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                nn_version = nn_ver_result.stdout.strip()
                ok(f"nndownload found: {nn_version}")
            except Exception as exc:
                fail(f"nndownload --version failed: {exc}")
                nn_version = ""

            # Check that nndownload is the patched version (has --download-archive).
            try:
                help_result = subprocess.run(
                    ["nndownload", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                help_text = help_result.stdout + help_result.stderr
                if "--download-archive" not in help_text:
                    fail("unpatched nndownload: --download-archive not found in --help output")
                else:
                    ok("nndownload is patched (--download-archive present)")
            except Exception as exc:
                fail(f"nndownload --help failed: {exc}")

    # 4. Per-source checks.
    for source in config.sources:
        prefix = f"source {source.name!r}"

        # output_dir.
        if not source.output_dir.exists():
            warn(f"{prefix}: output_dir {source.output_dir} does not exist (will be created on run)")
        elif not os.access(source.output_dir, os.W_OK):
            fail(f"{prefix}: output_dir {source.output_dir} exists but is not writable")
        else:
            ok(f"{prefix}: output_dir {source.output_dir} is writable")

        # archive_file parent.
        archive_parent = source.archive_file.parent
        if not os.access(archive_parent, os.W_OK):
            fail(f"{prefix}: archive_file parent {archive_parent} is not writable")
        else:
            ok(f"{prefix}: archive_file parent is writable")

        # archive line count (informational).
        if source.archive_file.exists():
            try:
                line_count = sum(1 for _ in source.archive_file.open("r", encoding="utf-8", errors="replace"))
                info(f"{prefix}: archive has {line_count} entries ({source.archive_file})")
            except OSError:
                warn(f"{prefix}: could not read archive file {source.archive_file}")
        else:
            info(f"{prefix}: archive file does not exist yet ({source.archive_file})")

        # Profile.
        profile_path = paths.profile_file(paths.config_dir(), source.profile)
        ok(f"{prefix}: profile {source.profile!r} → {profile_path}")

        # Cookies (ytdlp sources).
        if source.cookies:
            _check_cookies(source.cookies, prefix, ok, warn, fail)

        # nndownload per-source checks.
        if source.downloader == "nndownload":
            if source.auth:
                ok(f"{prefix}: auth present")
            _check_session_cookie(source.auth or "", prefix, ok, warn, fail)

    # 5. state.db integrity.
    db_path = paths.state_db()
    if not db_path.exists():
        warn(f"state.db does not exist yet ({db_path}); run `kura init`")
    else:
        try:
            db_size = db_path.stat().st_size
            with state.connect(db_path) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] == "ok":
                ok(f"state.db integrity ok ({db_size} bytes, {db_path})")
            else:
                fail(f"state.db PRAGMA quick_check returned {result!r}")
        except Exception as exc:
            fail(f"state.db could not be opened: {exc}")

    # 6. Currently-running sources (lock probe).
    for source in config.sources:
        with source_lock(source.name, paths.state_dir()) as acquired:
            if not acquired:
                info(f"{source.name!r} appears to be running right now")
            # Lock is released immediately when acquired — harmless.

    # 7. systemd timer status.
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        warn("systemctl not found; cannot check kura.timer status")
    else:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "kura.timer"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = result.stdout.strip()
            if status == "active":
                ok("kura.timer is active")
            else:
                warn(f"kura.timer is {status!r} — scheduling not enabled (see README)")
        except Exception as exc:
            warn(f"could not query kura.timer: {exc}")

    return 1 if has_fail else 0


def _check_cookies(cookies: str, prefix: str, ok, warn, fail) -> None:
    """Inspect the cookies string and validate any referenced files."""
    import shlex as _shlex
    try:
        parts = _shlex.split(cookies)
    except ValueError:
        warn(f"{prefix}: cookies string could not be parsed: {cookies!r}")
        return

    i = 0
    while i < len(parts):
        token = parts[i]
        if token == "--cookies" and i + 1 < len(parts):
            cookie_file = Path(parts[i + 1]).expanduser()
            if not cookie_file.exists():
                fail(f"{prefix}: cookies file not found: {cookie_file}")
            else:
                ok(f"{prefix}: cookies file found: {cookie_file}")
            i += 2
            continue
        if token == "--cookies-from-browser" and i + 1 < len(parts):
            browser = parts[i + 1]
            ok(
                f"{prefix}: cookies from browser {browser!r} "
                "(extraction tested only at run time)"
            )
            i += 2
            continue
        i += 1


def _check_session_cookie(auth: str, prefix: str, ok, warn, fail) -> None:
    """Check --session-cookie file in nndownload auth string."""
    import shlex as _shlex
    import stat as _stat
    try:
        parts = _shlex.split(auth)
    except ValueError:
        return

    i = 0
    while i < len(parts):
        if parts[i] == "--session-cookie" and i + 1 < len(parts):
            cookie_path = Path(parts[i + 1]).expanduser()
            if not cookie_path.exists():
                fail(f"{prefix}: session-cookie file not found: {cookie_path}")
            elif cookie_path.stat().st_size == 0:
                fail(f"{prefix}: session-cookie file is empty: {cookie_path}")
            else:
                ok(f"{prefix}: session-cookie file found: {cookie_path}")
                # Warn if world-readable.
                mode = cookie_path.stat().st_mode
                if mode & 0o004:
                    warn(f"{prefix}: session-cookie file is world-readable: {cookie_path} (consider chmod 600)")
            i += 2
            continue
        i += 1


def stub_command(command: str, note: str):
    def run(_args) -> int:
        print(f"{command}: not implemented yet ({note})")
        return 2

    return run


def handle_config_error(error: ConfigError) -> int:
    print(f"error: {error}")
    return 1
