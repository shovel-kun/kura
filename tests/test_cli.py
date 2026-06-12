from __future__ import annotations

import sqlite3
from pathlib import Path

from kura.cli import main


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def test_init_idempotence(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)

    assert main(["init"]) == 0
    config_text = (config_dir / "config.yaml").read_text()
    profile_text = (config_dir / "profiles" / "av1-archive.conf").read_text()

    assert main(["init"]) == 0
    assert (config_dir / "config.yaml").read_text() == config_text
    assert (config_dir / "profiles" / "av1-archive.conf").read_text() == profile_text
    assert (state_dir / "state.db").exists()
    assert (state_dir / "locks").is_dir()
    assert (state_dir / "logs").is_dir()


def test_state_db_schema_created(monkeypatch, tmp_path: Path) -> None:
    _config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)

    assert main(["init"]) == 0

    with sqlite3.connect(state_dir / "state.db") as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        source_state_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(source_state)")
        }

    assert {"runs", "video_failures", "suppressions", "channel_cache", "source_state"} <= tables
    assert "last_full_run_at" in source_state_cols


def test_add_and_list_end_to_end(monkeypatch, tmp_path: Path, capsys) -> None:
    set_kura_dirs(monkeypatch, tmp_path)
    assert main(["init"]) == 0

    assert (
        main(
            [
                "add",
                "test-channel",
                "https://www.youtube.com/@TestChannel",
                "--dir",
                "/archive/test-channel",
                "--schedule",
                "0 0 * * *",
                "--strategy",
                "rss",
                "--cookies",
                "--cookies-from-browser firefox",
            ]
        )
        == 0
    )
    assert main(["list"]) == 0

    output = capsys.readouterr().out
    assert "test-channel" in output
    assert "rss" in output
    assert "0 0 * * *" in output
    assert "/archive/test-channel" in output


def test_doctor_without_config_reports_fail(monkeypatch, tmp_path: Path, capsys) -> None:
    set_kura_dirs(monkeypatch, tmp_path)

    # doctor exits 1 when config.yaml is missing (FAIL emitted)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
