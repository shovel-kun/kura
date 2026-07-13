from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from kura.archive import (
    MEDIA_EXTENSIONS,
    append_archive_entries,
    archive_key,
    read_archive,
)
from kura.cli import main


# ---------------------------------------------------------------------------
# Helpers shared by multiple tests
# ---------------------------------------------------------------------------


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def write_profile(config_dir: Path, name: str = "av1-archive") -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / f"{name}.conf").write_text("--continue\n")


def make_config(config_dir: Path, output_dir: Path, archive_file: Path | None = None) -> None:
    """Write a minimal config.yaml with one source called 'test-src'."""
    write_profile(config_dir)
    archive_line = ""
    if archive_file is not None:
        archive_line = f"\n  archive_file: {archive_file}"
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: quick
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://www.youtube.com/@TestChannel
    output_dir: {output_dir}{archive_line}
    extra_args: []
    enabled: true
"""
    )


def write_info_json(directory: Path, video_id: str, extractor_key: str = "Youtube") -> Path:
    """Write a minimal .info.json file and return its path."""
    info = {
        "id": video_id,
        "extractor_key": extractor_key,
        "title": f"Video {video_id}",
        "_type": "video",
    }
    path = directory / f"Video {video_id}" / f"Video {video_id}.info.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. PARITY TEST — archive_key must match yt-dlp's _make_archive_id exactly
# ---------------------------------------------------------------------------


PARITY_CASES = [
    # (info dict, expected key)
    ({"id": "dQw4w9WgXcQ", "extractor_key": "Youtube"}, "youtube dQw4w9WgXcQ"),
    ({"id": "sm9", "extractor_key": "Niconico"}, "niconico sm9"),
    ({"id": "abc123", "extractor_key": "BiliBili"}, "bilibili abc123"),
    # ie_key fallback
    ({"id": "xyz789", "ie_key": "Youtube"}, "youtube xyz789"),
]


@pytest.mark.parametrize("info,expected", PARITY_CASES)
def test_archive_key_matches_ytdlp(info: dict, expected: str) -> None:
    """archive_key must return the same value as yt-dlp's _make_archive_id."""
    import yt_dlp

    ydl = yt_dlp.YoutubeDL({"quiet": True})
    assert ydl._make_archive_id(info) == expected, "sanity check against installed yt-dlp"
    assert archive_key(info) == expected


@pytest.mark.parametrize("info,expected", PARITY_CASES)
def test_archive_key_fallback_matches_ytdlp(monkeypatch, info: dict, expected: str) -> None:
    """The pure-Python fallback must produce the same string as yt-dlp."""
    # Simulate yt_dlp being unavailable by patching the lazy getter to raise.
    import kura.archive as archive_mod

    def raise_import_error():
        raise ImportError("yt_dlp not available")

    monkeypatch.setattr(archive_mod, "_get_ydl", raise_import_error)
    # Also reset cached instance so the patch takes effect.
    monkeypatch.setattr(archive_mod, "_ydl_instance", None)

    assert archive_key(info) == expected


def test_archive_key_returns_none_when_no_id() -> None:
    assert archive_key({"extractor_key": "Youtube"}) is None


def test_archive_key_returns_none_when_no_extractor() -> None:
    # No url either — should return None
    assert archive_key({"id": "abc123"}) is None


def test_archive_key_fallback_returns_none_when_no_id(monkeypatch) -> None:
    import kura.archive as archive_mod

    def raise_import_error():
        raise ImportError("yt_dlp not available")

    monkeypatch.setattr(archive_mod, "_get_ydl", raise_import_error)
    monkeypatch.setattr(archive_mod, "_ydl_instance", None)

    assert archive_key({"extractor_key": "Youtube"}) is None


# ---------------------------------------------------------------------------
# 2. read_archive
# ---------------------------------------------------------------------------


def test_read_archive_missing_file(tmp_path: Path) -> None:
    assert read_archive(tmp_path / "nonexistent.txt") == set()


def test_read_archive_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "archive.txt"
    path.write_text("youtube dQw4w9WgXcQ\nniconico sm9\n\n  youtube abc123  \n", encoding="utf-8")
    result = read_archive(path)
    assert result == {"youtube dQw4w9WgXcQ", "niconico sm9", "youtube abc123"}


# ---------------------------------------------------------------------------
# 3. append_archive_entries
# ---------------------------------------------------------------------------


def test_append_creates_file_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "archive.txt"
    append_archive_entries(path, ["youtube aaaaaaaaaaa"])
    assert path.read_text(encoding="utf-8") == "youtube aaaaaaaaaaa\n"
    # No backup should have been created when the file didn't exist before.
    backups = list(tmp_path.glob("archive.txt.bak.*"))
    assert backups == []


def test_append_creates_backup_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "archive.txt"
    original = "youtube dQw4w9WgXcQ\n"
    path.write_text(original, encoding="utf-8")

    append_archive_entries(path, ["youtube bbbbbbbbbbb", "niconico sm9"])

    content = path.read_text(encoding="utf-8")
    assert "youtube dQw4w9WgXcQ" in content
    assert "youtube bbbbbbbbbbb" in content
    assert "niconico sm9" in content

    backups = list(tmp_path.glob("archive.txt.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_append_empty_entries_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "archive.txt"
    original = "youtube dQw4w9WgXcQ\n"
    path.write_text(original, encoding="utf-8")
    mtime_before = path.stat().st_mtime

    append_archive_entries(path, [])

    assert path.stat().st_mtime == mtime_before
    assert path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# 4. kura import — full synthetic tree integration test
# ---------------------------------------------------------------------------


def _build_synthetic_tree(output_dir: Path) -> dict:
    """
    Build a synthetic archive tree and return tracking metadata.

    Tree layout:
      vid_A/ — already in archive
      vid_B/ — NOT in archive → should be appended
      vid_C/ — NOT in archive → should be appended
      media_only/ — media file but no .info.json → unverifiable
      playlist_dir/ — playlist-level info.json → should be skipped
      corrupt_dir/ — corrupt JSON → should be warned about
    """
    # vid_A: youtube id1 — already in archive
    write_info_json(output_dir, "id1111111111", "Youtube")

    # vid_B: youtube id2 — missing from archive
    write_info_json(output_dir, "id2222222222", "Youtube")

    # vid_C: niconico sm999 — missing from archive
    nico_dir = output_dir / "nico-video"
    nico_dir.mkdir()
    (nico_dir / "nico-video.info.json").write_text(
        json.dumps({"id": "sm999", "extractor_key": "Niconico", "_type": "video"}),
        encoding="utf-8",
    )

    # media_only: a media file with no .info.json anywhere in its directory
    media_dir = output_dir / "media_only"
    media_dir.mkdir()
    (media_dir / "some_video.mkv").write_bytes(b"")  # empty file

    # playlist: playlist-level info.json
    pl_dir = output_dir / "playlist"
    pl_dir.mkdir()
    (pl_dir / "playlist.info.json").write_text(
        json.dumps({"_type": "playlist", "id": "PL123", "extractor_key": "YoutubeTab"}),
        encoding="utf-8",
    )

    # corrupt: unparseable JSON
    corrupt_dir = output_dir / "corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "bad.info.json").write_text("{not valid json", encoding="utf-8")

    return {
        "already_in_archive": "youtube id1111111111",
        "new_keys": ["niconico sm999", "youtube id2222222222"],  # sorted
    }


def test_import_full_tree(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    meta = _build_synthetic_tree(output_dir)

    # Pre-populate archive with one entry.
    archive_path = output_dir / "archive.txt"
    archive_path.write_text(meta["already_in_archive"] + "\n", encoding="utf-8")
    # Add one orphan entry (no corresponding info.json).
    with archive_path.open("a", encoding="utf-8") as f:
        f.write("youtube orphan00000\n")

    assert main(["import", "test-src"]) == 0

    out = capsys.readouterr().out
    assert "scanned" in out
    assert "  appended   2" in out
    assert "  in archive 1" in out

    # Unverifiable media files reported.
    assert "unverifiable media" in out
    assert "some_video.mkv" in out

    # Orphan reported but not removed.
    assert "1 orphan archive entry kept" in out

    # Corrupt JSON warned.
    assert "unparseable" in out.lower() or "Warnings" in out

    # Appended lines must match exactly.
    content = archive_path.read_text(encoding="utf-8")
    for key in meta["new_keys"]:
        assert key in content

    # Backup must exist with original content.
    backups = list(output_dir.glob("archive.txt.bak.*"))
    assert len(backups) == 1
    backup_content = backups[0].read_text(encoding="utf-8")
    assert meta["already_in_archive"] in backup_content
    assert "youtube orphan00000" in backup_content

    # source_state.imported_at must be set.
    with sqlite3.connect(state_dir / "state.db") as conn:
        row = conn.execute(
            "SELECT imported_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
    assert row is not None
    assert row[0]  # non-empty timestamp


def test_import_dry_run_no_changes(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    _build_synthetic_tree(output_dir)

    archive_path = output_dir / "archive.txt"
    original_content = "youtube id1111111111\n"
    archive_path.write_text(original_content, encoding="utf-8")

    # Record filesystem state before dry-run.
    before_files = {p: p.stat().st_mtime for p in output_dir.rglob("*") if p.is_file()}

    assert main(["import", "test-src", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would append 2" in out

    # No files changed.
    after_files = {p: p.stat().st_mtime for p in output_dir.rglob("*") if p.is_file()}
    assert before_files == after_files

    # No backup created.
    backups = list(output_dir.glob("archive.txt.bak.*"))
    assert backups == []

    # archive.txt unchanged.
    assert archive_path.read_text(encoding="utf-8") == original_content

    # No DB change.
    with sqlite3.connect(state_dir / "state.db") as conn:
        row = conn.execute(
            "SELECT imported_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
    assert row is None


def test_import_idempotent(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    write_info_json(output_dir, "id9999999999", "Youtube")

    assert main(["import", "test-src"]) == 0
    capsys.readouterr()  # discard first output

    assert main(["import", "test-src"]) == 0
    out = capsys.readouterr().out
    assert "appended   0" in out


def test_import_creates_archive_when_absent(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    write_info_json(output_dir, "id0000000001", "Youtube")
    write_info_json(output_dir, "id0000000002", "Youtube")

    archive_path = output_dir / "archive.txt"
    assert not archive_path.exists()

    assert main(["import", "test-src"]) == 0

    assert archive_path.exists()
    content = read_archive(archive_path)
    assert "youtube id0000000001" in content
    assert "youtube id0000000002" in content

    # No backup since file didn't exist before.
    backups = list(output_dir.glob("archive.txt.bak.*"))
    assert backups == []


def test_import_unknown_source_exits_1(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    assert main(["import", "no-such-source"]) == 1
    out = capsys.readouterr().out
    assert "unknown source" in out
