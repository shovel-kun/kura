from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from croniter import croniter
from rich.console import Console
from rich.live import Live

from . import monitor, paths, state, ui
from .archive import (
    MEDIA_EXTENSIONS,
    append_archive_entries,
    archive_key,
    read_archive,
    remove_archive_entry,
)
from .archive import (
    nndownload_archive_key as _nndownload_archive_key,
)
from .config import (
    DEFAULT_CONFIG_TEXT,
    DEFAULT_PROFILE_TEXT,
    NICONICO_PROFILE_TEXT,
    Config,
    ConfigError,
    Source,
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
    names = [source.name for source in config.sources]
    details = [
        f"{source.downloader or 'ytdlp'} {ui.DOT} {source.update_strategy} {ui.DOT} "
        f"{ui.human_cron(source.schedule)} {ui.DOT} {source.profile}"
        for source in config.sources
    ]
    name_width = max(map(len, names), default=0)
    detail_width = max(map(len, details), default=0)
    for source, detail in zip(config.sources, details):
        glyph = ui.green(ui.ON) if source.enabled else ui.dim(ui.OFF)
        name = ui.bold(source.name) if source.enabled else ui.dim(source.name)
        print(
            f"  {glyph} {ui.pad(name, name_width)}   "
            f"{ui.pad(ui.dim(detail), detail_width)}   {source.output_dir}"
        )
    return 0


def import_command(args) -> int:
    """Adopt an existing yt-dlp archive directory into kura."""
    dry_run: bool = args.dry_run
    name: str = args.name

    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
        return 1

    # Acquire lock to prevent concurrent run-vs-import on the same archive.
    with source_lock(name, paths.state_dir()) as acquired:
        if not acquired:
            print(ui.dim(f"{name} — skipped, already running; import aborted to avoid archive corruption"))
            return 1
        return _import_locked(source, name, dry_run)


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
    print(ui.bold(name))
    append_label = "would append" if dry_run else "appended"
    labels = ("scanned", "in archive", append_label)
    label_width = max(map(len, labels))
    print(f"  {'scanned':<{label_width}} {scanned} evidence files")
    print(f"  {'in archive':<{label_width}} {already_in_archive}")
    append_line = f"{append_label:<{label_width}} {len(missing)}"
    append_style = ui.green if missing else ui.dim
    print(f"  {append_style(append_line)}")

    if unverifiable:
        print()
        print(ui.red(f"  unverifiable media ({len(unverifiable)}) — WILL be re-downloaded:"))
        for p in sorted(unverifiable):
            print(ui.red(f"  {ui.FAIL} {p}"))

    if orphan_count:
        print()
        noun = "entry" if orphan_count == 1 else "entries"
        print(ui.dim(
            f"  {orphan_count} orphan archive {noun} kept "
            "(suppresses re-downloads of deleted items)"
        ))

    if skipped_unparseable:
        print()
        print(ui.yellow(f"Warnings — unparseable JSON files ({len(skipped_unparseable)}):"))
        for p in skipped_unparseable:
            print(ui.yellow(f"  {p}"))

    if dry_run:
        return 0

    # --- Write ---
    if missing:
        append_archive_entries(archive_path, missing)

    # Upsert imported_at into source_state.
    imported_at = datetime.now(tz=UTC).isoformat()
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
        now = datetime.now(tz=UTC)
    if last_check_at is None:
        # Never run → return a sentinel that is always in the past.
        return now.replace(year=1970)
    base_dt = datetime.fromisoformat(last_check_at)
    return croniter(schedule, base_dt).get_next(datetime)


def is_source_due(schedule: str, last_check_at: str | None, now: datetime | None = None) -> bool:
    """Return True iff the source's next scheduled run is <= *now*."""
    if now is None:
        now = datetime.now(tz=UTC)
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
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    db_path = paths.state_db()

    force: bool = getattr(args, "force", False)

    # --- kura run NAME [--force] ---
    if args.name is not None:
        name: str = args.name
        source = next((s for s in config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
            return 1

        if not source.enabled and not force:
            print(ui.red(f"{ui.FAIL} source {name!r} is disabled; use --force to run it anyway"))
            return 1

        notify_enabled = bool(config.defaults.get("notify", True))
        exit_code = run_source(source, paths.config_dir(), paths.state_dir(), notify=notify_enabled)
        if exit_code is None:
            return 0  # locked → skip is not an error
        return 0 if exit_code in (0, 101) else 1

    # --- kura run --due / --all ---
    due_mode: bool = getattr(args, "due", False)
    all_mode: bool = getattr(args, "all", False)

    now = datetime.now(tz=UTC)
    any_failure = False
    due_sources: list = []

    for source in config.sources:
        if not source.enabled:
            print(ui.dim(f"  {source.name} — disabled"))
            continue

        if due_mode:
            last_check_at = _query_last_check_at(db_path, source.name)
            if not is_source_due(source.schedule, last_check_at, now):
                next_dt = compute_next_due(source.schedule, last_check_at, now)
                print(ui.dim(f"  {source.name} — not due (next {ui.rel_time(next_dt, now)})"))
                continue

        due_sources.append(source)

    # Run due sources — serially or concurrently based on config.
    concurrency: int = config.defaults.get("concurrency", 1)
    notify_enabled = bool(config.defaults.get("notify", True))

    if concurrency <= 1 or len(due_sources) <= 1:
        # Serial path: stream output normally.
        for source in due_sources:
            exit_code = run_source(
                source,
                paths.config_dir(),
                paths.state_dir(),
                notify=notify_enabled,
                summary_indent="  ",
            )
            if exit_code is not None and exit_code not in (0, 101):
                any_failure = True
    else:
        # Concurrent path: run up to `concurrency` sources at once, quiet mode.
        def _run_one(src):
            return run_source(
                src,
                paths.config_dir(),
                paths.state_dir(),
                quiet=True,
                notify=notify_enabled,
                summary_indent="  ",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(_run_one, src): src for src in due_sources}
            for fut in concurrent.futures.as_completed(futures):
                exit_code = fut.result()
                if exit_code is not None and exit_code not in (0, 101):
                    any_failure = True

    return 1 if any_failure else 0


def verify_command(args) -> int:
    """Dry-run for a named source to report pending downloads."""
    name: str = args.name

    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
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
    _print_verify_result(name, lines)

    if proc.returncode in (0, 101):
        return 0
    return 1


def _verify_nndownload(source) -> int:
    """Verify an nndownload source using --list-ids."""
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
        print(ui.red(f"{ui.FAIL} enumeration returned no ids — treating as failure, not an empty backlog"))
        return 1

    pending = ids - archived_ids
    _print_verify_result(source.name, sorted(pending))

    if proc.returncode in (0, 101):
        return 0
    return 1


def _print_verify_result(name: str, lines: list[str]) -> None:
    count = len(lines)
    if count == 0:
        print(f"{ui.bold(name)} {ui.dim('—')} {ui.green(f'{ui.OK} up to date')}")
        return

    print(f"{ui.bold(name)} {ui.dim('—')} {ui.yellow(f'{count} pending')}")
    for line in lines[:20]:
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            item_id, rest = parts[1], parts[2]
        else:
            item_id, rest = parts[0], ""
        suffix = f"  {ui.dim(rest)}" if rest else ""
        print(f"  {item_id}{suffix}")
    if count > 20:
        print(ui.dim(f"  … and {count - 20} more"))


def query_source_state(db: sqlite3.Connection | None, name: str) -> dict:
    if db is None:
        return {}
    row = db.execute(
        "SELECT last_check_at, imported_at FROM source_state WHERE source = ?",
        (name,),
    ).fetchone()
    if row is None:
        return {}
    return {"last_check_at": row[0], "imported_at": row[1]}


def query_last_run(db: sqlite3.Connection | None, name: str) -> dict:
    if db is None:
        return {}
    row = db.execute(
        """
        SELECT ended_at, exit_code, new_count
        FROM runs WHERE source = ?
        ORDER BY id DESC LIMIT 1
        """,
        (name,),
    ).fetchone()
    if row is None:
        return {}
    return {"ended_at": row[0], "exit_code": row[1], "new_count": row[2]}


def query_permanent_fails(db: sqlite3.Connection | None, name: str) -> int:
    if db is None:
        return 0
    row = db.execute(
        """
        SELECT count(*) FROM video_failures
        WHERE source = ? AND kind = 'permanent' AND attempts >= 3
        """,
        (name,),
    ).fetchone()
    return row[0] if row else 0


def _progress_for_source(name: str, state_dir: Path) -> dict:
    log_path = monitor.current_log(name, state_dir)
    if log_path is None:
        return monitor.parse_progress([])
    return monitor.parse_progress(monitor.tail_lines(log_path))


def _running_outcome(name: str, state_dir: Path) -> str:
    pct = _progress_for_source(name, state_dir)["pct"]
    suffix = f" {pct:g}%" if pct is not None else ""
    return ui.yellow(f"{ui.ON} running{suffix}")


def _watch_snapshots(
    sources: list[Source],
    running_names: set[str],
    state_dir: Path,
    archive_baselines: dict[str, int],
) -> list[monitor.WatchSnapshot]:
    snapshots: list[monitor.WatchSnapshot] = []
    for source in sources:
        archive_delta = max(
            0,
            monitor.archive_count(source.archive_file) - archive_baselines[source.name],
        )
        if source.name in running_names:
            progress = _progress_for_source(source.name, state_dir)
            snapshots.append(
                monitor.WatchSnapshot(
                    name=source.name,
                    running=True,
                    archive_delta=archive_delta,
                    **progress,
                )
            )
        else:
            snapshots.append(
                monitor.WatchSnapshot(
                    name=source.name,
                    running=False,
                    archive_delta=archive_delta,
                )
            )
    return snapshots


def watch_command(args) -> int:
    """Watch currently running sources by probing locks and tailing their logs."""
    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    name: str | None = getattr(args, "name", None)
    if name is not None:
        source = next((source for source in config.sources if source.name == name), None)
        if source is None:
            known = ", ".join(source.name for source in config.sources)
            print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
            return 1
        sources = [source]
    else:
        sources = [source for source in config.sources if source.enabled]

    state_dir = paths.state_dir()
    archive_baselines = {
        source.name: monitor.archive_count(source.archive_file) for source in sources
    }
    running_at_start = set(monitor.running_sources(config, state_dir))
    selected_names = {source.name for source in sources}
    running = running_at_start & selected_names

    if getattr(args, "once", False) or not sys.stdout.isatty():
        console = Console(file=sys.stdout, force_terminal=False, color_system=None)
        snapshots = _watch_snapshots(sources, running, state_dir, archive_baselines)
        console.print(monitor.render_watch(snapshots))
        return 0

    if not running and getattr(args, "wait", False):
        # A run that has nothing new finishes in about a second, so the window
        # where anything is live is easy to miss. Sit and wait for one instead.
        print(ui.dim("waiting for a run to start · ctrl-c to stop"))
        try:
            while not running:
                time.sleep(args.interval)
                running = set(monitor.running_sources(config, state_dir)) & selected_names
        except KeyboardInterrupt:
            return 0

    if not running:
        print(
            ui.dim(
                "nothing running · start a run with `kura run --all`,"
                " or use `kura watch --wait` to wait for one"
            )
        )
        return 0

    snapshots = _watch_snapshots(sources, running, state_dir, archive_baselines)
    empty_polls = 0
    console = Console(file=sys.stdout)
    try:
        with Live(
            monitor.render_watch(snapshots),
            console=console,
            auto_refresh=False,
        ) as live:
            while empty_polls < 3:
                time.sleep(args.interval)
                running = set(monitor.running_sources(config, state_dir)) & selected_names
                snapshots = _watch_snapshots(sources, running, state_dir, archive_baselines)
                live.update(monitor.render_watch(snapshots), refresh=True)
                empty_polls = empty_polls + 1 if not running else 0
    except KeyboardInterrupt:
        pass

    for source in sources:
        new_count = max(
            0,
            monitor.archive_count(source.archive_file) - archive_baselines[source.name],
        )
        print(f"{ui.green(ui.OK)} {source.name} {ui.dim('—')} +{new_count} new")
    return 0


def status_command(args) -> int:
    """Print a summary table of all sources, or detailed view for one source."""
    name: str | None = getattr(args, "name", None)

    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    db_path = paths.state_db()
    db = state.connect(db_path) if db_path.exists() else None

    if name is not None:
        # Detailed single-source view.
        source = next((s for s in config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
            if db is not None:
                db.close()
            return 1
        rc = _status_detail(source, db, config=config)
        if db is not None:
            db.close()
        return rc

    state_dir = paths.state_dir()
    running = set(monitor.running_sources(config, state_dir))
    rows: list[tuple[object, str, str]] = []
    any_permanent = False
    for source in config.sources:
        ss = query_source_state(db, source.name)
        lr = query_last_run(db, source.name)
        pf = query_permanent_fails(db, source.name)
        if pf > 0:
            any_permanent = True

        if source.name in running:
            outcome = _running_outcome(source.name, state_dir)
        elif lr:
            success = lr["exit_code"] in (0, 101)
            if success and lr["new_count"]:
                outcome = ui.green(f"{ui.OK} +{lr['new_count']} new")
            elif success:
                outcome = ui.dim(f"{ui.OK} nothing new")
            else:
                outcome = ui.red(f"{ui.FAIL} failed (exit {lr['exit_code']})")
            outcome += ui.dim(f" {ui.DOT} {ui.rel_time(ui.local_dt(lr['ended_at']))}")
        else:
            outcome = ui.dim("never run")
        if source.name not in running:
            if not ss.get("imported_at"):
                outcome += " " + ui.yellow("not imported")
            if pf:
                outcome += " " + ui.red(f"{pf} failing")

        if source.enabled:
            last_check_at = ss.get("last_check_at")
            if last_check_at:
                next_text = f"next {ui.rel_time(compute_next_due(source.schedule, last_check_at))}"
            else:
                next_text = "next now"
        else:
            next_text = "paused"
        rows.append((source, outcome, next_text))

    if db is not None:
        db.close()

    source_count = len(config.sources)
    noun = "source" if source_count == 1 else "sources"
    print(f"kura {ui.dim(f'{ui.DOT} {source_count} {noun}')}")
    print()
    name_width = max((len(source.name) for source in config.sources), default=0)
    outcome_width = max((ui.visible_len(outcome) for _, outcome, _ in rows), default=0)
    for source, outcome, next_text in rows:
        glyph = ui.green(ui.ON) if source.enabled else ui.dim(ui.OFF)
        name_text = ui.bold(source.name) if source.enabled else ui.dim(source.name)
        print(
            f"  {glyph} {ui.pad(name_text, name_width)}   "
            f"{ui.pad(outcome, outcome_width)}    {ui.dim(next_text)}"
        )

    if any_permanent:
        print()
        print(ui.dim("  run `kura status NAME` for failure details"))

    return 0


def _status_detail(source, db, *, config: Config | None = None) -> int:
    """Print detailed view for a single source."""
    name = source.name
    config = config or load_config()
    state_dir = paths.state_dir()
    running = name in monitor.running_sources(config, state_dir)
    source_state = None
    if db is not None:
        source_state = db.execute(
            "SELECT last_check_at, imported_at FROM source_state WHERE source = ?",
            (name,),
        ).fetchone()

    print(ui.bold(name))
    print(ui.dim(f"  {source.url}"))
    glyph = ui.green(ui.ON) if source.enabled else ui.dim(ui.OFF)
    status_word = "enabled" if source.enabled else "disabled"
    metadata = [status_word, source.update_strategy, ui.human_cron(source.schedule), source.profile]
    if source_state and source_state[1]:
        metadata.append(f"imported {ui.local_ts(source_state[1], '%b %d')}")
    separator = ui.dim(f" {ui.DOT} ")
    print(f"  {glyph} " + separator.join(metadata))

    archived_count = 0
    if source.archive_file.exists():
        archived_count = sum(
            1 for _ in source.archive_file.open("r", encoding="utf-8", errors="replace")
        )
    print(f"  {source.output_dir} {ui.dim('—')} {archived_count} archived")
    if running:
        print(f"  {_running_outcome(name, state_dir)}")

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

    if runs:
        print()
        print(ui.dim("  runs"))
        times = [ui.local_ts(r[0]) if r[0] else "-" for r in runs]
        time_width = max(map(len, times))
        for r, started in zip(runs, times):
            success = r[1] in (0, 101)
            glyph = ui.green(ui.OK) if success else ui.red(ui.FAIL)
            if success and r[2]:
                outcome = ui.green(f"+{r[2]} new")
            elif success:
                outcome = ui.dim("nothing new")
            else:
                outcome = ui.red(f"failed (exit {r[1]})")
                outcome += ui.dim(f" {ui.DOT} see kura logs {name}")
            if r[3]:
                outcome += ui.dim(f" {ui.DOT} {r[3]}")
            print(f"  {glyph} {ui.pad(started, time_width)}   {outcome}")

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

    if failures:
        print()
        print(ui.dim("  failing permanently"))
        for vid_id, attempts, last_error in failures:
            trunc = (last_error or "")[:60]
            print(
                f"  {ui.red(ui.FAIL)} {vid_id}   "
                f"{ui.dim(f'{attempts} attempts {ui.DOT}')} {trunc}"
            )
        print(ui.dim(f"  suppress with `kura suppress {name} VIDEO_ID`"))

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

    if suppressions:
        print()
        print(ui.dim("  suppressed"))
        for key, suppressed_at, reason in suppressions:
            date_text = ui.local_ts(suppressed_at, "%b %d") if suppressed_at else "-"
            details = f"{ui.DOT} {date_text}"
            if reason:
                details = f"{ui.DOT} {reason} {ui.DOT} {date_text}"
            print(f"  {ui.dim(ui.OFF)} {key} {ui.dim(details)}")

    print()
    if not source.enabled:
        print(ui.dim(f"  paused — resume with `kura resume {name}`"))
    elif source_state and source_state[0]:
        next_due = compute_next_due(source.schedule, source_state[0])
        print(ui.dim(f"  next check {ui.rel_time(next_due)}"))
    else:
        print(ui.dim("  next check now"))

    return 0


def pause_command(args) -> int:
    """Disable a source in config.yaml (pause scheduling)."""
    name: str = args.name
    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
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
        suppressed_at = datetime.now(tz=UTC).isoformat()
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
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source = next((s for s in config.sources if s.name == name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
        return 1

    # Find the most recent log file for this source.
    log_source_dir = paths.logs_dir() / name
    if not log_source_dir.is_dir():
        print(ui.dim(f"no logs yet for {name!r}"))
        return 0

    log_files = sorted(log_source_dir.glob("*.log"))
    if not log_files:
        print(ui.dim(f"no logs yet for {name!r}"))
        return 0

    latest_log = log_files[-1]

    if path_only:
        print(str(latest_log.resolve()))
        return 0

    # Print the last n lines.
    try:
        content = latest_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(ui.red(f"{ui.FAIL} cannot read log {latest_log}: {exc}"))
        return 1

    lines = content.splitlines()
    tail = lines[-n:] if len(lines) > n else lines
    for line in tail:
        print(line)
    return 0


def doctor_command(_args) -> int:
    """Run a series of health checks and report each with a status glyph.

    Exits 0 when no FAIL lines were emitted, 1 otherwise.
    """
    has_fail = False

    def ok(msg: str) -> None:
        print(f"{ui.green(ui.OK)} {msg}")

    def warn(msg: str) -> None:
        print(f"{ui.yellow('!')} {msg}")

    def fail(msg: str) -> None:
        nonlocal has_fail
        has_fail = True
        print(f"{ui.red(ui.FAIL)} {msg}")

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
            ok(f"yt-dlp {ytdlp_version}")
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
        ok("ffmpeg")

    # 3. Config loads cleanly.
    try:
        config = load_config()
        ok("config.yaml")
    except (ConfigError, Exception) as exc:
        fail(f"config.yaml: {exc}")
        return 1  # skip per-source checks

    # 3a. notify-send / libnotify (only checked when notifications are enabled).
    if config.defaults.get("notify", True):
        if shutil.which("notify-send") is not None:
            ok("notify-send")
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
                ok(f"nndownload {nn_version}")
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
                    fail(
                        "nndownload unpatched — reinstall from the clone "
                        "(--download-archive missing)"
                    )
                else:
                    ok("nndownload patched")
            except Exception as exc:
                fail(f"nndownload --help failed: {exc}")

    # 4. Per-source checks.
    for source in config.sources:
        prefix = f"source {source.name!r}"
        checks: list[tuple[str, str]] = []

        def source_ok(message: str) -> None:
            checks.append(("ok", message))

        def source_warn(message: str) -> None:
            checks.append(("warn", message))

        def source_fail(message: str) -> None:
            checks.append(("fail", message))

        # output_dir.
        if not source.output_dir.exists():
            source_warn(f"{prefix}: output_dir {source.output_dir} does not exist (will be created on run)")
        elif not os.access(source.output_dir, os.W_OK):
            source_fail(f"{prefix}: output_dir {source.output_dir} exists but is not writable")
        else:
            source_ok(f"{prefix}: output_dir {source.output_dir} is writable")

        # archive_file parent.
        archive_parent = source.archive_file.parent
        if not os.access(archive_parent, os.W_OK):
            source_fail(f"{prefix}: archive_file parent {archive_parent} is not writable")
        else:
            source_ok(f"{prefix}: archive_file parent is writable")

        # archive line count (informational).
        archive_summary = "archive not created"
        if source.archive_file.exists():
            try:
                line_count = sum(1 for _ in source.archive_file.open("r", encoding="utf-8", errors="replace"))
                archive_summary = f"archive {line_count} entries"
            except OSError:
                source_warn(f"{prefix}: could not read archive file {source.archive_file}")

        # Profile.
        profile_path = paths.profile_file(paths.config_dir(), source.profile)
        source_ok(f"{prefix}: profile {source.profile!r} → {profile_path}")

        # Cookies (ytdlp sources).
        if source.cookies:
            _check_cookies(source.cookies, prefix, source_ok, source_warn, source_fail)

        # nndownload per-source checks.
        if source.downloader == "nndownload":
            if source.auth:
                source_ok(f"{prefix}: auth present")
            _check_session_cookie(source.auth or "", prefix, source_ok, source_warn, source_fail)

        problems = [(kind, message) for kind, message in checks if kind != "ok"]
        if problems:
            for kind, message in problems:
                (warn if kind == "warn" else fail)(message)
            continue

        auth_summary = ""
        auth_text = source.cookies or source.auth or ""
        try:
            auth_parts = shlex.split(auth_text)
        except ValueError:
            auth_parts = []
        if "--cookies-from-browser" in auth_parts:
            index = auth_parts.index("--cookies-from-browser")
            auth_summary = f" {ui.DOT} cookies from {auth_parts[index + 1]}"
        elif "--cookies" in auth_parts:
            auth_summary = f" {ui.DOT} cookies file"
        elif "--session-cookie" in auth_parts:
            auth_summary = f" {ui.DOT} session cookie"
        trailing = ui.dim(f"— output dir writable {ui.DOT} {archive_summary}{auth_summary}")
        ok(f"{ui.bold(source.name)} {trailing}")

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
                warn(f"{source.name} — appears to be running right now")
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
                warn(f"kura.timer {status} — scheduling not enabled (see README)")
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
    print(ui.red(f"{ui.FAIL} {error}"))
    return 1


# ---------------------------------------------------------------------------
# kura prune
# ---------------------------------------------------------------------------


def _video_files(directory: Path, video_id: str) -> tuple[list[Path], str | None]:
    """Return the files on disk belonging to *video_id* inside *directory*.

    Resolution is sidecar-first: locate the ``*.info.json`` (yt-dlp) or ``*.json``
    (nndownload) whose parsed id matches, then take every sibling sharing its
    stem.  Falls back to an ``[<video_id>]`` filename match for layouts whose
    sidecars are missing.  Returns ``(files, stem)``.
    """
    from .library import _parse_nndownload_sidecar, _parse_ytdlp_sidecar

    if not directory.is_dir():
        return [], None

    stem: str | None = None
    for sidecar in sorted(directory.iterdir()):
        if not sidecar.is_file():
            continue
        if sidecar.name.endswith(".info.json"):
            parsed = _parse_ytdlp_sidecar(sidecar)
            candidate = sidecar.name[: -len(".info.json")]
        elif sidecar.suffix == ".json":
            parsed = _parse_nndownload_sidecar(sidecar)
            candidate = sidecar.stem
        else:
            continue
        if parsed and parsed[1] == video_id:
            stem = candidate
            break

    if stem is not None:
        files = [p for p in directory.iterdir() if p.is_file() and p.name.startswith(stem + ".")]
    else:
        # No usable sidecar — fall back to the id embedded in the filename.
        files = [p for p in directory.iterdir() if p.is_file() and f"[{video_id}]" in p.name]
    return files, stem


def prune_command(args) -> int:
    """Delete a source's video files while guaranteeing their IDs stay archived.

    This is the safe counterpart to hand-editing ``archive.txt``.  The archive is
    written *before* anything is unlinked, so an interruption can only ever leave
    videos over-suppressed — never re-downloadable.  Removing an ID from the
    archive is what causes a re-download; this command never removes one.
    """
    from .library import fmt_size, query_videos, refresh_index

    source_name: str = args.name
    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source = next((s for s in config.sources if s.name == source_name), None)
    if source is None:
        known = ", ".join(s.name for s in config.sources)
        print(ui.red(f"{ui.FAIL} unknown source {source_name!r}" + (f"; known: {known}" if known else "")))
        return 1

    ids: list[str] = list(getattr(args, "ids", None) or [])
    terms: list[str] = list(getattr(args, "query", None) or [])
    if not ids and not terms and not args.since and not args.until:
        print(ui.red(f"{ui.FAIL} refusing to prune everything; pass search terms, --id, --since or --until"))
        return 1

    db_path = paths.state_db()
    state.init_db(db_path)

    with source_lock(source_name, paths.state_dir()) as acquired:
        if not acquired:
            print(ui.red(f"{ui.FAIL} {source_name} is currently running; cannot prune while the lock is held"))
            return 1

        refresh_index(source, db_path=db_path)
        rows = query_videos(
            db_path,
            [source_name],
            terms=terms,
            since=args.since,
            until=args.until,
            uploaded=getattr(args, "uploaded", False),
        )
        if ids:
            wanted = set(ids)
            rows = [r for r in rows if r["video_id"] in wanted]

        # Only rows that still have files on disk are prunable.
        targets: list[tuple[dict, list[Path]]] = []
        for row in rows:
            directory = Path(row["dir"]) if row.get("dir") else None
            if directory is None:
                continue
            files, _ = _video_files(directory, row["video_id"])
            if files:
                targets.append((row, files))

        if not targets:
            print(ui.dim("nothing to prune"))
            return 0

        total = sum(f.stat().st_size for _, files in targets for f in files if f.exists())
        print(f"{len(targets)} video(s), {fmt_size(total)}:")
        for row, files in targets:
            print(f"  {ui.dim(row['video_id'])}  {row['title'] or '(untitled)'}")

        if getattr(args, "dry_run", False):
            print(ui.dim("dry run; nothing deleted"))
            return 0

        if not getattr(args, "yes", False):
            try:
                reply = input("delete these files? their IDs stay archived [y/N] ").strip().lower()
            except EOFError:
                reply = ""
            if reply not in ("y", "yes"):
                print(ui.dim("aborted"))
                return 1

        # 1. Archive first — this is the step that must not be skipped or reordered.
        existing = read_archive(source.archive_file)
        missing = [row["archive_key"] for row, _ in targets if row["archive_key"] not in existing]
        if missing:
            append_archive_entries(source.archive_file, missing)
        verify = read_archive(source.archive_file)
        unprotected = [row["archive_key"] for row, _ in targets if row["archive_key"] not in verify]
        if unprotected:
            print(ui.red(f"{ui.FAIL} archive write did not take effect for {len(unprotected)} entries; nothing deleted"))
            return 1

        # 2. Only now touch the filesystem.
        freed = 0
        removed = 0
        suppressed_at = datetime.now(tz=UTC).isoformat()
        for row, files in targets:
            for path in files:
                try:
                    freed += path.stat().st_size
                    path.unlink()
                except OSError as error:
                    print(ui.yellow(f"! could not delete {path}: {error}"))
            directory = Path(row["dir"])
            if directory != source.output_dir and directory.is_dir() and not any(directory.iterdir()):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            removed += 1

        with state.connect(db_path) as db:
            for row, _ in targets:
                db.execute(
                    """
                    INSERT INTO suppressions (source, archive_key, suppressed_at, reason)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, archive_key) DO UPDATE SET
                        suppressed_at = excluded.suppressed_at,
                        reason        = excluded.reason
                    """,
                    (source_name, row["archive_key"], suppressed_at, getattr(args, "reason", None) or "pruned"),
                )

        refresh_index(source, full=True, db_path=db_path)

    print(ui.green(f"{ui.OK} pruned {removed} video(s), freed {fmt_size(freed)}; "
                   f"{len(missing)} new archive entr{'y' if len(missing) == 1 else 'ies'} added"))
    return 0
