from datetime import UTC, datetime, timedelta

from kura import ui

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_rel_time_past_buckets() -> None:
    assert ui.rel_time(NOW - timedelta(seconds=20), NOW) == "just now"
    assert ui.rel_time(NOW - timedelta(minutes=12), NOW) == "12m ago"
    assert ui.rel_time(NOW - timedelta(hours=3), NOW) == "3h ago"

    local_now = NOW.astimezone()
    yesterday = (local_now - timedelta(days=1)).replace(hour=9, minute=30)
    assert ui.rel_time(yesterday, NOW) == "yesterday 09:30"

    same_year = local_now.replace(month=6, day=12, hour=11, minute=23)
    assert ui.rel_time(same_year, NOW) == "Jun 12 11:23"
    old = local_now.replace(year=2025, month=12, day=31)
    assert ui.rel_time(old, NOW) == "2025-12-31"


def test_rel_time_future_buckets() -> None:
    assert ui.rel_time(NOW + timedelta(minutes=25), NOW) == "in 25m"
    assert ui.rel_time(NOW + timedelta(hours=4), NOW) == "in 4h"

    local_now = NOW.astimezone()
    tomorrow = (local_now + timedelta(days=1)).replace(hour=23, minute=30)
    assert ui.rel_time(tomorrow, NOW) == "tomorrow 23:30"

    later = local_now + timedelta(days=8)
    assert ui.rel_time(later, NOW) == later.strftime("%b %d %H:%M")


def test_human_cron_known_patterns_and_passthrough() -> None:
    assert ui.human_cron("0 */6 * * *") == "every 6h"
    assert ui.human_cron("0 */6 * * * *") == "every 6h"
    assert ui.human_cron("*/15 * * * *") == "every 15m"
    assert ui.human_cron("5 9 * * *") == "daily 09:05"
    assert ui.human_cron("0 9 * * 1") == "0 9 * * 1"


def test_use_color_auto_and_overrides(monkeypatch) -> None:
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("KURA_COLOR", raising=False)
    ui._reset()
    assert ui.use_color()

    monkeypatch.setenv("NO_COLOR", "1")
    ui._reset()
    assert not ui.use_color()

    monkeypatch.setenv("KURA_COLOR", "always")
    ui._reset()
    assert ui.use_color()

    monkeypatch.setenv("KURA_COLOR", "never")
    monkeypatch.delenv("NO_COLOR", raising=False)
    ui._reset()
    assert not ui.use_color()
    ui._reset()


def test_visible_len_and_pad_ignore_ansi(monkeypatch) -> None:
    monkeypatch.setenv("KURA_COLOR", "always")
    ui._reset()
    styled = ui.green("✓ ready")
    assert ui.visible_len(styled) == len("✓ ready")
    assert ui.visible_len(ui.pad(styled, 10)) == 10
    ui._reset()
