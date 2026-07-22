from __future__ import annotations

import io
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console

from kura import monitor
from kura.config import Config, Source


def make_source(tmp_path: Path, name: str, *, enabled: bool = True) -> Source:
    output_dir = tmp_path / name
    return Source(
        name=name,
        url=f"https://example.com/{name}",
        output_dir=output_dir,
        archive_file=output_dir / "archive.txt",
        profile="av1-archive",
        cookies=None,
        schedule="0 */6 * * *",
        update_strategy="full",
        full_reindex_every="7d",
        extra_args=[],
        enabled=enabled,
    )


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            ["[download]  42.2% of  106.91MiB at    4.19MiB/s ETA 00:15"],
            {
                "pct": 42.2,
                "speed": "4.19MiB/s",
                "eta": "00:15",
                "size": "106.91MiB",
                "phase": "downloading",
            },
        ),
        (
            ["[youtube:tab] Playlist uploads: Downloading API JSON page 3"],
            {"phase": "enumerating"},
        ),
        (
            ["[download] abc123 has already been recorded in the archive"],
            {"phase": "skipping"},
        ),
        (
            ["[download] Destination: /archive/すごい動画 [abc123]/すごい動画 [abc123].webm"],
            {"title": "すごい動画 [abc123]"},
        ),
        (
            ["[youtube] Downloading comments via the comment API"],
            {"phase": "comments"},
        ),
        (
            [
                "[download] 100% of    1.19GiB in 00:04:26 at 4.56MiB/s",
                '[FixupM3u8] Fixing MPEG-TS in MP4 container of "vid [abc].mp4"',
            ],
            {"phase": "remuxing", "pct": None, "speed": None, "eta": None},
        ),
        (
            [
                "[download] 100% of 50.0MiB in 00:10",
                "[Merger] Merging formats into \"vid [abc].mkv\"",
            ],
            {"phase": "merging", "pct": None},
        ),
        (
            [
                "[download] 100% of 50.0MiB in 00:10",
                "[EmbedThumbnail] Adding thumbnail to \"vid [abc].mkv\"",
            ],
            {"phase": "embedding", "pct": None},
        ),
        (
            [],
            {
                "pct": None,
                "speed": None,
                "eta": None,
                "size": None,
                "title": None,
                "phase": "working",
            },
        ),
        (
            ["unrecognized output"],
            {
                "pct": None,
                "speed": None,
                "eta": None,
                "size": None,
                "title": None,
                "phase": "working",
            },
        ),
    ],
)
def test_parse_progress(lines: list[str], expected: dict) -> None:
    result = monitor.parse_progress(lines)
    assert set(result) == {"pct", "speed", "eta", "size", "title", "phase"}
    for key, value in expected.items():
        assert result[key] == value


def test_parse_progress_completed_download_and_id_fallback() -> None:
    result = monitor.parse_progress(
        [
            "[youtube] abc123: Downloading webpage",
            "[download] 100% of  106.91MiB in 00:12",
        ]
    )
    assert result == {
        "pct": 100.0,
        "speed": None,
        "eta": None,
        "size": "106.91MiB",
        "title": "abc123",
        "phase": "downloading",
    }


def test_current_log_uses_newest_mtime(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    log_dir = state_dir / "logs" / "source-a"
    log_dir.mkdir(parents=True)
    older = log_dir / "older.log"
    newer = log_dir / "newer.log"
    ignored = log_dir / "newer.txt"
    older.write_text("old")
    newer.write_text("new")
    ignored.write_text("ignored")
    os.utime(older, (10, 10))
    os.utime(newer, (20, 20))
    os.utime(ignored, (30, 30))

    assert monitor.current_log("source-a", state_dir) == newer
    assert monitor.current_log("missing", state_dir) is None


def test_tail_lines_reads_last_lines_and_tolerates_missing_file(tmp_path: Path) -> None:
    log_path = tmp_path / "run.log"
    log_path.write_bytes("one\ntwø\n三\nfour\n".encode())

    assert monitor.tail_lines(log_path, 2) == ["三", "four"]
    assert monitor.tail_lines(tmp_path / "missing.log") == []


def test_running_sources_returns_held_locks_in_config_order(monkeypatch, tmp_path: Path) -> None:
    config = Config(
        defaults={},
        sources=[
            make_source(tmp_path, "first"),
            make_source(tmp_path, "second"),
            make_source(tmp_path, "third"),
        ],
    )

    @contextmanager
    def fake_source_lock(name: str, state_dir: Path):
        del state_dir
        yield name not in {"first", "third"}

    monkeypatch.setattr(monitor.locks, "source_lock", fake_source_lock)

    assert monitor.running_sources(config, tmp_path / "state") == ["first", "third"]


def test_archive_count(tmp_path: Path) -> None:
    archive = tmp_path / "archive.txt"
    assert monitor.archive_count(archive) == 0
    archive.write_text("youtube one\nyoutube two\n")
    assert monitor.archive_count(archive) == 2


def test_render_watch_contains_running_and_idle_sources() -> None:
    snapshots = [
        monitor.WatchSnapshot(
            name="active",
            running=True,
            pct=42.0,
            speed="4.19MiB/s",
            eta="00:15",
            title="Current video [abc123]",
            phase="downloading",
            archive_delta=2,
        ),
        monitor.WatchSnapshot(name="resting", running=False),
    ]
    output = io.StringIO()
    Console(file=output, force_terminal=False, width=100).print(
        monitor.render_watch(snapshots)
    )

    text = output.getvalue()
    assert "active" in text
    assert "resting" in text
    assert "idle" in text
    assert "42%" in text
    assert "+2 new" in text
