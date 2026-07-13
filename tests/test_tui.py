from __future__ import annotations

import io
import sys
import time

import pytest

from kura import library, ui
from kura.cli import main
from kura.config import load_config
from test_library import make_config, set_kura_dirs, write_nn_video, write_yt_video


@pytest.fixture(autouse=True)
def no_color(monkeypatch):
    monkeypatch.setenv("KURA_COLOR", "never")
    ui._reset()
    yield
    ui._reset()


def prepare_archive(monkeypatch, tmp_path):
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    yt_dir = tmp_path / "yt"
    nn_dir = tmp_path / "nn"
    make_config(
        config_dir,
        [
            {"name": "yt-src", "output_dir": yt_dir},
            {"name": "nico-src", "output_dir": nn_dir, "downloader": "nndownload"},
        ],
    )
    now = time.time()
    newest = write_yt_video(
        yt_dir,
        "ytNEWEST001",
        "Alpha Newest",
        uploader="Maker One",
        media_bytes=2_000,
        mtime=now,
    )
    write_yt_video(
        yt_dir,
        "ytOLDER0001",
        "Alpha Session",
        uploader="Maker Two",
        mtime=now - 60,
    )
    write_nn_video(
        nn_dir,
        "sm9",
        "Beta Nico",
        uploader="Nico Maker",
        mtime=now - 120,
    )
    config = load_config()
    for source in config.sources:
        library.refresh_index(source)
    return newest


def load_app_class():
    from kura.tui.app import KuraApp

    return KuraApp


def cell_text(table, row: int, column: int) -> str:
    return str(table.get_cell_at((row, column)))


def test_ui_non_tty_does_not_import_tui(monkeypatch):
    for name in [name for name in sys.modules if name == "kura.tui" or name.startswith("kura.tui.")]:
        sys.modules.pop(name)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)

    assert main(["ui"]) == 1
    assert "kura ui needs an interactive terminal" in output.getvalue()
    assert "kura.tui" not in sys.modules


@pytest.mark.asyncio
async def test_app_boots_with_downloaded_order(monkeypatch, tmp_path):
    prepare_archive(monkeypatch, tmp_path)
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#library-table")
        assert table.row_count == 3
        assert cell_text(table, 0, 1) == "Alpha Newest"


@pytest.mark.asyncio
async def test_search_filters_with_and_semantics(monkeypatch, tmp_path):
    prepare_archive(monkeypatch, tmp_path)
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        search = app.screen.query_one("#search")
        search.value = "alpha two"
        await pilot.pause(0.2)
        table = app.screen.query_one("#library-table")
        assert table.row_count == 1
        assert cell_text(table, 0, 1) == "Alpha Session"


@pytest.mark.asyncio
async def test_highlight_updates_detail(monkeypatch, tmp_path):
    prepare_archive(monkeypatch, tmp_path)
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#library-table")
        table.move_cursor(row=1)
        await pilot.pause()
        detail = app.screen.query_one("#detail")
        assert "Maker Two" in str(detail.content)


@pytest.mark.asyncio
async def test_tab_opens_sources_dashboard(monkeypatch, tmp_path):
    prepare_archive(monkeypatch, tmp_path)
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("tab")
        table = app.screen.query_one("#sources-table")
        assert table.row_count == 2
        assert cell_text(table, 0, 0) == "●"
        assert cell_text(table, 0, 1) == "yt-src"


@pytest.mark.asyncio
async def test_pause_updates_config_and_source_row(monkeypatch, tmp_path):
    prepare_archive(monkeypatch, tmp_path)
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("tab", "p")
        table = app.screen.query_one("#sources-table")
        assert load_config().sources[0].enabled is False
        assert cell_text(table, 0, 0) == "○"


@pytest.mark.asyncio
async def test_enter_plays_resolved_media_file(monkeypatch, tmp_path):
    newest = prepare_archive(monkeypatch, tmp_path)
    from kura.tui import launchers

    captured = []

    def fake_popen(argv, **kwargs):
        captured.append(argv)

    monkeypatch.setattr(launchers, "Popen", fake_popen)
    monkeypatch.setattr(launchers.shutil, "which", lambda command: "/usr/bin/mpv")
    app = load_app_class()()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("enter")

    assert captured
    assert captured[0] == ["mpv", str(newest / "Alpha Newest [ytNEWEST001].mkv")]


def test_truncate_cells_cjk_aware():
    from kura.tui.library_screen import _truncate_cells

    # ASCII: fits untouched, truncates with ellipsis
    assert _truncate_cells("hello", 10) == "hello"
    assert _truncate_cells("hello world", 8) == "hello w…"
    # CJK: each char is 2 cells; 10 cells fit 4 chars + ellipsis
    assert _truncate_cells("あきばっかーの", 10) == "あきばっ…"
    # exact fit is not truncated
    assert _truncate_cells("あいう", 6) == "あいう"
