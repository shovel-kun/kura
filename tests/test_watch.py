from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kura import monitor
from kura.cli import main


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def make_config_file(config_dir: Path, output_dir: Path) -> None:
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "av1-archive.conf").write_text("--continue\n")
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://example.com/channel
    output_dir: {output_dir}
    enabled: true
"""
    )


@pytest.mark.parametrize("argv", [["status"], ["status", "test-src"]])
def test_status_shows_running_indicator(
    monkeypatch, tmp_path: Path, capsys, argv: list[str]
) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    assert main(["init"]) == 0
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(state_dir / "state.db") as db:
        db.execute(
            "INSERT INTO source_state (source, last_check_at, imported_at) VALUES (?, ?, ?)",
            ("test-src", now, now),
        )
        db.execute(
            """
            INSERT INTO runs (source, started_at, ended_at, exit_code, new_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("test-src", now, now, 1, 0),
        )
    log_dir = state_dir / "logs" / "test-src"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "run.log").write_text(
        "[download]  42.0% of  10.00MiB at  1.00MiB/s ETA 00:06\n"
    )
    monkeypatch.setattr(monitor, "running_sources", lambda config, state: ["test-src"])
    capsys.readouterr()

    assert main(argv) == 0
    output = capsys.readouterr().out
    assert "running 42%" in output
    if argv == ["status"]:
        assert "failed (exit 1)" not in output


def test_watch_once_non_tty_prints_snapshot_and_exits(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    log_dir = state_dir / "logs" / "test-src"
    log_dir.mkdir(parents=True)
    (log_dir / "run.log").write_text(
        "[download]  42.0% of  10.00MiB at  1.00MiB/s ETA 00:06\n"
    )
    monkeypatch.setattr(monitor, "running_sources", lambda config, state: ["test-src"])

    assert main(["watch", "--once"]) == 0
    output = capsys.readouterr().out
    assert "test-src" in output
    assert "42%" in output


def test_watch_once_unknown_source_exits_one(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)

    assert main(["watch", "missing", "--once"]) == 1
    assert "unknown source 'missing'" in capsys.readouterr().out


def test_watch_interactive_with_nothing_running_exits_immediately(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    monkeypatch.setattr(monitor, "running_sources", lambda config, state: [])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert main(["watch"]) == 0
    assert "nothing running" in capsys.readouterr().out


def test_watch_wait_polls_until_a_run_starts(monkeypatch, tmp_path: Path, capsys) -> None:
    """--wait should keep polling while idle, then pick the run up once it starts."""
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    # idle for three polls, then the run appears, then it finishes
    running_polls = iter([[], [], [], ["test-src"], [], [], []])
    monkeypatch.setattr(
        monitor, "running_sources", lambda config, state: next(running_polls, [])
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert main(["watch", "--wait", "--interval", "0"]) == 0
    output = capsys.readouterr().out
    assert "waiting for a run to start" in output
    assert "nothing running" not in output


def test_watch_wait_stops_cleanly_on_interrupt(monkeypatch, tmp_path: Path, capsys) -> None:
    """Ctrl-C while waiting should exit 0, not raise."""
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    monkeypatch.setattr(monitor, "running_sources", lambda config, state: [])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def boom(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("kura.commands.time.sleep", boom)

    assert main(["watch", "--wait"]) == 0
    assert "waiting for a run to start" in capsys.readouterr().out


def test_watch_without_wait_still_exits_immediately(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Default behaviour is unchanged: no --wait means exit when idle."""
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    monkeypatch.setattr(monitor, "running_sources", lambda config, state: [])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert main(["watch"]) == 0
    out = capsys.readouterr().out
    assert "nothing running" in out
    assert "waiting for a run to start" not in out


def test_watch_interactive_exits_after_three_empty_polls(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config_file(config_dir, output_dir)
    running_polls = iter([["test-src"], [], [], []])
    monkeypatch.setattr(
        monitor,
        "running_sources",
        lambda config, state: next(running_polls),
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("kura.commands.time.sleep", lambda interval: None)

    assert main(["watch"]) == 0
    assert "test-src" in capsys.readouterr().out
