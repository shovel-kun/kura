from __future__ import annotations

import shlex
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import archive, notify, paths, state
from .config import Source, load_nnconf, parse_duration
from .failures import classify_line, classify_nndownload_failure, parse_result_line
from .locks import source_lock


def build_command(
    source: Source,
    profile_path: str | Path,
    *,
    simulate: bool = False,
    strategy_args: list[str] | None = None,
    urls: list[str] | None = None,
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
    strategy_args:
        Extra flags inserted after ``extra_args`` and before the URL(s).
        Used by the *quick* strategy (``--lazy-playlist``, etc.).
    urls:
        Override the URL list; defaults to ``[source.url]``.
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

    if strategy_args:
        cmd.extend(strategy_args)

    if urls is not None:
        cmd.extend(urls)
    else:
        cmd.append(source.url)

    return cmd


def build_nndownload_command(source: Source, profile_args: list[str]) -> list[str]:
    """Build the nndownload argv list for *source*.

    Parameters
    ----------
    source:
        The source configuration object (must have ``downloader == 'nndownload'``).
    profile_args:
        Pre-parsed argv list from the ``.nnconf`` profile file.
    """
    cmd: list[str] = [
        "nndownload",
        "--non-interactive",
        "--download-archive", str(source.archive_file),
    ]

    if source.auth:
        cmd.extend(shlex.split(source.auth))

    cmd.extend(profile_args)

    if source.update_strategy == "quick":
        cmd.append("--break-on-existing")

    if source.extra_args:
        if isinstance(source.extra_args, str):
            cmd.extend(shlex.split(source.extra_args))
        else:
            cmd.extend(source.extra_args)

    cmd.append(source.url)
    return cmd


# ---------------------------------------------------------------------------
# Quick strategy helpers
# ---------------------------------------------------------------------------

_QUICK_FLAGS = ["--lazy-playlist", "--break-on-existing", "--break-per-input"]


def _channel_tab_urls(channel_url: str) -> list[str]:
    """Derive the /videos, /shorts, /streams tab URLs from *channel_url*."""
    base = channel_url.rstrip("/")
    return [
        f"{base}/videos",
        f"{base}/shorts",
        f"{base}/streams",
    ]


# ---------------------------------------------------------------------------
# RSS strategy helpers
# ---------------------------------------------------------------------------

_RSS_FEED_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def _fetch_rss_video_ids(channel_id: str, *, timeout: int = 30) -> list[str]:
    """Fetch the RSS feed for *channel_id* and return video IDs."""
    url = _RSS_FEED_TEMPLATE.format(channel_id)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        xml_bytes = resp.read()
    root = ET.fromstring(xml_bytes)  # noqa: S314
    ns = {"yt": "http://www.youtube.com/xml/schemas/2015"}
    ids: list[str] = []
    for elem in root.iter("{http://www.youtube.com/xml/schemas/2015}videoId"):
        vid = elem.text
        if vid:
            ids.append(vid.strip())
    return ids


def _resolve_channel_id(
    source: Source,
    db: object,  # sqlite3.Connection
) -> str | None:
    """Return the channel_id for *source*, resolving and caching if needed.

    Returns *None* on failure (caller should fall through to quick).
    """
    import sqlite3

    # Check cache first.
    row = db.execute(  # type: ignore[union-attr]
        "SELECT channel_id FROM channel_cache WHERE source = ?",
        (source.name,),
    ).fetchone()
    if row:
        return row[0]

    # Resolve via yt-dlp.
    cookies_args: list[str] = shlex.split(source.cookies) if source.cookies else []
    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--print", "%(channel_id)s",
        "--playlist-items", "1",
        *cookies_args,
        source.url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        channel_id = result.stdout.strip()
        if not channel_id or not channel_id.startswith("UC"):
            return None
    except Exception:
        return None

    # Cache it.
    resolved_at = datetime.now(tz=timezone.utc).isoformat()
    db.execute(  # type: ignore[union-attr]
        """
        INSERT INTO channel_cache (source, channel_id, resolved_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            channel_id = excluded.channel_id,
            resolved_at = excluded.resolved_at
        """,
        (source.name, channel_id, resolved_at),
    )
    return channel_id


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------


def _is_promotion_due(last_full_run_at: str | None, interval_secs: int) -> bool:
    """Return True if a full reindex is overdue."""
    if last_full_run_at is None:
        return True
    try:
        last = datetime.fromisoformat(last_full_run_at)
    except ValueError:
        return True
    now = datetime.now(tz=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= interval_secs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_source(
    source: Source,
    config_dir: Path,
    state_dir: Path,
    *,
    quiet: bool = False,
    notify: bool = True,
) -> int | None:
    """Execute yt-dlp for *source*, stream output, and persist run state.

    Returns the raw yt-dlp exit code, or *None* if the source lock was
    already held by another process (concurrent run).  The CLI caller maps
    *None* to exit 0 (overlapping timer tick is normal, not an error).

    Parameters
    ----------
    quiet:
        When *True*, suppress per-line console streaming (log file still
        receives all output).  Only the start and end summary lines are
        printed.  Used by concurrent multi-source runs to avoid interleaved
        output.
    notify:
        When *True* (the default), send a best-effort desktop notification
        summarizing the run outcome (resolved from ``defaults.notify`` by
        the CLI caller).  Set *False* to suppress notifications entirely.
    """
    with source_lock(source.name, state_dir) as acquired:
        if not acquired:
            print(f"skipped: {source.name} is already running")
            return None
        return _run_source_locked(source, config_dir, state_dir, quiet=quiet, notify=notify)


def _run_source_locked(
    source: Source,
    config_dir: Path,
    state_dir: Path,
    *,
    quiet: bool = False,
    notify: bool = True,
) -> int:
    """Inner implementation of run_source; called with the lock already held."""
    # 1. Ensure directories exist.
    source.output_dir.mkdir(parents=True, exist_ok=True)
    log_source_dir = paths.logs_dir(state_dir) / source.name
    log_source_dir.mkdir(parents=True, exist_ok=True)

    # 2. Open DB early (needed for channel_id resolution and source_state).
    db_path = paths.state_db(state_dir)
    state.init_db(db_path)
    db = state.connect(db_path)

    # 3. Determine effective strategy (with full_reindex promotion).
    effective_strategy = source.update_strategy
    interval_secs = parse_duration(source.full_reindex_every)

    if source.update_strategy in ("quick", "rss"):
        # Check if promotion to full is due.
        ss_row = db.execute(
            "SELECT last_full_run_at FROM source_state WHERE source = ?",
            (source.name,),
        ).fetchone()
        last_full_run_at = ss_row[0] if ss_row else None

        if _is_promotion_due(last_full_run_at, interval_secs):
            print("promoted to full reindex")
            effective_strategy = "full"

    # 4. Handle rss strategy (before invoking yt-dlp).
    if effective_strategy == "rss":
        rss_result = _try_rss_check(source, db, state_dir, log_source_dir)
        if rss_result is not None:
            db.close()
            return rss_result
        # Fall through: either error or new videos → run quick.
        effective_strategy = "quick"

    # 5. Snapshot archive.
    old_keys = archive.read_archive(source.archive_file)

    # 6. Capture downloader version.
    if source.downloader == "nndownload":
        version_result = subprocess.run(
            ["nndownload", "--version"],
            capture_output=True,
            text=True,
        )
        downloader_version = f"nndownload {version_result.stdout.strip()}"
    else:
        version_result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True,
            text=True,
        )
        downloader_version = version_result.stdout.strip()

    # 7. Build log path.
    started_at = datetime.now(tz=timezone.utc)
    timestamp_compact = started_at.strftime("%Y%m%dT%H%M%SZ")
    log_path = log_source_dir / f"{timestamp_compact}.log"

    # 8. Build command based on downloader and effective strategy.
    if source.downloader == "nndownload":
        profile_path = paths.profile_file(config_dir, source.profile, ext="nnconf")
        profile_args = load_nnconf(profile_path)
        # For promoted full runs, effective_strategy differs from source.update_strategy.
        # build_nndownload_command reads update_strategy to decide --break-on-existing.
        if effective_strategy != source.update_strategy:
            import dataclasses
            source_for_cmd = dataclasses.replace(source, update_strategy=effective_strategy)
        else:
            source_for_cmd = source
        cmd = build_nndownload_command(source_for_cmd, profile_args)
    else:
        profile_path = paths.profile_file(config_dir, source.profile)
        if effective_strategy == "quick":
            tab_urls = _channel_tab_urls(source.url)
            cmd = build_command(source, profile_path, strategy_args=_QUICK_FLAGS, urls=tab_urls)
        else:
            # full
            cmd = build_command(source, profile_path)

    # 9. Open log file and write header.
    output_lines: list[str] = []
    if quiet:
        print(f"{source.name}: started")
    with log_path.open("w", encoding="utf-8") as log_file:
        header = shlex.join(cmd) + f"  started_at={started_at.isoformat()}"
        log_file.write(header + "\n")
        log_file.flush()

        # 10. Run subprocess and stream output.
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
            if not quiet:
                print(line, end="", flush=True)
            output_lines.append(line)
        proc.wait()

    exit_code = proc.returncode

    # 11. Diff archive to find newly downloaded items.
    new_keys = archive.read_archive(source.archive_file) - old_keys
    new_count = len(new_keys)

    # 12. Classify failures from output lines.
    if source.downloader == "nndownload":
        failure_summary = _collect_nndownload_failures(source.name, output_lines, db)
    else:
        failure_summary = _collect_failures(source.name, output_lines, db)

    # 12b. Send a best-effort desktop notification summarizing the outcome.
    if notify:
        _notify_run_outcome(source.name, exit_code, new_count, failure_summary)

    # 13. Insert runs row.
    ended_at = datetime.now(tz=timezone.utc)
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
                downloader_version,
                new_count,
                str(log_path),
                failure_summary,
            ),
        )

        # 14. Upsert source_state.
        if exit_code in (0, 101):
            if effective_strategy == "full":
                db.execute(
                    """
                    INSERT INTO source_state
                        (source, last_check_at, last_success_at, last_full_run_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        last_check_at    = excluded.last_check_at,
                        last_success_at  = excluded.last_success_at,
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
                # quick run: update check/success but NOT last_full_run_at
                db.execute(
                    """
                    INSERT INTO source_state
                        (source, last_check_at, last_success_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        last_check_at   = excluded.last_check_at,
                        last_success_at = excluded.last_success_at
                    """,
                    (
                        source.name,
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

    # 15. Print summary.
    print(f"exit={exit_code}  new={new_count}  log={log_path}")

    # 16. Return actual exit code (caller does sys.exit(min(1, exit_code))).
    return exit_code


# ---------------------------------------------------------------------------
# RSS check helper (returns int exit code if resolved, None to fall through)
# ---------------------------------------------------------------------------


def _try_rss_check(
    source: Source,
    db: object,  # sqlite3.Connection
    state_dir: Path,
    log_source_dir: Path,
) -> int | None:
    """Attempt an RSS-only check.

    Returns an exit code (0) when all feed videos are already archived (no
    yt-dlp download needed).  Returns *None* to signal the caller should fall
    through to the quick strategy.
    """
    # Resolve channel_id.
    try:
        with db:
            channel_id = _resolve_channel_id(source, db)
    except Exception as exc:
        print(f"rss: warning: channel_id resolution failed: {exc}; falling through to quick")
        return None

    if channel_id is None:
        print("rss: warning: could not resolve channel_id; falling through to quick")
        return None

    # Fetch feed.
    try:
        feed_video_ids = _fetch_rss_video_ids(channel_id)
    except Exception as exc:
        print(f"rss: warning: feed fetch failed: {exc}; falling through to quick")
        return None

    # Check which are already archived.
    archive_set = archive.read_archive(source.archive_file)
    missing = [vid for vid in feed_video_ids if f"youtube {vid}" not in archive_set]

    if missing:
        # Some videos are new — fall through to quick run.
        return None

    # All known — record a runs row and update last_check_at.
    print("rss: nothing new")
    ended_at = datetime.now(tz=timezone.utc)

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
                ended_at.isoformat(),
                ended_at.isoformat(),
                0,
                "rss-check",
                0,
                None,
                None,
            ),
        )
        db.execute(
            """
            INSERT INTO source_state (source, last_check_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_check_at = excluded.last_check_at
            """,
            (source.name, ended_at.isoformat()),
        )

    return 0


# ---------------------------------------------------------------------------
# Desktop notification
# ---------------------------------------------------------------------------


def _notify_run_outcome(
    source_name: str,
    exit_code: int,
    new_count: int,
    failure_summary: str | None,
) -> None:
    """Send a best-effort desktop notification summarizing a run outcome.

    - A crashed process (nonzero exit, and not the yt-dlp "no more videos"
      code 101) always raises a critical notification.
    - Otherwise, notify only when something happened: new videos archived,
      or failures were recorded.  A clean "nothing new" check stays silent.
    """
    if exit_code != 0 and exit_code != 101:
        notify.send_notification(
            f"kura: {source_name} run failed",
            f"exit {exit_code} — see kura logs {source_name}",
            critical=True,
        )
        return

    if new_count > 0:
        body = f"{new_count} new video(s) archived"
        if failure_summary:
            body += f", {failure_summary}"
        notify.send_notification(f"kura: {source_name}", body)
        return

    if failure_summary:
        notify.send_notification(f"kura: {source_name}", f"no new videos; {failure_summary}")


# ---------------------------------------------------------------------------
# Failure collection
# ---------------------------------------------------------------------------


def _collect_failures(
    source_name: str,
    output_lines: list[str],
    db: object,  # sqlite3.Connection
) -> str | None:
    """Classify yt-dlp output lines, upsert video_failures rows, return failure_summary."""
    counts: dict[str, int] = {"permanent": 0, "transient": 0}
    now = datetime.now(tz=timezone.utc).isoformat()

    for line in output_lines:
        cls = classify_line(line)
        if cls.kind is None:
            continue
        counts[cls.kind] = counts.get(cls.kind, 0) + 1

        if cls.video_id is not None:
            with db:
                db.execute(
                    """
                    INSERT INTO video_failures
                        (source, video_id, url, kind, attempts, last_seen, last_error)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(source, video_id) DO UPDATE SET
                        attempts   = attempts + 1,
                        last_seen  = excluded.last_seen,
                        last_error = excluded.last_error,
                        kind       = excluded.kind
                    """,
                    (
                        source_name,
                        cls.video_id,
                        None,
                        cls.kind,
                        now,
                        line.strip()[:500],
                    ),
                )

    parts = []
    if counts.get("permanent", 0):
        parts.append(f"{counts['permanent']} permanent")
    if counts.get("transient", 0):
        parts.append(f"{counts['transient']} transient")

    return ", ".join(parts) if parts else None


def _collect_nndownload_failures(
    source_name: str,
    output_lines: list[str],
    db: object,  # sqlite3.Connection
) -> str | None:
    """Parse nndownload [result] lines, upsert video_failures rows, return failure_summary."""
    counts: dict[str, int] = {}
    now = datetime.now(tz=timezone.utc).isoformat()

    for line in output_lines:
        parsed = parse_result_line(line)
        if parsed is None:
            continue
        status, video_id, detail = parsed
        if status != "fail":
            continue

        kind = classify_nndownload_failure(detail)
        counts[kind] = counts.get(kind, 0) + 1

        with db:
            db.execute(
                """
                INSERT INTO video_failures
                    (source, video_id, url, kind, attempts, last_seen, last_error)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(source, video_id) DO UPDATE SET
                    attempts   = attempts + 1,
                    last_seen  = excluded.last_seen,
                    last_error = excluded.last_error,
                    kind       = excluded.kind
                """,
                (
                    source_name,
                    video_id,
                    None,
                    kind,
                    now,
                    line.strip()[:500],
                ),
            )

    parts = []
    if counts.get("permanent", 0):
        parts.append(f"{counts['permanent']} permanent")
    if counts.get("transient", 0):
        parts.append(f"{counts['transient']} transient")

    return ", ".join(parts) if parts else None
