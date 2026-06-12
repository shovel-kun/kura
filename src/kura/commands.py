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
from .archive import MEDIA_EXTENSIONS, append_archive_entries, archive_key, read_archive
from .config import DEFAULT_CONFIG_TEXT, DEFAULT_PROFILE_TEXT, ConfigError, append_source, load_config
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


def run_command(args) -> int:
    """Run yt-dlp for a named source and persist the result."""
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

    exit_code = run_source(source, paths.config_dir(), paths.state_dir())
    return 0 if exit_code == 0 else 1


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


def status_command(_args) -> int:
    """Print a summary table of all sources with last-run state."""
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

    def query_source_state(name: str) -> dict:
        if db is None:
            return {}
        row = db.execute(
            "SELECT last_check_at, imported_at FROM source_state WHERE source = ?",
            (name,),
        ).fetchone()
        if row is None:
            return {}
        return {"last_check_at": row[0], "imported_at": row[1]}

    def query_last_run(name: str) -> dict:
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

    header = ("name", "enabled", "imported", "last_run", "exit", "new", "next_due")
    rows: list[tuple[str, ...]] = [header]

    for source in config.sources:
        ss = query_source_state(source.name)
        lr = query_last_run(source.name)

        enabled_str = "yes" if source.enabled else "no"
        imported_str = "yes" if ss.get("imported_at") else "no"

        last_run_str = "-"
        exit_str = "-"
        new_str = "-"
        if lr.get("ended_at"):
            last_run_str = lr["ended_at"][:16].replace("T", " ")
            exit_str = str(lr["exit_code"])
            new_str = str(lr["new_count"])

        last_check_at = ss.get("last_check_at")
        if last_check_at:
            try:
                base_dt = datetime.fromisoformat(last_check_at)
                cron = croniter(source.schedule, base_dt)
                next_dt = cron.get_next(datetime)
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
            next_due_str,
        ))

    if db is not None:
        db.close()

    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * w for w in widths).rstrip())

    return 0


def stub_command(command: str, note: str):
    def run(_args) -> int:
        print(f"{command}: not implemented yet ({note})")
        return 2

    return run


def handle_config_error(error: ConfigError) -> int:
    print(f"error: {error}")
    return 1
