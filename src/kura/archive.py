from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Media file extensions whose absence of a sibling .info.json marks them as unverifiable.
MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {"mkv", "mp4", "webm", "m4v", "mov", "avi", "flv", "opus", "m4a", "mp3", "ogg"}
)

# Lazy-initialised YoutubeDL instance used only for _make_archive_id.
_ydl_instance = None


def _get_ydl():
    global _ydl_instance
    if _ydl_instance is None:
        import yt_dlp  # noqa: PLC0415

        _ydl_instance = yt_dlp.YoutubeDL({"quiet": True})
    return _ydl_instance


def archive_key(info: dict) -> str | None:
    """Compute the yt-dlp archive key for an info dict.

    Uses yt_dlp.YoutubeDL._make_archive_id when available; falls back to the
    equivalent ``f"{extractor_key.lower()} {id}"`` formula when yt_dlp is not
    importable (e.g. in isolated unit-test environments).

    Returns None when there is no usable id/extractor evidence.
    """
    try:
        return _get_ydl()._make_archive_id(info)
    except ImportError:
        pass

    # Fallback: replicate yt-dlp's make_archive_id(extractor, video_id) logic.
    video_id = info.get("id")
    if not video_id:
        return None
    extractor = info.get("extractor_key") or info.get("ie_key") or info.get("extractor")
    if not extractor:
        return None
    return f"{extractor.lower()} {video_id}"


def read_archive(path: Path) -> set[str]:
    """Return the set of non-empty stripped lines from *path*.

    Returns an empty set when the file does not exist.
    """
    if not path.exists():
        return set()
    lines: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            lines.add(stripped)
    return lines


def append_archive_entries(path: Path, entries: list[str]) -> None:
    """Append *entries* to the archive file at *path* atomically.

    If the file already exists a timestamped backup (``archive.txt.bak.<compact ISO8601>``)
    is written first.  The original file is never modified if anything fails.
    If the file does not exist it is created (no backup is needed in that case).
    """
    if not entries:
        return

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")

    existing_content: str = ""
    if path.exists():
        existing_content = path.read_text(encoding="utf-8")
        # Write timestamped backup before touching the live file.
        backup_path = path.parent / f"{path.name}.bak.{timestamp}"
        backup_path.write_text(existing_content, encoding="utf-8")

    # Build new full content: preserve existing lines, then append new ones.
    new_entries_block = "\n".join(entries) + "\n"
    if existing_content and not existing_content.endswith("\n"):
        new_content = existing_content + "\n" + new_entries_block
    else:
        new_content = existing_content + new_entries_block

    # Atomic write: write to a temp file in the same directory, then os.replace.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(new_content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
