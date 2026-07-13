"""Tests for logs, doctor, concurrency, local-time display, and --version."""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import textwrap
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from kura.cli import main


# ---------------------------------------------------------------------------
# Shared helpers (mirrors earlier test modules)
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


def make_config_yaml(
    config_dir: Path,
    output_dir: Path,
    *,
    url: str = "https://example.com/ch",
    update_strategy: str = "full",
    concurrency: int | None = None,
) -> None:
    write_profile(config_dir)
    extra = f"\n  concurrency: {concurrency}" if concurrency is not None else ""
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: {update_strategy}
  full_reindex_every: 7d{extra}
sources:
  - name: test-src
    url: {url}
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )


def make_multi_source_config(
    config_dir: Path,
    sources: list[tuple[str, Path]],
    *,
    concurrency: int = 1,
) -> None:
    """Write config with multiple sources."""
    write_profile(config_dir)
    source_items = ""
    for name, out in sources:
        source_items += f"""  - name: {name}
    url: https://example.com/{name}
    output_dir: {out}
    extra_args: []
    enabled: true
"""
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
  concurrency: {concurrency}
sources:
{source_items}"""
    )


def write_fake_ytdlp(
    bin_dir: Path,
    exit_code: int = 0,
    *,
    version: str = "2099.01.01",
    sleep_secs: float = 0.0,
    archive_lines: list[str] | None = None,
    extra_output: str = "",
) -> None:
    """Write a minimal fake yt-dlp to bin_dir."""
    archive_lines = archive_lines or []
    lines_repr = repr(archive_lines)
    extra_repr = repr(extra_output)
    sleep_repr = repr(sleep_secs)
    version_repr = repr(version)

    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys, time

        args = sys.argv[1:]

        if args == ["--version"]:
            print({version_repr})
            sys.exit(0)

        if "--simulate" in args:
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

        extra = {extra_repr}
        if extra:
            print(extra, flush=True)
        time.sleep({sleep_repr})
        print("Downloading video abc123", flush=True)
        sys.exit({exit_code})
    """)

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_ffmpeg(bin_dir: Path) -> None:
    script = "#!/bin/sh\nexit 0\n"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "ffmpeg"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_systemctl(bin_dir: Path, *, active: bool = True) -> None:
    status = "active" if active else "inactive"
    script = textwrap.dedent(f"""\
        #!/bin/sh
        echo "{status}"
        exit 0
    """)
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "systemctl"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def inject_run_row(
    db_path: Path,
    source: str,
    log_path: str,
    *,
    started_at: str = "2024-06-01T10:00:00+00:00",
    ended_at: str = "2024-06-01T10:05:00+00:00",
    exit_code: int = 0,
    new_count: int = 3,
) -> None:
    import kura.state as st
    st.init_db(db_path)
    with st.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (source, started_at, ended_at, exit_code, ytdlp_version,
                              new_count, log_path, failure_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (source, started_at, ended_at, exit_code, "2099.01.01", new_count, log_path),
        )


# ---------------------------------------------------------------------------
# 1. kura logs
# ---------------------------------------------------------------------------


class TestLogsCommand:
    def _setup(self, monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0
        return config_dir, state_dir

    def _write_log(self, state_dir: Path, source: str, ts: str, content: str) -> Path:
        """Write a fake log file and return its path."""
        log_dir = state_dir / "logs" / source
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{ts}.log"
        log_path.write_text(content, encoding="utf-8")
        return log_path

    def test_unknown_source_exits_1(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        rc = main(["logs", "no-such-source"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "✗ unknown source" in out

    def test_no_logs_yet_exits_0(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        rc = main(["logs", "test-src"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no logs" in out.lower()

    def test_no_log_dir_exits_0(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Graceful when log directory for source doesn't exist at all."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        # Don't create the log dir for the source.
        rc = main(["logs", "test-src"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no logs" in out.lower()

    def test_prints_latest_log_content(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        # Write two log files; the lexicographically later one should be shown.
        self._write_log(state_dir, "test-src", "20240601T100000Z", "old log line\n")
        self._write_log(state_dir, "test-src", "20240602T100000Z", "new log line\n")

        rc = main(["logs", "test-src"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "new log line" in out
        assert "old log line" not in out

    def test_selects_most_recent_log_across_multiple(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        for ts in ["20240601T100000Z", "20240602T100000Z", "20240603T100000Z"]:
            self._write_log(state_dir, "test-src", ts, f"log from {ts}\n")

        rc = main(["logs", "test-src"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "20240603T100000Z" in out
        assert "20240601T100000Z" not in out
        assert "20240602T100000Z" not in out

    def test_n_limits_lines(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        content = "\n".join(f"line {i}" for i in range(1, 101))  # 100 lines
        self._write_log(state_dir, "test-src", "20240601T100000Z", content)

        rc = main(["logs", "test-src", "-n", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 5
        # Should show the LAST 5 lines (lines 96-100).
        assert "line 96" in out
        assert "line 100" in out
        # Should NOT show early lines (use full word boundary check)
        assert "line 5\n" not in out
        assert "line 50\n" not in out

    def test_default_n_is_50(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        content = "\n".join(f"line {i}" for i in range(1, 201))  # 200 lines
        self._write_log(state_dir, "test-src", "20240601T100000Z", content)

        rc = main(["logs", "test-src"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 50
        assert "line 151" in out  # first of last 50 from 200

    def test_path_flag_prints_absolute_path(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        log_path = self._write_log(state_dir, "test-src", "20240601T100000Z", "content\n")

        rc = main(["logs", "test-src", "--path"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == str(log_path.resolve())

    def test_path_flag_no_logs(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        rc = main(["logs", "test-src", "--path"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no logs" in out.lower()

    def test_n_larger_than_file_shows_all(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        content = "only three\nlines here\nend"
        self._write_log(state_dir, "test-src", "20240601T100000Z", content)

        rc = main(["logs", "test-src", "-n", "1000"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "only three" in out
        assert "end" in out


# ---------------------------------------------------------------------------
# 2. kura doctor
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    def _setup_healthy(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        ytdlp_version: str = "2099.01.01",
        systemctl_active: bool = True,
    ) -> tuple[Path, Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, version=ytdlp_version)
        write_fake_ffmpeg(bin_dir)
        write_fake_systemctl(bin_dir, active=systemctl_active)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir, output_dir

    def test_healthy_setup_exits_0(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✗" not in out
        assert "✓" in out

    def test_healthy_setup_reports_ytdlp_ok(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        main(["doctor"])
        out = capsys.readouterr().out
        assert "✓ yt-dlp" in out

    def test_healthy_setup_reports_ffmpeg_ok(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        main(["doctor"])
        out = capsys.readouterr().out
        assert "✓ ffmpeg" in out

    def test_missing_ffmpeg_exits_1_with_fail(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0
        capsys.readouterr()  # flush init output

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir)
        # Do NOT write fake ffmpeg — and use a clean PATH with no system tools.
        write_fake_systemctl(bin_dir)
        monkeypatch.setenv("PATH", str(bin_dir))

        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "✗" in out
        assert "ffmpeg" in out

    def test_missing_ytdlp_exits_1_with_fail(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0
        capsys.readouterr()  # flush init output

        bin_dir = tmp_path / "bin"
        write_fake_ffmpeg(bin_dir)
        write_fake_systemctl(bin_dir)
        # Ensure a clean PATH with no real yt-dlp or ffmpeg
        monkeypatch.setenv("PATH", str(bin_dir))

        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "✗" in out
        assert "yt-dlp" in out

    def test_stale_ytdlp_version_warns_but_exits_0(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """A yt-dlp version > 90 days old → warning but no failure, exit 0."""
        self._setup_healthy(monkeypatch, tmp_path, ytdlp_version="2024.01.01")
        capsys.readouterr()  # flush init output
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✗" not in out
        assert "! yt-dlp version" in out
        assert "old" in out or "days" in out

    def test_recent_ytdlp_version_no_warn(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """A yt-dlp version < 90 days old → no version warning."""
        # Use a version from 30 days ago.
        import datetime as _dt
        recent = date.today() - _dt.timedelta(days=30)
        ver = recent.strftime("%Y.%m.%d")
        self._setup_healthy(monkeypatch, tmp_path, ytdlp_version=ver)
        capsys.readouterr()  # flush init output
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✗" not in out
        # Should NOT warn about age
        lines_with_old = [line for line in out.splitlines() if "days old" in line]
        assert lines_with_old == []

    def test_broken_config_exits_1_fail_skips_per_source(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        config_dir.mkdir(parents=True, exist_ok=True)
        # Write a config with a missing required field (triggers ConfigError).
        write_profile(config_dir)
        (config_dir / "config.yaml").write_text(
            """defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: INVALID_DURATION
sources: []
"""
        )

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir)
        write_fake_ffmpeg(bin_dir)
        write_fake_systemctl(bin_dir)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "✗" in out
        assert "config" in out.lower()

    def test_cookies_file_missing_exits_1_fail(  # noqa: C901
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        write_profile(config_dir)
        missing_cookie = tmp_path / "cookies.txt"
        (config_dir / "config.yaml").write_text(
            f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://example.com/ch
    output_dir: {output_dir}
    cookies: "--cookies {missing_cookie}"
    extra_args: []
    enabled: true
"""
        )
        assert main(["init"]) == 0
        capsys.readouterr()  # flush init output

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir)
        write_fake_ffmpeg(bin_dir)
        write_fake_systemctl(bin_dir)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "✗" in out
        assert "cookies" in out.lower()

    def test_cookies_from_browser_reports_ok(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        write_profile(config_dir)
        (config_dir / "config.yaml").write_text(
            f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://example.com/ch
    output_dir: {output_dir}
    cookies: "--cookies-from-browser firefox"
    extra_args: []
    enabled: true
"""
        )
        assert main(["init"]) == 0
        capsys.readouterr()  # flush init output

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir)
        write_fake_ffmpeg(bin_dir)
        write_fake_systemctl(bin_dir)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✗" not in out
        assert (
            "✓ test-src — output dir writable · archive not created · cookies from firefox"
            in out
        )

    def test_held_lock_reports_running(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """When a source lock is held, doctor reports 'appears to be running'."""
        from kura.locks import source_lock
        from kura import paths

        self._setup_healthy(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output

        with source_lock("test-src", paths.state_dir()) as acquired:
            assert acquired is True
            rc = main(["doctor"])

        out = capsys.readouterr().out
        assert rc == 0  # held lock is a warning, not a failure
        assert "appears to be running" in out

    def test_systemctl_inactive_warns(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path, systemctl_active=False)
        capsys.readouterr()  # flush init output
        rc = main(["doctor"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "! kura.timer" in out
        assert "✗" not in out
        assert "kura.timer" in out

    def test_systemctl_active_reports_ok(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path, systemctl_active=True)
        capsys.readouterr()  # flush init output
        main(["doctor"])
        out = capsys.readouterr().out
        assert "✓ kura.timer is active" in out

    def test_db_integrity_reported(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup_healthy(monkeypatch, tmp_path)
        capsys.readouterr()  # flush init output
        main(["doctor"])
        out = capsys.readouterr().out
        assert "state.db" in out
        assert "✗" not in out


# ---------------------------------------------------------------------------
# 3. Concurrency config validation
# ---------------------------------------------------------------------------


class TestConcurrencyConfig:
    def _config_dir(self, tmp_path: Path) -> Path:
        write_profile(tmp_path)
        return tmp_path

    def test_concurrency_default_is_1(self, tmp_path: Path) -> None:
        from kura.config import config_from_data
        config_dir = self._config_dir(tmp_path)
        cfg = config_from_data(
            {
                "defaults": {
                    "profile": "av1-archive",
                    "schedule": "0 */6 * * *",
                    "update_strategy": "full",
                    "full_reindex_every": "7d",
                },
                "sources": [],
            },
            config_dir,
        )
        assert cfg.defaults["concurrency"] == 1

    def test_concurrency_valid_range(self, tmp_path: Path) -> None:
        from kura.config import config_from_data
        config_dir = self._config_dir(tmp_path)
        for n in (1, 4, 8):
            cfg = config_from_data(
                {
                    "defaults": {
                        "profile": "av1-archive",
                        "schedule": "0 */6 * * *",
                        "update_strategy": "full",
                        "full_reindex_every": "7d",
                        "concurrency": n,
                    },
                    "sources": [],
                },
                config_dir,
            )
            assert cfg.defaults["concurrency"] == n

    @pytest.mark.parametrize("bad_value", [0, 9, "x", -1, None])
    def test_concurrency_invalid_raises(self, bad_value, tmp_path: Path) -> None:
        from kura.config import config_from_data, ConfigError
        config_dir = self._config_dir(tmp_path)
        with pytest.raises(ConfigError, match="concurrency"):
            config_from_data(
                {
                    "defaults": {
                        "profile": "av1-archive",
                        "schedule": "0 */6 * * *",
                        "update_strategy": "full",
                        "full_reindex_every": "7d",
                        "concurrency": bad_value,
                    },
                    "sources": [],
                },
                config_dir,
            )


# ---------------------------------------------------------------------------
# 4. Concurrent run (multi-source with concurrency > 1)
# ---------------------------------------------------------------------------


class TestConcurrentRun:
    def _setup(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        concurrency: int = 2,
        num_sources: int = 3,
        sleep_secs: float = 0.0,
        extra_output: str = "",
    ) -> tuple[Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        sources = []
        for i in range(num_sources):
            out = tmp_path / f"out{i}"
            out.mkdir()
            sources.append((f"src-{i}", out))

        make_multi_source_config(config_dir, sources, concurrency=concurrency)
        assert main(["init"]) == 0

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, sleep_secs=sleep_secs, extra_output=extra_output)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir

    def test_all_sources_run_with_concurrency(self, monkeypatch, tmp_path: Path) -> None:
        """With concurrency 2 and 3 sources, all 3 complete successfully."""
        config_dir, state_dir = self._setup(monkeypatch, tmp_path, concurrency=2, num_sources=3)

        rc = main(["run", "--all"])
        assert rc == 0

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            for i in range(3):
                count = conn.execute(
                    "SELECT count(*) FROM runs WHERE source = ?",
                    (f"src-{i}",),
                ).fetchone()[0]
                assert count == 1, f"src-{i} should have 1 run row"

    def test_concurrent_summaries_printed(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Concurrent run prints summary lines for each source."""
        self._setup(monkeypatch, tmp_path, concurrency=2, num_sources=3)

        rc = main(["run", "--all"])
        assert rc == 0

        out = capsys.readouterr().out
        for i in range(3):
            assert f"src-{i}" in out

    def test_quiet_mode_suppresses_streaming_output(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Concurrent run prints summaries without streaming downloader output."""
        known_line = "UNIQUE_STREAMING_LINE_XYZ"
        self._setup(
            monkeypatch,
            tmp_path,
            concurrency=2,
            num_sources=2,
            extra_output=known_line,
        )

        main(["run", "--all"])
        out = capsys.readouterr().out

        # The streaming line should NOT appear on stdout (quiet mode).
        assert known_line not in out
        assert "✓ src-0 — nothing new" in out
        assert "✓ src-1 — nothing new" in out

    def test_quiet_mode_output_in_log_file(self, monkeypatch, tmp_path: Path) -> None:
        """In concurrent mode, per-line output still written to log file."""
        known_line = "UNIQUE_LOG_ONLY_LINE_ABC"
        config_dir, state_dir = self._setup(
            monkeypatch,
            tmp_path,
            concurrency=2,
            num_sources=2,
            extra_output=known_line,
        )

        main(["run", "--all"])

        # Find log files for src-0.
        log_dir = state_dir / "logs" / "src-0"
        assert log_dir.is_dir()
        log_files = list(log_dir.glob("*.log"))
        assert len(log_files) >= 1
        log_content = log_files[0].read_text(encoding="utf-8")
        assert known_line in log_content

    def test_single_source_not_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """kura run NAME always streams output regardless of concurrency setting."""
        known_line = "SERIAL_STREAMING_OUTPUT_QRS"
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, concurrency=4)
        assert main(["init"]) == 0

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, extra_output=known_line)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        main(["run", "test-src"])
        out = capsys.readouterr().out
        assert known_line in out  # should NOT be suppressed for single source

    def test_failed_source_propagates_exit_1(self, monkeypatch, tmp_path: Path) -> None:
        """If any concurrent source fails, the overall exit is 1."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        sources = [(f"src-{i}", tmp_path / f"out{i}") for i in range(2)]
        for _, d in sources:
            d.mkdir()
        make_multi_source_config(config_dir, sources, concurrency=2)
        assert main(["init"]) == 0

        # Write a yt-dlp that exits 1 (failure).
        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, exit_code=1)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        rc = main(["run", "--all"])
        assert rc == 1


# ---------------------------------------------------------------------------
# 5. Local time display
# ---------------------------------------------------------------------------


class TestLocalTimeDisplay:
    def _setup(self, monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0
        return config_dir, state_dir

    def test_status_timezone_footnote_dropped(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """kura status uses self-explanatory relative times without a footer."""
        self._setup(monkeypatch, tmp_path)
        main(["status"])
        out = capsys.readouterr().out
        assert "kura · 1 source" in out

    def test_status_does_not_show_timezone_abbreviation(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """The overview no longer needs a timezone abbreviation."""
        self._setup(monkeypatch, tmp_path)
        expected_tz = datetime.now().astimezone().tzname()
        main(["status"])
        out = capsys.readouterr().out
        assert expected_tz not in out

    def test_status_detail_uses_self_explanatory_next_time(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """kura status NAME shows a relative next-check line."""
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path)
        main(["status", "test-src"])
        out = capsys.readouterr().out
        assert "next check" in out.lower()

    def test_local_ts_converts_utc_correctly(self) -> None:
        """ui.local_ts converts a known UTC string to local time."""
        from kura.ui import local_ts

        # A fixed UTC time
        utc_str = "2024-06-01T12:00:00+00:00"
        result = local_ts(utc_str, "%Y-%m-%d %H:%M")

        # Re-derive the expected local string.
        dt = datetime.fromisoformat(utc_str)
        expected = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        assert result == expected

    def test_local_ts_handles_naive_utc(self) -> None:
        """ui.local_ts treats naive datetimes as UTC and converts."""
        from kura.ui import local_ts

        # Naive UTC string (no tz suffix) as stored in older rows.
        utc_naive = "2024-06-01T12:00:00"
        result = local_ts(utc_naive, "%Y-%m-%d %H:%M")
        # Should not raise; result should be a formatted string.
        assert len(result) == 16
        assert "T" not in result  # was replaced

    def test_run_due_not_due_shows_local_time(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """'not due' output shows time in local timezone, not raw UTC."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir)
        assert main(["init"]) == 0

        # Set last_check_at to 1 hour ago so it's not due (6h schedule).
        import kura.state as st
        db_path = state_dir / "state.db"
        real_now = datetime.now(tz=timezone.utc)
        one_hour_ago = real_now.replace(hour=(real_now.hour - 1) % 24).isoformat()
        with st.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO source_state (source, last_check_at)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET last_check_at = excluded.last_check_at
                """,
                ("test-src", one_hour_ago),
            )

        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        main(["run", "--due"])
        out = capsys.readouterr().out

        assert "not due (next in" in out


# ---------------------------------------------------------------------------
# 6. --version flag
# ---------------------------------------------------------------------------


class TestVersionFlag:
    def test_version_exits_0(self, monkeypatch, tmp_path: Path) -> None:
        set_kura_dirs(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_version_output_contains_kura(self, monkeypatch, tmp_path: Path, capsys) -> None:
        set_kura_dirs(monkeypatch, tmp_path)
        with pytest.raises(SystemExit):
            main(["--version"])
        out = capsys.readouterr().out
        assert "kura" in out
