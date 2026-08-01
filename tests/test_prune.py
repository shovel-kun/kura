"""Tests for `kura prune` — deleting files while guaranteeing IDs stay archived."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kura.cli import main


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir, state_dir = tmp_path / "config", tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def make_video(output_dir: Path, video_id: str, title: str) -> Path:
    """Create a yt-dlp style per-video folder with a sidecar and a media file."""
    d = output_dir / f"{title} [{video_id}]"
    d.mkdir(parents=True)
    stem = f"{title} [{video_id}]"
    (d / f"{stem}.info.json").write_text(json.dumps({
        "id": video_id, "title": title, "extractor_key": "Youtube",
        "uploader": "chan", "upload_date": "20260101", "duration": 60,
    }))
    (d / f"{stem}.mp4").write_bytes(b"x" * 1024)
    return d


def setup_source(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "profiles" / "av1-archive.conf").write_text("--continue\n")
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: src
    url: https://www.youtube.com/@Test
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )
    assert main(["init"]) == 0
    return config_dir, state_dir, output_dir


def test_prune_deletes_files_and_keeps_id_archived(monkeypatch, tmp_path, capsys) -> None:
    """The whole point: files go, the ID stays so it is never re-downloaded."""
    _, _, output_dir = setup_source(monkeypatch, tmp_path)
    d = make_video(output_dir, "aaaaaaaaaaa", "Some Short")
    make_video(output_dir, "bbbbbbbbbbb", "A Keeper")

    assert main(["prune", "src", "Short", "--yes"]) == 0

    assert not d.exists()                                   # folder gone
    assert (output_dir / "A Keeper [bbbbbbbbbbb]").exists()  # untouched
    archive = (output_dir / "archive.txt").read_text()
    assert "youtube aaaaaaaaaaa" in archive                  # ID retained → no re-download


def test_prune_archives_before_deleting(monkeypatch, tmp_path) -> None:
    """Fail-safe ordering: a crash mid-deletion still leaves the ID protected.

    This is the property that makes prune safe. Deletion is the step that can
    fail (permissions, disk, interruption); if the archive were written after,
    a partial run would leave files gone *and* the ID absent — exactly the
    re-download trap the command exists to prevent.
    """
    _, _, output_dir = setup_source(monkeypatch, tmp_path)
    make_video(output_dir, "ccccccccccc", "Doomed")

    def boom(self, *a, **kw):
        raise OSError("simulated failure during deletion")

    monkeypatch.setattr(Path, "unlink", boom)
    try:
        main(["prune", "src", "Doomed", "--yes"])
    except OSError:
        pass

    archive = output_dir / "archive.txt"
    assert archive.exists(), "archive must be written before any deletion is attempted"
    assert "youtube ccccccccccc" in archive.read_text()


def test_prune_dry_run_changes_nothing(monkeypatch, tmp_path, capsys) -> None:
    _, _, output_dir = setup_source(monkeypatch, tmp_path)
    d = make_video(output_dir, "ddddddddddd", "Preview Me")

    assert main(["prune", "src", "Preview", "--dry-run"]) == 0
    assert d.exists()
    assert not (output_dir / "archive.txt").exists() or \
        "ddddddddddd" not in (output_dir / "archive.txt").read_text()
    assert "dry run" in capsys.readouterr().out


def test_prune_refuses_unfiltered(monkeypatch, tmp_path, capsys) -> None:
    """No filter must never mean 'delete the whole source'."""
    _, _, output_dir = setup_source(monkeypatch, tmp_path)
    d = make_video(output_dir, "eeeeeeeeeee", "Everything")

    assert main(["prune", "src", "--yes"]) == 1
    assert d.exists()
    assert "refusing to prune everything" in capsys.readouterr().out


def test_prune_by_id(monkeypatch, tmp_path) -> None:
    _, _, output_dir = setup_source(monkeypatch, tmp_path)
    a = make_video(output_dir, "fffffffffff", "Title One")
    b = make_video(output_dir, "ggggggggggg", "Title Two")

    assert main(["prune", "src", "--id", "fffffffffff", "--yes"]) == 0
    assert not a.exists() and b.exists()
    assert "youtube fffffffffff" in (output_dir / "archive.txt").read_text()


def test_prune_records_suppression(monkeypatch, tmp_path) -> None:
    _, state_dir, output_dir = setup_source(monkeypatch, tmp_path)
    make_video(output_dir, "hhhhhhhhhhh", "Logged")

    assert main(["prune", "src", "Logged", "--yes", "--reason", "shorts"]) == 0
    with sqlite3.connect(state_dir / "state.db") as conn:
        row = conn.execute(
            "SELECT archive_key, reason FROM suppressions WHERE source='src'"
        ).fetchone()
    assert row == ("youtube hhhhhhhhhhh", "shorts")
