from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import archive, paths, state
from .config import Source


def build_command(
    source: Source,
    profile_path: str | Path,
    *,
    simulate: bool = False,
) -> list[str]:
    """Build the yt-dlp argv list for *source*.

    Parameters
    ----------
    source:
        The source configuration object.
    profile_path:
        Absolute path to the yt-dlp ``.conf`` profile file.
    simulate:
        When *True* append ``--simulate --skip-download --print <template>``
        so yt-dlp reports what it *would* download without writing anything.
    """
    cmd: list[str] = [
        "yt-dlp",
        "--ignore-config",
        "--config-locations", str(profile_path),
        "--download-archive", str(source.archive_file),
    ]

    if source.cookies:
        cmd.extend(shlex.split(source.cookies))

    if source.extra_args:
        if isinstance(source.extra_args, str):
            cmd.extend(shlex.split(source.extra_args))
        else:
            cmd.extend(source.extra_args)

    if simulate:
        cmd += [
            "--simulate",
            "--skip-download",
            "--print", "pre_process:%(extractor_key)s %(id)s %(title)s",
        ]

    cmd.append(source.url)
    return cmd


def run_source(source: Source, config_dir: Path, state_dir: Path) -> int:
    """Execute yt-dlp for *source*, stream output, and persist run state.

    Returns the raw yt-dlp exit code.  The CLI caller is expected to do
    ``sys.exit(min(1, exit_code))`` so it never propagates codes > 1.
    """
    # 1. Ensure directories exist.
    source.output_dir.mkdir(parents=True, exist_ok=True)
    log_source_dir = paths.logs_dir(state_dir) / source.name
    log_source_dir.mkdir(parents=True, exist_ok=True)

    # 2. Snapshot archive.
    old_keys = archive.read_archive(source.archive_file)

    # 3. Capture yt-dlp version.
    version_result = subprocess.run(
        ["yt-dlp", "--version"],
        capture_output=True,
        text=True,
    )
    ytdlp_version = version_result.stdout.strip()

    # 4. Build log path.
    started_at = datetime.now(tz=timezone.utc)
    timestamp_compact = started_at.strftime("%Y%m%dT%H%M%SZ")
    log_path = log_source_dir / f"{timestamp_compact}.log"

    # 5. Build command.
    profile_path = paths.profile_file(config_dir, source.profile)
    cmd = build_command(source, profile_path)

    # 6. Open log file and write header.
    with log_path.open("w", encoding="utf-8") as log_file:
        header = shlex.join(cmd) + f"  started_at={started_at.isoformat()}"
        log_file.write(header + "\n")
        log_file.flush()

        # 7. Run subprocess and stream output.
        proc = subprocess.Popen(
            cmd,
            cwd=source.output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log_file.write(line)
            log_file.flush()
            print(line, end="", flush=True)
        proc.wait()

    exit_code = proc.returncode

    # 8. Diff archive to find newly downloaded items.
    new_keys = archive.read_archive(source.archive_file) - old_keys
    new_count = len(new_keys)

    # 9-10. Open DB, init schema, insert runs row.
    ended_at = datetime.now(tz=timezone.utc)
    db_path = paths.state_db(state_dir)
    state.init_db(db_path)
    db = state.connect(db_path)
    with db:
        db.execute(
            """
            INSERT INTO runs
                (source, started_at, ended_at, exit_code, ytdlp_version,
                 new_count, log_path, failure_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.name,
                started_at.isoformat(),
                ended_at.isoformat(),
                exit_code,
                ytdlp_version,
                new_count,
                str(log_path),
                None,
            ),
        )

        # 11. Upsert source_state.
        if exit_code in (0, 101):
            db.execute(
                """
                INSERT INTO source_state
                    (source, last_check_at, last_success_at, last_full_run_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_check_at   = excluded.last_check_at,
                    last_success_at = excluded.last_success_at,
                    last_full_run_at = excluded.last_full_run_at
                """,
                (
                    source.name,
                    ended_at.isoformat(),
                    ended_at.isoformat(),
                    ended_at.isoformat(),
                ),
            )
        else:
            db.execute(
                """
                INSERT INTO source_state (source, last_check_at)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_check_at = excluded.last_check_at
                """,
                (source.name, ended_at.isoformat()),
            )

    db.close()

    # 12. Print summary.
    print(f"exit={exit_code}  new={new_count}  log={log_path}")

    # 13. Return actual exit code (caller does sys.exit(min(1, exit_code))).
    return exit_code
