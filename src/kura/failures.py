"""Failure classification for yt-dlp and nndownload output lines.

Provides:
- :func:`classify_line` — yt-dlp ERROR line parser.
- :func:`parse_result_line` — nndownload ``[result] ...`` line parser.
- :func:`classify_nndownload_failure` — map nndownload error text to failure kind.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class Classification(NamedTuple):
    video_id: str | None
    kind: str | None  # 'permanent' | 'transient' | None


# Pattern for the standard yt-dlp error prefix: "ERROR: [youtube] VIDEOID: message"
_ERROR_PREFIX = re.compile(
    r"ERROR:\s+\[(?:youtube|YoutubeTab|youtube:tab)\]\s+([A-Za-z0-9_-]{3,32}):\s+(.*)",
    re.IGNORECASE,
)

# Table of (compiled_pattern, kind) matched against the *message* part of the line.
# Order matters — first match wins.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Permanent errors
    (re.compile(r"members[- ]only", re.IGNORECASE), "permanent"),
    (re.compile(r"join this channel", re.IGNORECASE), "permanent"),
    (re.compile(r"private video", re.IGNORECASE), "permanent"),
    (re.compile(r"video unavailable", re.IGNORECASE), "permanent"),
    (re.compile(r"account .* terminated", re.IGNORECASE), "permanent"),
    (re.compile(r"this video has been removed", re.IGNORECASE), "permanent"),
    (re.compile(r"sign in to confirm your age", re.IGNORECASE), "permanent"),
    # Transient errors
    (re.compile(r"HTTP Error 403", re.IGNORECASE), "transient"),
    (re.compile(r"HTTP Error 429", re.IGNORECASE), "transient"),
    (re.compile(r"connection reset", re.IGNORECASE), "transient"),
    (re.compile(r"timed? ?out", re.IGNORECASE), "transient"),
    (re.compile(r"temporary failure", re.IGNORECASE), "transient"),
    (re.compile(r"fragment \S+ not found", re.IGNORECASE), "transient"),
    (re.compile(r"sign in to confirm you.?re not a bot", re.IGNORECASE), "transient"),
]


def classify_line(line: str) -> Classification:
    """Classify a single yt-dlp output line.

    Returns a :class:`Classification` with ``video_id`` extracted when the line
    matches the standard ``ERROR: [youtube] VIDEOID:`` prefix, and ``kind`` set
    to ``'permanent'`` or ``'transient'`` when a pattern matches.  Both fields
    are *None* when the line does not match any classification pattern.
    """
    # Try to extract the video id from the standard ERROR prefix.
    video_id: str | None = None
    message: str = line

    m = _ERROR_PREFIX.match(line.strip())
    if m:
        video_id = m.group(1)
        message = m.group(2)

    # Check all patterns against the full line (not just the extracted message)
    # so we catch error text that appears in lines without the standard prefix.
    full_text = line

    for pattern, kind in _PATTERNS:
        if pattern.search(full_text):
            return Classification(video_id=video_id, kind=kind)

    return Classification(video_id=None, kind=None)


# ---------------------------------------------------------------------------
# nndownload result-line support
# ---------------------------------------------------------------------------

# Pattern for nndownload structured result lines:
#   [result] ok <id>
#   [result] skip <id> archived|exists
#   [result] fail <id> ExceptionClass: message
_RESULT_LINE = re.compile(
    r"^\[result\]\s+(ok|skip|fail)\s+(\S+)(?:\s+(.*))?$"
)

# nndownload failure patterns → kind.
# Permanent: deleted/private video, premium-only format, region block.
# Transient: network errors, site-change breakage, auth/session issues.
NNDOWNLOAD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Permanent
    (re.compile(r"deleted|removed|not found", re.IGNORECASE), "permanent"),
    (re.compile(r"private", re.IGNORECASE), "permanent"),
    (re.compile(r"FormatNotAvailableException.*(?:premium|paid)", re.IGNORECASE), "permanent"),
    (re.compile(r"FormatNotAvailableException.*economy", re.IGNORECASE), "permanent"),
    (re.compile(r"region", re.IGNORECASE), "permanent"),
    # Transient
    (re.compile(r"HTTP [45]\d\d", re.IGNORECASE), "transient"),
    (re.compile(r"timed? ?out", re.IGNORECASE), "transient"),
    (re.compile(r"ParameterExtractionException", re.IGNORECASE), "transient"),
    (re.compile(r"authentication|session.?invalid|session.?expired|login", re.IGNORECASE), "transient"),
    (re.compile(r"connection reset|connection error", re.IGNORECASE), "transient"),
]


def parse_result_line(line: str) -> tuple[str, str, str | None] | None:
    """Parse an nndownload ``[result] ...`` line.

    Returns ``(status, video_id, detail)`` where *status* is one of
    ``'ok'``, ``'skip'``, ``'fail'``, *video_id* is the video identifier,
    and *detail* is the rest of the line (or *None*).

    Returns *None* for lines that don't match the pattern.
    """
    m = _RESULT_LINE.match(line.strip())
    if m is None:
        return None
    status = m.group(1)
    video_id = m.group(2)
    detail = m.group(3) or None
    return status, video_id, detail


def classify_nndownload_failure(detail: str | None) -> str:
    """Classify an nndownload failure detail string as ``'permanent'`` or ``'transient'``.

    Returns ``'transient'`` by default when no pattern matches.
    """
    if detail is None:
        return "transient"
    for pattern, kind in NNDOWNLOAD_PATTERNS:
        if pattern.search(detail):
            return kind
    return "transient"
