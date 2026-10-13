"""Media integrity checks for freshly downloaded files.

A failing disk can return success for a write that never landed: the file has
the right size but contains zero-filled ranges, or the bitstream is garbled.
yt-dlp cannot see this, so it records the video in the download archive and
the corrupt copy is never fetched again.  Separately, yt-dlp reuses existing
format fragments (``.f399.mp4``) and merged outputs across runs, so one bad
fragment makes every later run fail the same merge.

Provides:
- :func:`check_media` — return a problem description for a corrupt file.
- :func:`media_files_for_key` — locate the final media files of an archive key.
- :func:`postprocess_targets` — merge/postprocess targets that hit an error.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .archive import MEDIA_EXTENSIONS

# Compressed media never contains a whole aligned MiB of zero bytes; a write
# the disk silently dropped reads back exactly like that.
_BLOCK = 1 << 20
_ZERO_BLOCK = bytes(_BLOCK)

# Generous: remuxing is I/O bound, but a multi-GB file on a slow disk takes a while.
_FFMPEG_TIMEOUT_S = 1800


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _zero_block_offset(path: Path) -> int | None:
    """Return the MiB offset of the first all-zero aligned block, or None."""
    with path.open("rb") as f:
        index = 0
        while block := f.read(_BLOCK):
            if block == _ZERO_BLOCK[: len(block)]:
                return index
            index += 1
    return None


def check_media(path: Path) -> str | None:
    """Return a short description of what is wrong with *path*, or None if it is sound.

    Three checks, cheapest first: an all-zero MiB block, ffprobe finding no
    streams, and errors while stream-copying the whole file into a null
    matroska sink (the same demux/mux path yt-dlp's merger takes).  The
    ffmpeg checks are skipped when ffmpeg/ffprobe are not installed, and a
    check that times out proves nothing, so it counts as sound.
    """
    try:
        return _check_media(path)
    except subprocess.TimeoutExpired:
        return None


def _check_media(path: Path) -> str | None:
    try:
        offset = _zero_block_offset(path)
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    if offset is not None:
        return f"zero-filled block at {offset} MiB"

    if _have("ffprobe"):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-print_format", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT_S,
            check=False,
        )
        try:
            streams = json.loads(probe.stdout or "{}").get("streams")
        except json.JSONDecodeError:
            streams = None
        if not streams:
            return "ffprobe found no streams"

    if _have("ffmpeg"):
        remux = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
             "-map", "0", "-c", "copy", "-f", "matroska", "-y", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT_S,
            check=False,
        )
        errors = [line.strip() for line in remux.stderr.splitlines() if line.strip()]
        if errors:
            return f"remux error: {errors[0][:160]}"
        if remux.returncode != 0:
            return f"remux failed (exit {remux.returncode})"

    return None


def _is_final_media(path: Path) -> bool:
    """True for a finished media file — not a ``.part``/``.temp``/``.fNNN`` fragment."""
    if path.suffix.lstrip(".").lower() not in MEDIA_EXTENSIONS:
        return False
    inner = Path(path.stem).suffix
    return inner != ".temp" and not re.fullmatch(r"\.f[\w-]+", inner)


def media_files_for_key(output_dir: Path, key: str) -> list[Path]:
    """Return the finished media files on disk for archive *key* (``"<extractor> <id>"``).

    Matches yt-dlp's ``[<id>]`` filename convention anywhere under *output_dir*.
    Returns an empty list when nothing matches (custom output templates,
    audio-only formats deleted by a postprocessor, …) — absence is not corruption.
    """
    parts = key.split()
    if len(parts) != 2 or not output_dir.is_dir():
        return []
    pattern = f"*[[]{glob_escape(parts[1])}[]]*"
    return sorted(p for p in output_dir.rglob(pattern) if p.is_file() and _is_final_media(p))


def glob_escape(text: str) -> str:
    """Escape glob metacharacters in *text* (``[`` ``]`` ``*`` ``?``)."""
    return re.sub(r"([\[\]*?])", r"[\1]", text)


# "[Merger] Merging formats into "<path>""
_MERGER = re.compile(r'^\[Merger\] Merging formats into "(.+)"\s*$')
# "[download] <path> has already been downloaded"
_ALREADY = re.compile(r"^\[download\] (.+) has already been downloaded\s*$")
# Postprocessing errors with no video id: a failed ffmpeg merge, and yt-dlp's
# KeyError when ffprobe returns "{}" for an unreadable file.
_PP_ERROR = re.compile(r"^ERROR: (?:Conversion failed!|'streams')\s*$")
# Any line that starts work on the next video resets the current target.
_NEXT_ITEM = re.compile(r"^\[download\] Downloading item |^\[\w+(?::\w+)?\] Extracting URL")


def postprocess_targets(output_lines: list[str]) -> list[str]:
    """Return the media paths (as printed by yt-dlp) whose postprocessing failed.

    A path comes from the most recent ``[Merger]`` or "has already been
    downloaded" line for the current video, and is reported when a merge or
    ffprobe error follows before the next video starts.
    """
    targets: list[str] = []
    current: str | None = None
    for raw in output_lines:
        line = raw.rstrip("\n")
        if _NEXT_ITEM.match(line):
            current = None
            continue
        m = _MERGER.match(line) or _ALREADY.match(line)
        if m:
            current = m.group(1)
            continue
        if current is not None and _PP_ERROR.match(line) and current not in targets:
            targets.append(current)
    return targets


def fragment_files(target: Path) -> list[Path]:
    """Return the format fragments and temp files yt-dlp left next to *target*."""
    stem = target.stem
    if not target.parent.is_dir():
        return []
    found = []
    for p in target.parent.iterdir():
        if not p.is_file() or not p.name.startswith(stem + "."):
            continue
        inner = p.name[len(stem):]
        if re.match(r"^\.f[\w-]+\.\w+$", inner) or re.match(r"^\.temp\.\w+$", inner):
            found.append(p)
    return sorted(found)
