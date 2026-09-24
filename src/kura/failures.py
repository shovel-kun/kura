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


# Sentinel video_id for error lines that carry no per-video identifier
# (e.g. "ERROR: unable to download video data: HTTP Error 403" or channel-tab
# level errors).  Recorded in video_failures so `kura status` and the TUI can
# surface *source-level* failures instead of silently dropping them.
SOURCE_FAILURE_ID = "(source)"



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
    # Errors without a per-video id: mid-download fetch failures and the
    # channel-tab auth check (stale cookies / broken extractor build).
    (re.compile(r"unable to download (?:video data|webpage|API page)", re.IGNORECASE), "transient"),
    (re.compile(r"playlists? that require authentication", re.IGNORECASE), "transient"),
]


# Any yt-dlp ERROR line, used to decide whether an exit code can be forgiven.
_ANY_ERROR = re.compile(r"^\s*ERROR:", re.IGNORECASE)

# "ERROR: [youtube:tab] @handle: This channel does not have a streams tab"
# yt-dlp continues past this and still processes the remaining tab URLs, but
# the process still exits 1.  See :func:`missing_tabs`.
_MISSING_TAB = re.compile(
    r"ERROR:.*this channel does not have an?\s+(\w+)\s+tab",
    re.IGNORECASE,
)


def missing_tabs(output_lines: list[str]) -> list[str] | None:
    """Return the tab names that were missing, when *that was the only problem*.

    The *quick* strategy always passes ``/videos``, ``/shorts`` and ``/streams``
    to yt-dlp, but plenty of channels have never posted a Short or gone live.
    yt-dlp logs an error for each absent tab and exits 1, even though it
    archived the tabs that do exist perfectly well.  Taking that exit code at
    face value marks a healthy source as permanently failing.

    Returns the missing tab names (in the order they were reported) when every
    ``ERROR:`` line in *output_lines* was a missing-tab error, so the caller may
    treat the run as successful.  Returns *None* when there were no missing-tab
    errors, or when some *other* error also occurred — a real failure must keep
    its nonzero exit code even if a tab happened to be missing too.
    """
    tabs: list[str] = []
    for line in output_lines:
        if not _ANY_ERROR.match(line):
            continue
        m = _MISSING_TAB.search(line)
        if m is None:
            return None  # an unrelated error — the exit code is genuine
        tabs.append(m.group(1).lower())
    return tabs or None


def classify_line(line: str) -> Classification:
    """Classify a single yt-dlp output line.

    Returns a :class:`Classification` with ``video_id`` extracted when the line
    matches the standard ``ERROR: [youtube] VIDEOID:`` prefix, and ``kind`` set
    to ``'permanent'`` or ``'transient'`` when a pattern matches.  Both fields
    are *None* when the line does not match any classification pattern.
    """
    # Try to extract the video id from the standard ERROR prefix.
    video_id: str | None = None

    m = _ERROR_PREFIX.match(line.strip())
    if m:
        video_id = m.group(1)

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
