from __future__ import annotations

import os
import sqlite3
import stat
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kura.cli import main
from kura.config import Source
from kura.failures import missing_tabs
from kura.runner import _print_run_summary, build_command, run_source

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_run_summary_formats_duration_and_home_path(capsys) -> None:
    started = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    _print_run_summary(
        "test-channel",
        0,
        16,
        started,
        started + timedelta(minutes=8, seconds=12),
        Path.home() / ".local/state/kura/logs/test-channel/run.log",
    )
    assert capsys.readouterr().out == (
        "✓ test-channel — 16 new · 8m 12s · "
        "log ~/.local/state/kura/logs/test-channel/run.log\n"
    )


def test_failed_run_summary_omits_duration(capsys) -> None:
    started = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    _print_run_summary(
        "test-channel",
        3,
        0,
        started,
        started + timedelta(minutes=8),
        Path("/tmp/run.log"),
    )
    assert capsys.readouterr().out == "✗ test-channel — failed (exit 3) · log /tmp/run.log\n"


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


def make_config(config_dir: Path, output_dir: Path) -> None:
    """Write a minimal config.yaml with one source called 'test-src'."""
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
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )


def make_source(
    output_dir: Path,
    *,
    cookies: str | None = None,
    extra_args: list[str] | None = None,
) -> Source:
    return Source(
        name="test-src",
        url="https://example.com/channel",
        output_dir=output_dir,
        archive_file=output_dir / "archive.txt",
        profile="av1-archive",
        cookies=cookies,
        schedule="0 */6 * * *",
        update_strategy="full",
        full_reindex_every="7d",
        extra_args=extra_args or [],
        enabled=True,
    )


def write_fake_ytdlp(
    bin_dir: Path,
    exit_code: int = 0,
    archive_lines: list[str] | None = None,
    error_lines: list[str] | None = None,
) -> None:
    """Write a fake yt-dlp executable to *bin_dir*.

    The fake script:
    - Responds to ``--version`` with "2099.01.01".
    - When ``--simulate`` is present: prints 3 pre_process lines to stdout and exits.
    - Otherwise: appends *archive_lines* to the ``--download-archive`` file and
      prints *error_lines* verbatim before exiting.
    """
    archive_lines = archive_lines or []
    errors_repr = repr(error_lines or [])
    # Serialise archive lines as a Python list literal safe to embed in source.
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
            print("Youtube abc002 Second video")
            print("Youtube abc003 Third video")
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
        print("Downloading video def456")
        for line in {errors_repr}:
            print(line)
        sys.exit({exit_code})
    """)

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Tests 1–4: build_command
# ---------------------------------------------------------------------------


def test_build_command_no_cookies(tmp_path: Path) -> None:
    """build_command with no cookies produces the minimal argv."""
    output_dir = tmp_path / "output"
    source = make_source(output_dir)
    cmd = build_command(source, "/path/to/profile.conf")
    assert cmd == [
        "yt-dlp",
        "--ignore-config",
        "--config-locations", "/path/to/profile.conf",
        "--download-archive", str(source.archive_file),
        "https://example.com/channel",
    ]


def test_build_command_cookies_string(tmp_path: Path) -> None:
    """build_command with cookies= inserts the split args after --download-archive."""
    output_dir = tmp_path / "output"
    source = make_source(output_dir, cookies="--cookies-from-browser firefox")
    cmd = build_command(source, "/path/to/profile.conf")
    archive_idx = cmd.index("--download-archive")
    # cookies args follow immediately after the archive value
    assert "--cookies-from-browser" in cmd
    assert "firefox" in cmd
    # url is last
    assert cmd[-1] == source.url
    # cookies appear before the url
    cookie_idx = cmd.index("--cookies-from-browser")
    url_idx = cmd.index(source.url)
    assert cookie_idx < url_idx
    # ensure both expected cookie tokens are present consecutively
    assert cmd[cookie_idx + 1] == "firefox"


def test_build_command_extra_args(tmp_path: Path) -> None:
    """build_command appends extra_args (list[str]) before the URL."""
    output_dir = tmp_path / "output"
    source = make_source(output_dir, extra_args=["--max-downloads", "5"])
    cmd = build_command(source, "/path/to/profile.conf")
    assert "--max-downloads" in cmd
    assert "5" in cmd
    # extra_args appear before URL
    extra_idx = cmd.index("--max-downloads")
    url_idx = cmd.index(source.url)
    assert extra_idx < url_idx


def test_build_command_simulate(tmp_path: Path) -> None:
    """build_command with simulate=True appends the simulate flags before the URL."""
    output_dir = tmp_path / "output"
    source = make_source(output_dir)
    cmd = build_command(source, "/path/to/profile.conf", simulate=True)
    assert "--simulate" in cmd
    assert "--skip-download" in cmd
    assert "--print" in cmd
    print_idx = cmd.index("--print")
    assert "pre_process:" in cmd[print_idx + 1]
    # simulate flags appear before URL
    sim_idx = cmd.index("--simulate")
    url_idx = cmd.index(source.url)
    assert sim_idx < url_idx
    # URL is last
    assert cmd[-1] == source.url


# ---------------------------------------------------------------------------
# Tests 5–6: run_source end-to-end
# ---------------------------------------------------------------------------


def test_run_source_exit0_two_archive_lines(monkeypatch, tmp_path: Path, capsys) -> None:
    """Fake yt-dlp exits 0 and appends 2 archive lines; DB row reflects that."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123", "youtube def456"])
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "✓ test-src — 2 new" in captured.out

    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT exit_code, new_count, ytdlp_version, log_path FROM runs WHERE source = 'test-src'"
        ).fetchone()
        assert row is not None
        assert row[0] == 0        # exit_code
        assert row[1] == 2        # new_count
        assert row[2] == "2099.01.01"  # ytdlp_version
        log_path = row[3]
        assert log_path is not None
        assert Path(log_path).exists()

        # Log file first line must contain the argv.
        first_line = Path(log_path).read_text(encoding="utf-8").splitlines()[0]
        assert "yt-dlp" in first_line
        assert "started_at=" in first_line

        ss_row = conn.execute(
            "SELECT last_check_at, last_success_at, last_full_run_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
        assert ss_row is not None
        assert ss_row[0] is not None  # last_check_at
        assert ss_row[1] is not None  # last_success_at
        assert ss_row[2] is not None  # last_full_run_at


def test_run_source_exit3_no_archive_lines(monkeypatch, tmp_path: Path, capsys) -> None:
    """Fake yt-dlp exits 3 and writes nothing; DB row reflects failure."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(bin_dir, exit_code=3, archive_lines=[])
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir)
    assert exit_code == 3  # raw exit code returned
    # caller would do sys.exit(min(1, 3)) == 1

    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT exit_code, new_count FROM runs WHERE source = 'test-src'"
        ).fetchone()
        assert row is not None
        assert row[0] == 3   # exit_code
        assert row[1] == 0   # new_count

        ss_row = conn.execute(
            "SELECT last_check_at, last_success_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
        assert ss_row is not None
        assert ss_row[0] is not None  # last_check_at set
        assert ss_row[1] is None      # last_success_at NOT set


# ---------------------------------------------------------------------------
# Test 7: verify — fake yt-dlp prints 3 lines
# ---------------------------------------------------------------------------


def test_verify_fake_ytdlp_three_lines(monkeypatch, tmp_path: Path, capsys) -> None:
    """verify command counts 3 pending items from fake yt-dlp stdout."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(bin_dir, exit_code=0)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["verify", "test-src"])
    assert result == 0

    captured = capsys.readouterr()
    assert "test-src — 3 pending" in captured.out
    assert "  abc001  First video" in captured.out


# ---------------------------------------------------------------------------
# Test 8: status table
# ---------------------------------------------------------------------------


def test_status_table(monkeypatch, tmp_path: Path, capsys) -> None:
    """After a successful run, status shows source with populated columns."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123"])
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]
    run_source(source, config_dir, state_dir)
    capsys.readouterr()  # discard run output

    result = main(["status"])
    assert result == 0

    captured = capsys.readouterr()
    output = captured.out

    # Source name appears in table.
    assert "test-src" in output
    assert "● test-src" in output
    assert "✓ +1 new" in output
    # next_due is a parseable timestamp or "now".
    lines = [line for line in output.splitlines() if "test-src" in line]
    assert len(lines) == 1
    source_line = lines[0]
    # The line should contain a date or "now".
    assert "next in" in source_line or "next now" in source_line


# ---------------------------------------------------------------------------
# Missing channel tabs — yt-dlp exits 1 but the run is healthy
# ---------------------------------------------------------------------------

_NO_SHORTS = "ERROR: [youtube:tab] @somechannel: This channel does not have a shorts tab"
_NO_STREAMS = "ERROR: [youtube:tab] @somechannel: This channel does not have a streams tab"


def test_missing_tabs_detects_each_absent_tab() -> None:
    """Both absent tabs are reported, in the order yt-dlp printed them."""
    lines = ["[youtube:tab] Downloading page 1\n", _NO_SHORTS + "\n", _NO_STREAMS + "\n"]
    assert missing_tabs(lines) == ["shorts", "streams"]


def test_missing_tabs_none_when_no_tab_errors() -> None:
    """A clean run has nothing to forgive."""
    assert missing_tabs(["Downloading video abc123\n"]) is None


def test_missing_tabs_none_when_another_error_present() -> None:
    """A real error alongside a missing tab must keep the run's failure."""
    lines = [
        _NO_STREAMS + "\n",
        "ERROR: [youtube] abc123: Video unavailable\n",
    ]
    assert missing_tabs(lines) is None


def test_missing_tabs_ignores_non_error_mentions() -> None:
    """Only ERROR: lines are considered, so a title cannot spoof the check."""
    lines = ["[download] This channel does not have a streams tab.mp4\n"]
    assert missing_tabs(lines) is None


def test_run_source_missing_tabs_recorded_as_success(monkeypatch, tmp_path: Path, capsys) -> None:
    """yt-dlp exits 1 solely over absent tabs → run counts as a success."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(
        bin_dir,
        exit_code=1,
        archive_lines=["youtube abc123"],
        error_lines=[_NO_SHORTS, _NO_STREAMS],
    )
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    source = load_config().sources[0]

    assert run_source(source, config_dir, state_dir) == 0

    captured = capsys.readouterr()
    assert "no shorts, streams tab on this channel; skipped" in captured.out

    with sqlite3.connect(state_dir / "state.db") as conn:
        row = conn.execute(
            "SELECT exit_code, new_count FROM runs WHERE source = 'test-src'"
        ).fetchone()
        assert row == (0, 1)

        ss_row = conn.execute(
            "SELECT last_success_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
        assert ss_row[0] is not None  # the source is not marked as failing


def test_run_source_real_error_keeps_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    """An unrelated error alongside a missing tab keeps the nonzero exit code."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(
        bin_dir,
        exit_code=1,
        archive_lines=[],
        error_lines=[_NO_STREAMS, "ERROR: [youtube] abc123: Video unavailable"],
    )
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    source = load_config().sources[0]

    assert run_source(source, config_dir, state_dir) == 1

    with sqlite3.connect(state_dir / "state.db") as conn:
        ss_row = conn.execute(
            "SELECT last_success_at FROM source_state WHERE source = 'test-src'"
        ).fetchone()
        assert ss_row[0] is None
