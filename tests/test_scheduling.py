"""Tests for scheduling, locking, and pause/resume."""
from __future__ import annotations

import os
import sqlite3
import stat
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path

from kura.cli import main
from kura.commands import compute_next_due, is_source_due
from kura.locks import source_lock

# ---------------------------------------------------------------------------
# Shared helpers (mirror test_runner.py patterns)
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


def make_two_source_config(config_dir: Path, out1: Path, out2: Path) -> None:
    """Write a config with two sources: src-a (enabled) and src-b (enabled)."""
    write_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: src-a
    url: https://www.youtube.com/@TestChannelA
    output_dir: {out1}
    extra_args: []
    enabled: true
  - name: src-b
    url: https://www.youtube.com/@TestChannelB
    output_dir: {out2}
    extra_args: []
    enabled: true
"""
    )


def make_one_source_config(config_dir: Path, out: Path, *, enabled: bool = True) -> None:
    write_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://www.youtube.com/@TestChannel
    output_dir: {out}
    extra_args: []
    enabled: {str(enabled).lower()}
"""
    )


def write_fake_ytdlp(bin_dir: Path, exit_code: int = 0, archive_lines: list[str] | None = None) -> None:
    archive_lines = archive_lines or []
    lines_repr = repr(archive_lines)
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys

        args = sys.argv[1:]

        if args == ["--version"]:
            print("2099.01.01")
            sys.exit(0)

        if "--simulate" in args:
            print("Youtube abc001 First video")
            sys.exit({exit_code})

        archive = None
        for i, a in enumerate(args):
            if a == "--download-archive":
                archive = args[i + 1]
                break

        if archive is not None:
            for line in {lines_repr}:
                with open(archive, "a") as f:
                    f.write(line + "\\n")

        print("Downloading video abc123")
        sys.exit({exit_code})
    """)
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def set_last_check_at(db_path: Path, name: str, ts: str) -> None:
    """Upsert last_check_at in source_state for testing."""
    from kura import state
    with state.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, last_check_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_check_at = excluded.last_check_at
            """,
            (name, ts),
        )


# ---------------------------------------------------------------------------
# Due-computation unit tests
# ---------------------------------------------------------------------------


class TestComputeNextDue:
    """Tests for compute_next_due / is_source_due."""

    SCHEDULE = "0 */6 * * *"  # every 6 hours

    def test_null_last_check_is_always_due(self):
        """If last_check_at is None the source is immediately due."""
        now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        assert is_source_due(self.SCHEDULE, None, now=now) is True

    def test_just_run_is_not_due(self):
        """A source that ran seconds ago with a 6-hour schedule is not due."""
        now = datetime(2024, 6, 1, 12, 0, 5, tzinfo=UTC)
        last = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        assert is_source_due(self.SCHEDULE, last, now=now) is False

    def test_seven_hours_ago_is_due(self):
        """A source whose last run was 7 hours ago is due (schedule is 6h)."""
        now = datetime(2024, 6, 1, 19, 0, 0, tzinfo=UTC)
        last = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        assert is_source_due(self.SCHEDULE, last, now=now) is True

    def test_exactly_at_next_due_is_due(self):
        """A source whose next run time is exactly now is due."""
        last = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        next_dt = compute_next_due(self.SCHEDULE, last)
        # next_dt is 18:00 UTC; if now == next_dt it's due
        assert is_source_due(self.SCHEDULE, last, now=next_dt) is True

    def test_compute_next_due_returns_correct_datetime(self):
        """compute_next_due returns the correct next scheduled time."""
        last = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        next_dt = compute_next_due(self.SCHEDULE, last)
        assert next_dt.hour == 18
        assert next_dt.date() == date(2024, 6, 1)

    def test_null_last_check_returns_past_sentinel(self):
        """compute_next_due with None returns a past sentinel (year 1970)."""
        now = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        next_dt = compute_next_due(self.SCHEDULE, None, now=now)
        assert next_dt < now


# ---------------------------------------------------------------------------
# run --due e2e tests
# ---------------------------------------------------------------------------


class TestRunDue:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        out_a = tmp_path / "out-a"
        out_b = tmp_path / "out-b"
        out_a.mkdir()
        out_b.mkdir()
        make_two_source_config(config_dir, out_a, out_b)
        assert main(["init"]) == 0

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc111"])
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir

    def test_run_due_only_runs_due_source(self, monkeypatch, tmp_path, capsys):
        """Only src-a (due) runs; src-b (just ran) is skipped."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # src-b just checked → reliably not due on a 6h schedule. (croniter's next
        # fire is always strictly after the last check, so "just now" is never due
        # until the next boundary; a fixed "1 hour ago" is flaky because a 6h cron
        # boundary can fall within that hour, making the source due.)
        # src-a has no last_check_at → due immediately.
        recent = datetime.now(tz=UTC).isoformat()
        set_last_check_at(db_path, "src-b", recent)

        result = main(["run", "--due"])
        assert result == 0

        out = capsys.readouterr().out
        assert "not due" in out  # src-b printed as not due

        # Only src-a should have a runs row.
        with sqlite3.connect(db_path) as conn:
            src_a_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-a'"
            ).fetchone()[0]
            src_b_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-b'"
            ).fetchone()[0]
        assert src_a_runs == 1
        assert src_b_runs == 0

    def test_run_due_updates_source_state_for_due_source_only(self, monkeypatch, tmp_path):
        """source_state.last_check_at is updated only for the source that ran."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        recent = datetime.now(tz=UTC).isoformat()
        set_last_check_at(db_path, "src-b", recent)

        main(["run", "--due"])

        with sqlite3.connect(db_path) as conn:
            row_a = conn.execute(
                "SELECT last_check_at FROM source_state WHERE source = 'src-a'"
            ).fetchone()
            row_b = conn.execute(
                "SELECT last_check_at FROM source_state WHERE source = 'src-b'"
            ).fetchone()

        # src-a ran → last_check_at updated
        assert row_a is not None and row_a[0] is not None
        # src-b was NOT run → last_check_at unchanged (still `recent`)
        assert row_b is not None
        assert row_b[0][:16] == recent[:16]

    def test_run_all_runs_both_enabled_sources(self, monkeypatch, tmp_path):
        """--all runs both enabled sources regardless of schedule."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Make src-b appear to have just run (not due)
        real_now = datetime.now(tz=UTC)
        one_hour_ago = real_now.replace(hour=(real_now.hour - 1) % 24).isoformat()
        set_last_check_at(db_path, "src-b", one_hour_ago)

        result = main(["run", "--all"])
        assert result == 0

        with sqlite3.connect(db_path) as conn:
            src_a_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-a'"
            ).fetchone()[0]
            src_b_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-b'"
            ).fetchone()[0]
        assert src_a_runs == 1
        assert src_b_runs == 1

    def test_run_all_skips_disabled_source(self, monkeypatch, tmp_path, capsys):
        """--all still honours the enabled flag."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)

        # Disable src-b
        assert main(["pause", "src-b"]) == 0
        capsys.readouterr()

        result = main(["run", "--all"])
        assert result == 0

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            src_b_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-b'"
            ).fetchone()[0]
        assert src_b_runs == 0

        out = capsys.readouterr().out
        assert "disabled" in out

    def test_run_name_force_runs_disabled_source(self, monkeypatch, tmp_path, capsys):
        """kura run NAME --force runs a disabled source."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        assert main(["pause", "src-a"]) == 0
        capsys.readouterr()

        result = main(["run", "src-a", "--force"])
        assert result == 0

        with sqlite3.connect(db_path) as conn:
            src_a_runs = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'src-a'"
            ).fetchone()[0]
        assert src_a_runs == 1

    def test_run_name_disabled_without_force_fails(self, monkeypatch, tmp_path, capsys):
        """kura run NAME without --force on a disabled source exits 1."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        assert main(["pause", "src-a"]) == 0
        capsys.readouterr()

        result = main(["run", "src-a"])
        assert result == 1
        assert "disabled" in capsys.readouterr().out

    def test_run_no_args_exits_error(self, monkeypatch, tmp_path, capsys):
        """kura run with no arguments prints an error and exits 2."""
        set_kura_dirs(monkeypatch, tmp_path)
        result = main(["run"])
        assert result == 2

    def test_run_force_with_due_is_error(self, monkeypatch, tmp_path, capsys):
        """--force --due is an argparse error (exits 2)."""
        set_kura_dirs(monkeypatch, tmp_path)
        result = main(["run", "--force", "--due"])
        assert result == 2

    def test_run_force_with_all_is_error(self, monkeypatch, tmp_path, capsys):
        """--force --all is an argparse error (exits 2)."""
        set_kura_dirs(monkeypatch, tmp_path)
        result = main(["run", "--force", "--all"])
        assert result == 2


# ---------------------------------------------------------------------------
# Locking tests
# ---------------------------------------------------------------------------


class TestLocking:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        make_one_source_config(config_dir, out)
        assert main(["init"]) == 0

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123"])
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir

    def test_run_source_skipped_when_lock_held(self, monkeypatch, tmp_path, capsys):
        """kura run NAME exits 0 with skip message when source lock is held."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        with source_lock("test-src", state_dir) as acquired:
            assert acquired is True
            result = main(["run", "test-src"])

        assert result == 0
        out = capsys.readouterr().out
        assert "skipped" in out
        assert "test-src" in out

        # No runs row should have been inserted.
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT count(*) FROM runs WHERE source = 'test-src'"
            ).fetchone()[0]
        assert count == 0

    def test_import_skipped_when_lock_held(self, monkeypatch, tmp_path, capsys):
        """kura import NAME exits 1 with skip message when source lock is held."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)

        with source_lock("test-src", state_dir) as acquired:
            assert acquired is True
            result = main(["import", "test-src"])

        assert result == 1
        out = capsys.readouterr().out
        assert "skipped" in out or "already running" in out

    def test_import_does_not_touch_archive_when_lock_held(self, monkeypatch, tmp_path, capsys):
        """When import is skipped due to lock, archive file is not modified."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        out = tmp_path / "out"

        archive_path = out / "archive.txt"
        archive_path.write_text("youtube existing111\n")
        original_content = archive_path.read_text()

        with source_lock("test-src", state_dir) as acquired:
            assert acquired is True
            main(["import", "test-src"])

        # Archive must not have been touched.
        assert archive_path.read_text() == original_content

    def test_source_lock_acquired_when_free(self, tmp_path):
        """source_lock yields True when no other process holds the lock."""
        state_dir = tmp_path / "state"
        with source_lock("my-source", state_dir) as acquired:
            assert acquired is True

    def test_source_lock_not_acquired_when_held(self, tmp_path):
        """source_lock yields False when another context holds the lock."""
        state_dir = tmp_path / "state"
        with source_lock("my-source", state_dir) as first:
            assert first is True
            with source_lock("my-source", state_dir) as second:
                assert second is False

    def test_source_lock_released_after_context(self, tmp_path):
        """The lock is released after the context exits, allowing re-acquisition."""
        state_dir = tmp_path / "state"
        with source_lock("my-source", state_dir) as first:
            assert first is True
        # Lock should now be free.
        with source_lock("my-source", state_dir) as second:
            assert second is True


# ---------------------------------------------------------------------------
# pause / resume tests
# ---------------------------------------------------------------------------


class TestPauseResume:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        make_one_source_config(config_dir, out, enabled=True)
        assert main(["init"]) == 0
        return config_dir, state_dir

    def test_pause_flips_enabled_to_false(self, monkeypatch, tmp_path, capsys):
        """kura pause NAME sets enabled=false in config.yaml."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        result = main(["pause", "test-src"])
        assert result == 0

        from kura.config import load_config
        config = load_config()
        assert config.sources[0].enabled is False

        out = capsys.readouterr().out
        assert "disabled" in out

    def test_resume_flips_enabled_to_true(self, monkeypatch, tmp_path, capsys):
        """kura resume NAME sets enabled=true after pause."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        main(["pause", "test-src"])
        capsys.readouterr()

        result = main(["resume", "test-src"])
        assert result == 0

        from kura.config import load_config
        config = load_config()
        assert config.sources[0].enabled is True

        out = capsys.readouterr().out
        assert "enabled" in out

    def test_pause_preserves_yaml_comments(self, monkeypatch, tmp_path):
        """Comments in config.yaml survive a pause operation."""
        config_dir, _state_dir = self._setup(monkeypatch, tmp_path)
        config_path = config_dir / "config.yaml"

        # Add a comment to the config.
        text = config_path.read_text()
        config_path.write_text("# top-level comment\n" + text)

        main(["pause", "test-src"])

        new_text = config_path.read_text()
        assert "# top-level comment" in new_text

    def test_pause_already_paused_exits_0(self, monkeypatch, tmp_path, capsys):
        """Pausing an already-disabled source is idempotent and exits 0."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        main(["pause", "test-src"])
        capsys.readouterr()

        result = main(["pause", "test-src"])
        assert result == 0
        out = capsys.readouterr().out
        assert "already" in out

    def test_resume_already_enabled_exits_0(self, monkeypatch, tmp_path, capsys):
        """Resuming an already-enabled source is idempotent and exits 0."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        result = main(["resume", "test-src"])
        assert result == 0
        out = capsys.readouterr().out
        assert "already" in out

    def test_pause_unknown_source_exits_1(self, monkeypatch, tmp_path, capsys):
        """kura pause UNKNOWN exits 1 with an error message."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        result = main(["pause", "no-such-source"])
        assert result == 1
        out = capsys.readouterr().out
        assert "error" in out.lower() or "unknown" in out.lower()

    def test_resume_unknown_source_exits_1(self, monkeypatch, tmp_path, capsys):
        """kura resume UNKNOWN exits 1 with an error message."""
        _config_dir, _state_dir = self._setup(monkeypatch, tmp_path)

        result = main(["resume", "no-such-source"])
        assert result == 1
        out = capsys.readouterr().out
        assert "error" in out.lower() or "unknown" in out.lower()
