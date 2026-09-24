from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text

from . import locks, paths
from .config import Config

_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+(?:~\s*)?"
    r"(?P<size>\S+)(?:\s+at\s+(?P<speed>\S+)\s+ETA\s+(?P<eta>\S+)|\s+in\s+\S+)?"
)
_DESTINATION_RE = re.compile(r"\[download\]\s+Destination:\s+(.+)$")
_VIDEO_ID_RE = re.compile(r"(?:\[youtube\]|\[info\])\s+([^:]+):")
_NND_ITEM_RE = re.compile(r"^(?P<index>\d+)/(?P<total>\d+)$")
_NND_DESTINATION_RE = re.compile(
    r'^Downloading\s+\S+\s+to\s+["\'](?P<path>.+)["\']\.\.\.$'
)
# External-downloader runs (--downloader ffmpeg) log ffmpeg's own status lines
# instead of yt-dlp's [download] lines, e.g.:
#   frame=  120 fps=0.0 q=-1.0 size=    1536KiB time=00:00:03.99 bitrate=3146.6kbits/s speed=   8x elapsed=0:00:00.50
_FFMPEG_PROGRESS_RE = re.compile(
    r"\bsize=\s*(?P<size>\S+)\s+time=(?P<time>\d+:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"bitrate=\s*\S+\s+speed=\s*(?P<speed>\S+)"
)
_FFMPEG_DURATION_RE = re.compile(
    r"^\s*Duration:\s*(?P<duration>\d+:\d{2}:\d{2}(?:\.\d+)?)"
)


@dataclass(frozen=True)
class WatchSnapshot:
    name: str
    running: bool
    pct: float | None = None
    speed: str | None = None
    eta: str | None = None
    size: str | None = None
    title: str | None = None
    phase: str = "working"
    archive_delta: int = 0
    item_index: int | None = None
    item_total: int | None = None


def running_sources(config: Config, state_dir: Path) -> list[str]:
    """Return source names whose non-blocking lock probe cannot acquire the lock."""
    running: list[str] = []
    for source in config.sources:
        with locks.source_lock(source.name, state_dir) as acquired:
            if not acquired:
                running.append(source.name)
    return running


def current_log(source_name: str, state_dir: Path) -> Path | None:
    """Return the most recently modified log for *source_name*."""
    source_logs = paths.logs_dir(state_dir) / source_name
    if not source_logs.is_dir():
        return None
    logs = list(source_logs.glob("*.log"))
    if not logs:
        return None
    return max(logs, key=lambda path: path.stat().st_mtime_ns)


def tail_lines(path: Path, n: int = 40) -> list[str]:
    """Read the last *n* lines without decoding failures from partial writes."""
    if n <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            remaining = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while remaining and newline_count <= n:
                chunk_size = min(8192, remaining)
                remaining -= chunk_size
                handle.seek(remaining)
                chunk = handle.read(chunk_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except FileNotFoundError:
        return []

    data = b"".join(reversed(chunks))
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-n:]]


def _parse_clock(value: str) -> float | None:
    """Convert an ffmpeg H:MM:SS(.ff) timestamp into seconds."""
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None


def _format_clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes:02}:{secs:02}"


def _current_video_duration(lines: list[str], dest_index: int | None) -> float | None:
    """Return the Duration ffmpeg logged after the current video's Destination line."""
    if dest_index is None:
        return None
    for line in reversed(lines[dest_index:]):
        found = _FFMPEG_DURATION_RE.search(line)
        if found:
            return _parse_clock(found.group("duration"))
    return None


def _ffmpeg_progress(
    match: re.Match[str], duration: float | None
) -> tuple[float | None, str | None, str | None, str | None]:
    """Derive (pct, speed, eta, size) from an ffmpeg status line.

    ffmpeg reports the stream position, not a percentage, so pair it with the
    current video's duration. The speed column shows ffmpeg's realtime
    multiplier (e.g. "9.9x").
    """
    size = match.group("size") if match.group("size") != "N/A" else None
    speed = match.group("speed") if match.group("speed") != "N/A" else None
    position = _parse_clock(match.group("time"))

    if position is None or not duration:
        return None, speed, None, size

    pct = min(100.0, round(position / duration * 100, 1))
    try:
        multiplier = float((speed or "").rstrip("x"))
    except ValueError:
        multiplier = 0.0
    eta = None
    if multiplier > 0:
        eta = _format_clock(max(0.0, duration - position) / multiplier)
    return pct, speed, eta, size


def watch_window(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Read trailing log lines back to the current video's Destination marker.

    A long download scrolls the `[download] Destination:` line (and, for
    ffmpeg-based downloads, the Duration line) far past any fixed-size tail,
    so read backwards until the marker is included, capped at *max_bytes*.
    """
    marker = b"[download] Destination:"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            remaining = handle.tell()
            chunks: list[bytes] = []
            read = 0
            boundary = b""
            while remaining and read < max_bytes:
                chunk_size = min(65536, remaining)
                remaining -= chunk_size
                handle.seek(remaining)
                chunk = handle.read(chunk_size)
                chunks.append(chunk)
                read += chunk_size
                if marker in chunk + boundary:
                    break
                boundary = chunk[: len(marker) - 1]
    except FileNotFoundError:
        return []

    data = b"".join(reversed(chunks))
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()]


def parse_progress(lines: list[str]) -> dict[str, Any]:
    """Extract the latest useful yt-dlp or nndownload progress signals."""
    pct: float | None = None
    speed: str | None = None
    eta: str | None = None
    size: str | None = None
    title: str | None = None
    item_index: int | None = None
    item_total: int | None = None

    # nndownload emits a stable ``N/TOTAL`` marker before each video. Limit
    # phase/title parsing to that item's lines so a just-started item does not
    # inherit the previous video's comments or metadata phase.
    item_marker_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        item = _NND_ITEM_RE.fullmatch(lines[index].strip())
        if item:
            item_index = int(item.group("index"))
            item_total = int(item.group("total"))
            item_marker_index = index
            break
    current_lines = lines[item_marker_index:] if item_marker_index is not None else lines

    dest_index: int | None = None
    for index in range(len(current_lines) - 1, -1, -1):
        destination = _DESTINATION_RE.search(current_lines[index])
        if destination:
            title = Path(destination.group(1).strip().strip("\"'")).parent.name or None
            dest_index = index
            break
        nnd_destination = _NND_DESTINATION_RE.search(current_lines[index])
        if nnd_destination:
            title = Path(nnd_destination.group("path")).parent.name or None
            dest_index = index
            break
    if title is None:
        for line in reversed(current_lines):
            video_id = _VIDEO_ID_RE.search(line)
            if video_id:
                title = video_id.group(1).strip()
                break

    for line in reversed(current_lines):
        progress = _PROGRESS_RE.search(line)
        if progress:
            pct = float(progress.group("pct"))
            size = progress.group("size")
            speed = progress.group("speed")
            eta = progress.group("eta")
            break
        ffmpeg = _FFMPEG_PROGRESS_RE.search(line)
        if ffmpeg:
            duration = _current_video_duration(current_lines, dest_index)
            pct, speed, eta, size = _ffmpeg_progress(ffmpeg, duration)
            break

    phase = "starting" if item_marker_index is not None else "working"
    for line in reversed(current_lines):
        lower = line.lower()
        if "archiving comments for" in lower:
            phase = "comments"
            break
        if "downloading thumbnail for" in lower:
            phase = "thumbnail"
            break
        if "downloading metadata for" in lower or "adding metadata to" in lower:
            phase = "metadata"
            break
        if "merging audio and video" in lower:
            phase = "merging"
            break
        if _NND_DESTINATION_RE.search(line):
            phase = "downloading"
            break
        if "retrieving video manifest" in lower or "collecting video media uris" in lower:
            phase = "preparing"
            break
        if "[youtube:tab]" in lower and (
            "downloading api json" in lower
            or re.search(r"\bpage\s+\d+\b", lower)
            or "playlist" in lower
        ):
            phase = "enumerating"
            break
        if "downloading comments" in lower or "comment api" in lower:
            phase = "comments"
            break
        # Post-download processing steps have no progress bar; recognise them so
        # the display shows what yt-dlp is doing instead of a frozen 100%.
        if lower.startswith("[fixup") or ("fixing" in lower and "container" in lower):
            phase = "remuxing"
            break
        if "[merger]" in lower or "merging formats" in lower:
            phase = "merging"
            break
        if (
            "[embedthumbnail]" in lower
            or "[thumbnailsconvertor]" in lower
            or "[embedsubtitle]" in lower
        ):
            phase = "embedding"
            break
        if "[sponsorblock]" in lower:
            phase = "sponsorblock"
            break
        if (
            "[metadata]" in lower
            or "writing video metadata" in lower
            or ("writing " in lower and " to:" in lower)
        ):
            phase = "metadata"
            break
        if "deleting original file" in lower:
            phase = "processing"
            break
        if _PROGRESS_RE.search(line) or _FFMPEG_PROGRESS_RE.search(line):
            phase = "downloading"
            break
        if "has already been recorded" in lower or lower.startswith("[result] skip"):
            phase = "skipping"
            break

    # Only an active download shows the progress bar. During any post-download
    # phase (remux/merge/embed/comments/…), the last progress line is a stale
    # 100% — clear it so the view shows the current phase instead.
    if phase != "downloading":
        pct = speed = eta = size = None

    return {
        "pct": pct,
        "speed": speed,
        "eta": eta,
        "size": size,
        "title": title,
        "phase": phase,
        "item_index": item_index,
        "item_total": item_total,
    }


def archive_count(path: Path) -> int:
    """Return the number of entries in an archive file."""
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0


def _truncate_cells(text: str, max_cells: int) -> str:
    """Truncate *text* to *max_cells* terminal cells (CJK chars are 2 cells wide)."""
    if cell_len(text) <= max_cells:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        width = cell_len(ch)
        if used + width > max_cells - 1:
            break
        out.append(ch)
        used += width
    return "".join(out) + "…"


def render_watch(snapshots: list[WatchSnapshot], *, tz: object | None = None) -> Table:
    """Build the watch display from precomputed source snapshots."""
    del tz
    total_new = sum(snapshot.archive_delta for snapshot in snapshots)
    source_count = len(snapshots)
    noun = "source" if source_count == 1 else "sources"
    table = Table(
        title=f"kura · watching {source_count} {noun} · +{total_new} new",
        title_style="bold",
        box=None,
        pad_edge=False,
    )
    table.add_column("source", no_wrap=True)
    table.add_column("progress", no_wrap=True)
    table.add_column("speed", no_wrap=True)
    table.add_column("ETA", no_wrap=True)
    table.add_column("current", overflow="fold")

    for snapshot in snapshots:
        if not snapshot.running:
            source = Text(f"○ {snapshot.name}", style="dim")
            table.add_row(source, Text("— idle", style="dim"), "", "", "")
            continue

        source = Text(f"● {snapshot.name}", style="yellow")
        if snapshot.item_index is not None and snapshot.item_total is not None:
            progress = Text(
                f"{snapshot.item_index}/{snapshot.item_total} · {snapshot.phase}",
                style="yellow",
            )
        elif snapshot.pct is None:
            progress = Text("…  " + snapshot.phase, style="yellow")
        else:
            completed = min(20, round(snapshot.pct / 5))
            bar = "█" * completed + "░" * (20 - completed)
            progress = Text(f"{bar} {snapshot.pct:g}%", style="yellow")
        speed = snapshot.speed or "—"
        eta = snapshot.eta or "—"
        title = _truncate_cells(snapshot.title or snapshot.phase, 40)
        table.add_row(source, progress, speed, eta, Text(title))

    return table
