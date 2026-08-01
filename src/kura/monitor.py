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


def parse_progress(lines: list[str]) -> dict[str, Any]:
    """Extract the latest useful yt-dlp progress signals from log lines."""
    pct: float | None = None
    speed: str | None = None
    eta: str | None = None
    size: str | None = None
    title: str | None = None

    for line in reversed(lines):
        destination = _DESTINATION_RE.search(line)
        if destination:
            title = Path(destination.group(1).strip().strip("\"'")).parent.name or None
            break
    if title is None:
        for line in reversed(lines):
            video_id = _VIDEO_ID_RE.search(line)
            if video_id:
                title = video_id.group(1).strip()
                break

    for line in reversed(lines):
        progress = _PROGRESS_RE.search(line)
        if progress:
            pct = float(progress.group("pct"))
            size = progress.group("size")
            speed = progress.group("speed")
            eta = progress.group("eta")
            break

    phase = "working"
    for line in reversed(lines):
        lower = line.lower()
        if (
            "[youtube:tab]" in lower
            and (
                "downloading api json" in lower
                or re.search(r"\bpage\s+\d+\b", lower)
                or "playlist" in lower
            )
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
        if "[metadata]" in lower or "writing video metadata" in lower or ("writing " in lower and " to:" in lower):
            phase = "metadata"
            break
        if "deleting original file" in lower:
            phase = "processing"
            break
        if _PROGRESS_RE.search(line):
            phase = "downloading"
            break
        if "has already been recorded" in lower:
            phase = "skipping"
            break

    # Only an active download shows the progress bar. During any post-download
    # phase (remux/merge/embed/comments/…), the last progress line is a stale
    # 100% — clear it so the view shows "… <phase>" rather than a frozen bar.
    if phase != "downloading":
        pct = speed = eta = size = None

    return {
        "pct": pct,
        "speed": speed,
        "eta": eta,
        "size": size,
        "title": title,
        "phase": phase,
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
        if snapshot.pct is None:
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
