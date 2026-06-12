from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter

from . import paths, state
from .archive import MEDIA_EXTENSIONS, append_archive_entries, archive_key, read_archive, remove_archive_entry
from .config import (
    DEFAULT_CONFIG_TEXT,
    DEFAULT_PROFILE_TEXT,
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


def _import_locked(source, name: str, dry_run: bool) -> int:
    archive_path = source.archive_file
    archive_set = read_archive(archive_path)

    # Scan output_dir recursively for .info.json files.
    output_dir = source.output_dir
    info_json_paths: list[Path] = sorted(output_dir.rglob("*.info.json")) if output_dir.is_dir() else []

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

    missing = sorted(evidence_keys - archive_set)

    # Detect unverifiable media files.
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
            unverifiable.append(media_file)

    # Orphan archive entries: in archive but not evidenced by any info.json.
    orphan_count = len(archive_set - evidence_keys)

    # --- Report ---
    print(f"source: {name}")
    print(f"  info.json files scanned:  {scanned}")
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

        exit_code = run_source(source, paths.config_dir(), paths.state_dir())
        if exit_code is None:
            return 0  # locked → skip is not an error
        return 0 if exit_code in (0, 101) else 1

    # --- kura run --due / --all ---
    due_mode: bool = getattr(args, "due", False)
    all_mode: bool = getattr(args, "all", False)

    now = datetime.now(tz=timezone.utc)
    any_failure = False

    for source in config.sources:
        if not source.enabled:
            print(f"  {source.name}: disabled")
            continue

        if due_mode:
            last_check_at = _query_last_check_at(db_path, source.name)
            if not is_source_due(source.schedule, last_check_at, now):
                next_dt = compute_next_due(source.schedule, last_check_at, now)
                print(f"  {source.name}: not due (next {next_dt.strftime('%Y-%m-%d %H:%M')} UTC)")
                continue

        exit_code = run_source(source, paths.config_dir(), paths.state_dir())
        if exit_code is None:
            print(f"  {source.name}: skipped (already running)")
        elif exit_code in (0, 101):
            print(f"  {source.name}: ran (exit={exit_code})")
        else:
            print(f"  {source.name}: failed (exit={exit_code})")
            any_failure = True

    return 1 if any_failure else 0


def verify_command(args) -> int:
    """Dry-run yt-dlp for a named source to report pending downloads."""
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
            last_run_str = lr["ended_at"][:16].replace("T", " ")
            exit_str = str(lr["exit_code"])
            new_str = str(lr["new_count"])

        fails_str = str(pf) if pf > 0 else "-"
        if pf > 0:
            any_permanent = True

        last_check_at = ss.get("last_check_at")
        if last_check_at:
            try:
                next_dt = compute_next_due(source.schedule, last_check_at)
                next_due_str = next_dt.strftime("%Y-%m-%d %H:%M")
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

    print("recent runs:")
    if runs:
        run_header = ("time", "exit", "new", "failures")
        run_rows: list[tuple[str, ...]] = [run_header]
        for r in runs:
            started = (r[0] or "")[:16].replace("T", " ")
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
    extractor: str = getattr(args, "extractor", "youtube") or "youtube"

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

    print(f"suppressed: {archive_entry}")
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


def stub_command(command: str, note: str):
    def run(_args) -> int:
        print(f"{command}: not implemented yet ({note})")
        return 2

    return run


def handle_config_error(error: ConfigError) -> int:
    print(f"error: {error}")
    return 1
