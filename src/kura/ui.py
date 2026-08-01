from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime, timedelta

OK = "✓"
FAIL = "✗"
ON = "●"
OFF = "○"
DOT = "·"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_COLOR_CACHE: bool | None = None


def _reset() -> None:
    global _COLOR_CACHE
    _COLOR_CACHE = None


def use_color() -> bool:
    global _COLOR_CACHE
    if _COLOR_CACHE is None:
        override = os.environ.get("KURA_COLOR")
        if override == "always":
            _COLOR_CACHE = True
        elif override == "never":
            _COLOR_CACHE = False
        else:
            _COLOR_CACHE = (
                sys.stdout.isatty()
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM") != "dumb"
            )
    return _COLOR_CACHE


def _style(value: str, code: int) -> str:
    return f"\x1b[{code}m{value}\x1b[0m" if use_color() else value


def bold(value: str) -> str:
    return _style(value, 1)


def dim(value: str) -> str:
    return _style(value, 2)


def green(value: str) -> str:
    return _style(value, 32)


def red(value: str) -> str:
    return _style(value, 31)


def yellow(value: str) -> str:
    return _style(value, 33)


def cyan(value: str) -> str:
    return _style(value, 36)


def visible_len(value: str) -> int:
    return len(_ANSI_RE.sub("", value))


def pad(value: str, width: int) -> str:
    return value + " " * max(0, width - visible_len(value))


def local_dt(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def local_ts(value: str | datetime, fmt: str = "%b %d %H:%M") -> str:
    return local_dt(value).strftime(fmt)


def rel_time(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    local_value = dt.astimezone()
    local_now = now.astimezone()

    if local_value <= local_now:
        seconds = (local_now - local_value).total_seconds()
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{int(seconds // 60)}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        if local_value.date() == local_now.date() - timedelta(days=1):
            return f"yesterday {local_value:%H:%M}"
        if local_value.year == local_now.year:
            return local_value.strftime("%b %d %H:%M")
        return local_value.strftime("%Y-%m-%d")

    seconds = (local_value - local_now).total_seconds()
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    if seconds < 86400:
        return f"in {int(seconds // 3600)}h"
    if local_value.date() == local_now.date() + timedelta(days=1):
        return f"tomorrow {local_value:%H:%M}"
    return local_value.strftime("%b %d %H:%M")


def human_cron(expr: str) -> str:
    hourly = re.fullmatch(r"0 \*/(\d+) \* \* \*(?: \*)?", expr)
    if hourly:
        return f"every {hourly.group(1)}h"
    minutely = re.fullmatch(r"\*/(\d+) \* \* \* \*", expr)
    if minutely:
        return f"every {minutely.group(1)}m"
    daily = re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* \*", expr)
    if daily:
        minute, hour = map(int, daily.groups())
        if minute < 60 and hour < 24:
            return f"daily {hour:02d}:{minute:02d}"
    return expr
