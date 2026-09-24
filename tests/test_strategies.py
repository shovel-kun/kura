"""Tests for update strategies, failure classification, and suppress/unsuppress."""
from __future__ import annotations

import os
import sqlite3
import stat
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kura.archive import read_archive
from kura.cli import main
from kura.config import ConfigError, Source, config_from_data, parse_duration
from kura.failures import classify_line
from kura.runner import _QUICK_FLAGS, _channel_tab_urls, build_command

# ---------------------------------------------------------------------------
# Shared helpers
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


def make_channel_source(
    output_dir: Path,
    *,
    update_strategy: str = "quick",
    full_reindex_every: str = "7d",
    url: str = "https://www.youtube.com/@TestChannel",
) -> Source:
    return Source(
        name="test-src",
        url=url,
        output_dir=output_dir,
        archive_file=output_dir / "archive.txt",
        profile="av1-archive",
        cookies=None,
        schedule="0 */6 * * *",
        update_strategy=update_strategy,
        full_reindex_every=full_reindex_every,
        extra_args=[],
        enabled=True,
    )


def make_config_yaml(
    config_dir: Path,
    output_dir: Path,
    *,
    url: str = "https://www.youtube.com/@TestChannel",
    update_strategy: str = "quick",
) -> None:
    write_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: {update_strategy}
  full_reindex_every: 7d
sources:
  - name: test-src
    url: {url}
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )


def write_fake_ytdlp(
    bin_dir: Path,
    exit_code: int = 0,
    archive_lines: list[str] | None = None,
    *,
    extra_output: str = "",
    record_argv_to: Path | None = None,
    print_channel_id: str | None = None,
) -> None:
    """Write a fake yt-dlp executable.

    When *record_argv_to* is set, the fake script records sys.argv to that file
    on each invocation (used to assert which URLs/flags were passed).

    When *print_channel_id* is set, a ``--print`` invocation returns that value
    (used for channel_id resolution in RSS tests).
    """
    archive_lines = archive_lines or []
    lines_repr = repr(archive_lines)
    extra_repr = repr(extra_output)
    argv_path = repr(str(record_argv_to)) if record_argv_to else repr(None)
    channel_id_repr = repr(print_channel_id)

    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys, json

        args = sys.argv[1:]

        # Record argv if requested.
        argv_path = {argv_path}
        if argv_path is not None:
            with open(argv_path, "a") as f:
                f.write(json.dumps(args) + "\\n")

        if args == ["--version"]:
            print("2099.01.01")
            sys.exit(0)

        # channel_id resolution call: --print %(channel_id)s --playlist-items 1 URL
        channel_id_val = {channel_id_repr}
        if "--print" in args and "--playlist-items" in args:
            if channel_id_val is not None:
                print(channel_id_val)
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

        extra = {extra_repr}
        if extra:
            print(extra)
        print("Downloading video abc123")
        sys.exit({exit_code})
    """)

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def set_last_full_run_at(db_path: Path, name: str, ts: str) -> None:
    import kura.state as st
    with st.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, last_full_run_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_full_run_at = excluded.last_full_run_at
            """,
            (name, ts),
        )


# ---------------------------------------------------------------------------
# 1. Duration parser
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_days(self):
        assert parse_duration("7d") == 7 * 86400

    def test_hours(self):
        assert parse_duration("12h") == 12 * 3600

    def test_one_day(self):
        assert parse_duration("1d") == 86400

    def test_invalid_no_unit(self):
        with pytest.raises(ConfigError, match="invalid duration"):
            parse_duration("7")

    def test_invalid_bad_unit(self):
        with pytest.raises(ConfigError, match="invalid duration"):
            parse_duration("7m")

    def test_invalid_empty(self):
        with pytest.raises(ConfigError, match="invalid duration"):
            parse_duration("")

    def test_invalid_letters_first(self):
        with pytest.raises(ConfigError, match="invalid duration"):
            parse_duration("d7")


# ---------------------------------------------------------------------------
# 2. Strategy validation
# ---------------------------------------------------------------------------


class TestStrategyValidation:
    def _config_dir(self, tmp_path: Path) -> Path:
        write_profile(tmp_path)
        return tmp_path

    def _valid_source(self, **overrides) -> dict:
        src = {
            "name": "my-channel",
            "url": "https://www.youtube.com/@TestChannel",
            "output_dir": "/archive/test",
            "extra_args": [],
            "enabled": True,
        }
        src.update(overrides)
        return src

    def _base_config(self, source: dict) -> dict:
        return {
            "defaults": {
                "profile": "av1-archive",
                "schedule": "0 */6 * * *",
                "update_strategy": "full",
                "full_reindex_every": "7d",
            },
            "sources": [source],
        }

    def test_quick_valid_for_channel_url(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        cfg = config_from_data(
            self._base_config(self._valid_source(update_strategy="quick")),
            config_dir,
        )
        assert cfg.sources[0].update_strategy == "quick"

    def test_quick_invalid_for_non_channel_url(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        with pytest.raises(ConfigError, match="quick update_strategy requires a YouTube channel URL"):
            config_from_data(
                self._base_config(
                    self._valid_source(
                        update_strategy="quick",
                        url="https://example.com/playlist",
                    )
                ),
                config_dir,
            )

    def test_full_always_valid(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        cfg = config_from_data(
            self._base_config(
                self._valid_source(update_strategy="full", url="https://example.com/anything")
            ),
            config_dir,
        )
        assert cfg.sources[0].update_strategy == "full"

    def test_rss_invalid_for_non_channel_url(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        with pytest.raises(ConfigError, match="rss update_strategy requires a YouTube channel URL"):
            config_from_data(
                self._base_config(
                    self._valid_source(
                        update_strategy="rss",
                        url="https://www.youtube.com/playlist?list=PL123",
                    )
                ),
                config_dir,
            )

    def test_invalid_full_reindex_every_raises(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        data = self._base_config(self._valid_source(full_reindex_every="bad"))
        with pytest.raises(ConfigError, match="full_reindex_every"):
            config_from_data(data, config_dir)

    def test_per_source_full_reindex_every(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path)
        cfg = config_from_data(
            self._base_config(self._valid_source(full_reindex_every="3d")),
            config_dir,
        )
        assert cfg.sources[0].full_reindex_every == "3d"


# ---------------------------------------------------------------------------
# 3. build_command — quick strategy produces tab URLs + flags
# ---------------------------------------------------------------------------


class TestBuildCommandStrategy:
    def _make_quick_source(self, output_dir: Path) -> Source:
        return make_channel_source(output_dir, update_strategy="quick")

    def _make_full_source(self, output_dir: Path) -> Source:
        return Source(
            name="test-src",
            url="https://example.com/channel",
            output_dir=output_dir,
            archive_file=output_dir / "archive.txt",
            profile="av1-archive",
            cookies=None,
            schedule="0 */6 * * *",
            update_strategy="full",
            full_reindex_every="7d",
            extra_args=[],
            enabled=True,
        )

    def test_quick_three_tab_urls(self, tmp_path: Path) -> None:
        """build_command with quick strategy_args + tab URLs ends with 3 URLs."""
        output_dir = tmp_path / "out"
        source = self._make_quick_source(output_dir)
        tab_urls = _channel_tab_urls(source.url)
        cmd = build_command(source, "/path/to/profile.conf", strategy_args=_QUICK_FLAGS, urls=tab_urls)

        # Last 3 tokens are the tab URLs.
        assert cmd[-3:] == [
            "https://www.youtube.com/@TestChannel/videos",
            "https://www.youtube.com/@TestChannel/shorts",
            "https://www.youtube.com/@TestChannel/streams",
        ]

    def test_quick_contains_required_flags(self, tmp_path: Path) -> None:
        """Quick command uses a bounded window, not break-on-existing.

        break-on-existing stops at the first archived entry and cannot see a
        hole in the archive below newer uploads; the bounded --playlist-items
        window examines the newest _QUICK_WINDOW entries per tab regardless.
        """
        output_dir = tmp_path / "out"
        source = self._make_quick_source(output_dir)
        tab_urls = _channel_tab_urls(source.url)
        cmd = build_command(source, "/path/to/profile.conf", strategy_args=_QUICK_FLAGS, urls=tab_urls)

        assert "--lazy-playlist" in cmd
        window_idx = cmd.index("--playlist-items")
        assert cmd[window_idx + 1] == "1:50"
        assert "--break-on-existing" not in cmd
        assert "--break-per-input" not in cmd

    def test_quick_flags_before_urls(self, tmp_path: Path) -> None:
        """Quick flags appear before the tab URLs."""
        output_dir = tmp_path / "out"
        source = self._make_quick_source(output_dir)
        tab_urls = _channel_tab_urls(source.url)
        cmd = build_command(source, "/path/to/profile.conf", strategy_args=_QUICK_FLAGS, urls=tab_urls)

        lp_idx = cmd.index("--lazy-playlist")
        first_url_idx = cmd.index(tab_urls[0])
        assert lp_idx < first_url_idx

    def test_full_unchanged_single_url(self, tmp_path: Path) -> None:
        """Full strategy produces a single URL at the end, no extra flags."""
        output_dir = tmp_path / "out"
        source = self._make_full_source(output_dir)
        cmd = build_command(source, "/path/to/profile.conf")

        assert cmd[-1] == "https://example.com/channel"
        assert "--break-on-existing" not in cmd
        assert "--playlist-items" not in cmd

    def test_channel_tab_urls_handle_forms(self) -> None:
        """_channel_tab_urls works for various YouTube channel URL forms."""
        for url, expected_base in [
            ("https://www.youtube.com/@handle", "https://www.youtube.com/@handle"),
            ("https://www.youtube.com/channel/UCxxx", "https://www.youtube.com/channel/UCxxx"),
            ("https://www.youtube.com/c/MyChannel/", "https://www.youtube.com/c/MyChannel"),
        ]:
            tabs = _channel_tab_urls(url)
            assert tabs == [
                f"{expected_base}/videos",
                f"{expected_base}/shorts",
                f"{expected_base}/streams",
            ]


# ---------------------------------------------------------------------------
# 4. classify_line — failure classification
# ---------------------------------------------------------------------------


class TestClassifyLine:
    @pytest.mark.parametrize("line,expected_kind", [
        # Permanent
        (
            "ERROR: [youtube] abcDEF12345: members-only content",
            "permanent",
        ),
        (
            "ERROR: [youtube] abcDEF12345: This video is only available to members. Join this channel",
            "permanent",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Private video",
            "permanent",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Video unavailable",
            "permanent",
        ),
        (
            "ERROR: [youtube] abcDEF12345: This video has been removed by the uploader",
            "permanent",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Sign in to confirm your age",
            "permanent",
        ),
        (
            "ERROR: [youtube] channel: The uploader's account has been terminated",
            "permanent",
        ),
        # Transient
        (
            "ERROR: [youtube] abcDEF12345: HTTP Error 403: Forbidden",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: HTTP Error 429: Too Many Requests",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Connection reset by peer",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Read timed out.",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Temporary failure in name resolution",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: fragment 0001 not found",
            "transient",
        ),
        (
            "ERROR: [youtube] abcDEF12345: Sign in to confirm you're not a bot",
            "transient",
        ),
        # Errors without a per-video id: mid-download failures, tab authcheck.
        (
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "transient",
        ),
        (
            "ERROR: [youtube:tab] UCabc123456789: Playlists that require authentication may not extract correctly",
            "transient",
        ),
        # No match
        (
            "Downloading video abc123",
            None,
        ),
        (
            "[download] 100% of 10.00MiB",
            None,
        ),
    ])
    def test_classify_kind(self, line: str, expected_kind) -> None:
        result = classify_line(line)
        assert result.kind == expected_kind

    def test_extracts_video_id_from_error_prefix(self) -> None:
        line = "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable"
        result = classify_line(line)
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.kind == "permanent"

    def test_extracts_video_id_transient(self) -> None:
        line = "ERROR: [youtube] abcDEFghijk: HTTP Error 429: Too Many Requests"
        result = classify_line(line)
        assert result.video_id == "abcDEFghijk"
        assert result.kind == "transient"

    def test_no_video_id_for_non_error_line(self) -> None:
        result = classify_line("Downloading video abc123")
        assert result.video_id is None
        assert result.kind is None

    def test_no_video_id_for_unstructured_error(self) -> None:
        """A line matching a pattern but without the standard prefix has no video_id."""
        result = classify_line("video unavailable, skipping")
        assert result.kind == "permanent"
        assert result.video_id is None


# ---------------------------------------------------------------------------
# 5. Failure rows written to DB after run
# ---------------------------------------------------------------------------


class TestFailureRows:
    def _setup(self, monkeypatch, tmp_path, extra_output: str = ""):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, update_strategy="full", url="https://example.com/ch")
        assert main(["init"]) == 0
        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, exit_code=0, extra_output=extra_output)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir

    def test_two_permanent_failures_written(self, monkeypatch, tmp_path) -> None:
        """Two permanent-error lines → video_failures has both with attempts=1."""
        perm_output = (
            "ERROR: [youtube] vid111aaaaa: Video unavailable\n"
            "ERROR: [youtube] vid222bbbbb: Private video"
        )
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path, extra_output=perm_output)

        from kura.config import load_config
        from kura.runner import run_source
        config = load_config()
        source = config.sources[0]
        source_for_run = Source(
            name=source.name,
            url=source.url,
            output_dir=source.output_dir,
            archive_file=source.archive_file,
            profile=source.profile,
            cookies=source.cookies,
            schedule=source.schedule,
            update_strategy="full",
            full_reindex_every="7d",
            extra_args=source.extra_args,
            enabled=source.enabled,
        )

        from kura import paths
        run_source(source_for_run, paths.config_dir(), paths.state_dir())

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT video_id, kind, attempts FROM video_failures WHERE source = 'test-src'"
                " ORDER BY video_id"
            ).fetchall()

        assert len(rows) == 2
        assert rows[0][1] == "permanent"
        assert rows[0][2] == 1
        assert rows[1][1] == "permanent"
        assert rows[1][2] == 1

    def test_failure_attempts_increment_on_second_run(self, monkeypatch, tmp_path) -> None:
        """Second run with same error line → attempts=2."""
        perm_output = "ERROR: [youtube] vid111aaaaa: Video unavailable"
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path, extra_output=perm_output)

        from kura import paths
        from kura.config import load_config
        from kura.runner import run_source

        config = load_config()
        source = config.sources[0]
        source_for_run = Source(
            name=source.name,
            url=source.url,
            output_dir=source.output_dir,
            archive_file=source.archive_file,
            profile=source.profile,
            cookies=source.cookies,
            schedule=source.schedule,
            update_strategy="full",
            full_reindex_every="7d",
            extra_args=source.extra_args,
            enabled=source.enabled,
        )

        run_source(source_for_run, paths.config_dir(), paths.state_dir())
        run_source(source_for_run, paths.config_dir(), paths.state_dir())

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT attempts FROM video_failures WHERE source = 'test-src' AND video_id = 'vid111aaaaa'"
            ).fetchone()

        assert row is not None
        assert row[0] == 2

    def test_failure_summary_on_runs_row(self, monkeypatch, tmp_path) -> None:
        """failure_summary string stored on runs row."""
        mixed_output = (
            "ERROR: [youtube] vid111aaaaa: Video unavailable\n"
            "ERROR: [youtube] vid222bbbbb: HTTP Error 429: Too Many Requests"
        )
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path, extra_output=mixed_output)

        from kura import paths
        from kura.config import load_config
        from kura.runner import run_source

        config = load_config()
        source = config.sources[0]
        source_for_run = Source(
            name=source.name,
            url=source.url,
            output_dir=source.output_dir,
            archive_file=source.archive_file,
            profile=source.profile,
            cookies=source.cookies,
            schedule=source.schedule,
            update_strategy="full",
            full_reindex_every="7d",
            extra_args=source.extra_args,
            enabled=source.enabled,
        )

        run_source(source_for_run, paths.config_dir(), paths.state_dir())

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT failure_summary FROM runs WHERE source = 'test-src' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row is not None
        summary = row[0]
        assert "permanent" in summary
        assert "transient" in summary

    def test_source_level_failure_without_video_id(self, monkeypatch, tmp_path) -> None:
        """Id-less transient errors (mid-download 403) → one '(source)' row, attempts accumulate."""
        output = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        _config_dir, state_dir = self._setup(monkeypatch, tmp_path, extra_output=output)

        from kura import paths
        from kura.config import load_config
        from kura.failures import SOURCE_FAILURE_ID
        from kura.runner import run_source

        config = load_config()
        source = config.sources[0]
        source_for_run = Source(
            name=source.name,
            url=source.url,
            output_dir=source.output_dir,
            archive_file=source.archive_file,
            profile=source.profile,
            cookies=source.cookies,
            schedule=source.schedule,
            update_strategy="full",
            full_reindex_every="7d",
            extra_args=source.extra_args,
            enabled=source.enabled,
        )

        run_source(source_for_run, paths.config_dir(), paths.state_dir())
        run_source(source_for_run, paths.config_dir(), paths.state_dir())

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT video_id, kind, attempts FROM video_failures"
                " WHERE source = 'test-src' AND video_id = ?",
                (SOURCE_FAILURE_ID,),
            ).fetchone()
            summary = conn.execute(
                "SELECT failure_summary FROM runs WHERE source = 'test-src' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        assert row is not None, "source-level failure must be recorded, not dropped"
        assert row[1] == "transient"
        assert row[2] == 2, "one error line per run → attempts == runs"
        assert summary is not None and "transient" in summary[0]


# ---------------------------------------------------------------------------
# 6. Full reindex promotion
# ---------------------------------------------------------------------------


class TestFullReindexPromotion:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, update_strategy="quick")
        assert main(["init"]) == 0

        argv_file = tmp_path / "argv.jsonl"
        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(bin_dir, exit_code=0, record_argv_to=argv_file)
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir, argv_file

    def _get_run_argv(self, argv_file: Path) -> list[list[str]]:
        """Parse all recorded argvs from the file, skipping --version calls."""
        import json
        calls = []
        if not argv_file.exists():
            return calls
        for line in argv_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            args = json.loads(line)
            if args == ["--version"]:
                continue
            # Skip channel_id resolution calls
            if "--print" in args and "--playlist-items" in args:
                continue
            calls.append(args)
        return calls

    def test_null_last_full_run_promotes_to_full(self, monkeypatch, tmp_path, capsys) -> None:
        """quick source with NULL last_full_run_at → promoted to full (single URL)."""
        config_dir, state_dir, argv_file = self._setup(monkeypatch, tmp_path)

        result = main(["run", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "promoted to full reindex" in out

        calls = self._get_run_argv(argv_file)
        assert len(calls) >= 1
        # Full run: last arg is the base URL, not a /videos tab URL
        last_call = calls[-1]
        assert last_call[-1] == "https://www.youtube.com/@TestChannel"
        assert "--lazy-playlist" not in last_call

    def test_recent_last_full_run_uses_quick(self, monkeypatch, tmp_path, capsys) -> None:
        """quick source with recent last_full_run_at → stays as quick (tab URLs)."""
        config_dir, state_dir, argv_file = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Set last_full_run_at to 1 hour ago (well within 7d interval).
        recent_ts = datetime.now(tz=UTC).isoformat()
        set_last_full_run_at(db_path, "test-src", recent_ts)

        result = main(["run", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "promoted to full reindex" not in out

        calls = self._get_run_argv(argv_file)
        assert len(calls) >= 1
        last_call = calls[-1]
        # Quick run: ends with tab URLs
        assert last_call[-1].endswith("/streams")
        assert "--lazy-playlist" in last_call

    def test_old_last_full_run_promotes_again(self, monkeypatch, tmp_path, capsys) -> None:
        """quick source with last_full_run_at > 7d ago → promoted to full."""
        config_dir, state_dir, argv_file = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Set last_full_run_at to 8 days ago.
        old_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC).isoformat()
        set_last_full_run_at(db_path, "test-src", old_ts)

        result = main(["run", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "promoted to full reindex" in out

        calls = self._get_run_argv(argv_file)
        assert len(calls) >= 1
        last_call = calls[-1]
        assert last_call[-1] == "https://www.youtube.com/@TestChannel"

    def test_quick_run_does_not_update_last_full_run_at(self, monkeypatch, tmp_path, capsys) -> None:
        """A quick (non-promoted) run must NOT update last_full_run_at."""
        config_dir, state_dir, argv_file = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Set a recent last_full_run_at so no promotion happens.
        recent_ts = datetime.now(tz=UTC).isoformat()
        set_last_full_run_at(db_path, "test-src", recent_ts)

        main(["run", "test-src"])
        capsys.readouterr()

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_full_run_at FROM source_state WHERE source = 'test-src'"
            ).fetchone()

        assert row is not None
        # last_full_run_at should still be the value we set (not updated by quick run).
        assert row[0][:16] == recent_ts[:16]


# ---------------------------------------------------------------------------
# 7. RSS strategy
# ---------------------------------------------------------------------------


class TestRssStrategy:
    def _setup(
        self,
        monkeypatch,
        tmp_path,
        *,
        channel_id: str = "UCfakeChannelId123456789",
        archive_entries: list[str] | None = None,
    ):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, update_strategy="rss")
        assert main(["init"]) == 0

        # Pre-populate archive if requested.
        if archive_entries:
            archive_path = output_dir / "archive.txt"
            archive_path.write_text("\n".join(archive_entries) + "\n")

        argv_file = tmp_path / "argv.jsonl"
        bin_dir = tmp_path / "bin"
        write_fake_ytdlp(
            bin_dir,
            exit_code=0,
            record_argv_to=argv_file,
            print_channel_id=channel_id,
        )
        monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

        return config_dir, state_dir, output_dir, argv_file

    def _mock_rss(self, monkeypatch, video_ids: list[str]) -> None:
        """Monkeypatch _fetch_rss_video_ids to return *video_ids*."""
        import kura.runner as runner_mod

        def fake_fetch(channel_id: str, *, timeout: int = 30) -> list[str]:
            return video_ids

        monkeypatch.setattr(runner_mod, "_fetch_rss_video_ids", fake_fetch)

    def _get_download_calls(self, argv_file: Path) -> list[list[str]]:
        """Return yt-dlp calls that look like download invocations (not --version or --print)."""
        import json
        calls = []
        if not argv_file.exists():
            return calls
        for line in argv_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            args = json.loads(line)
            if args == ["--version"]:
                continue
            if "--print" in args and "--playlist-items" in args:
                continue
            calls.append(args)
        return calls

    def test_all_known_ids_no_ytdlp_download(self, monkeypatch, tmp_path, capsys) -> None:
        """If all RSS feed videos are in archive → no yt-dlp download invocation."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=["youtube vidAAA", "youtube vidBBB"],
        )
        # Promote to full reindex won't happen because we set last_full_run_at.
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA", "vidBBB"])

        result = main(["run", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "✓ test-src — nothing new" in out

        # No download call should have been made.
        download_calls = self._get_download_calls(argv_file)
        assert download_calls == []

    def test_all_known_ids_runs_row_recorded(self, monkeypatch, tmp_path, capsys) -> None:
        """RSS nothing-new path records a runs row with exit_code=0 and ytdlp_version='rss-check'."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=["youtube vidAAA"],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA"])

        main(["run", "test-src"])
        capsys.readouterr()

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT exit_code, new_count, ytdlp_version FROM runs WHERE source = 'test-src'"
            ).fetchone()

        assert row is not None
        assert row[0] == 0
        assert row[1] == 0
        assert row[2] == "rss-check"

    def test_all_known_ids_last_check_at_updated(self, monkeypatch, tmp_path, capsys) -> None:
        """RSS nothing-new path updates last_check_at in source_state."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=["youtube vidAAA"],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA"])
        main(["run", "test-src"])
        capsys.readouterr()

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_check_at FROM source_state WHERE source = 'test-src'"
            ).fetchone()

        assert row is not None
        assert row[0] is not None

    def test_unknown_id_falls_through_to_quick(self, monkeypatch, tmp_path, capsys) -> None:
        """One unknown RSS video → falls through to quick run (yt-dlp download invoked)."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=["youtube vidAAA"],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA", "vidNEW"])  # vidNEW is unknown

        result = main(["run", "test-src"])
        assert result == 0

        download_calls = self._get_download_calls(argv_file)
        assert len(download_calls) >= 1
        # Quick run: should have tab URLs
        last_call = download_calls[-1]
        assert any(u.endswith("/videos") for u in last_call)

    def test_rss_fetch_error_falls_through_to_quick(self, monkeypatch, tmp_path, capsys) -> None:
        """RSS fetch failure → warning printed, falls through to quick run."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        import kura.runner as runner_mod

        def fail_fetch(channel_id: str, *, timeout: int = 30) -> list[str]:
            raise ConnectionError("Network unreachable")

        monkeypatch.setattr(runner_mod, "_fetch_rss_video_ids", fail_fetch)

        result = main(["run", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "! rss feed fetch failed" in out

        # Should have fallen through to a download call.
        download_calls = self._get_download_calls(argv_file)
        assert len(download_calls) >= 1


    def test_feed_rotation_falls_through_to_quick(self, monkeypatch, tmp_path, capsys) -> None:
        """A fully rotated feed window must not be trusted as 'nothing new'."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=[
                "youtube vidAAA",
                "youtube vidBBB",
                "youtube vid111",
                "youtube vid222",
            ],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        # First check: establishes the remembered feed window.
        self._mock_rss(monkeypatch, ["vidAAA", "vidBBB"])
        main(["run", "test-src"])
        capsys.readouterr()
        assert self._get_download_calls(argv_file) == []

        # Second check: window fully rotated to disjoint IDs (burst uploads
        # scrolled past unseen videos).  Even though every feed ID is archived,
        # the run must escalate to quick instead of reporting nothing new.
        self._mock_rss(monkeypatch, ["vid111", "vid222"])
        main(["run", "test-src"])
        out = capsys.readouterr().out
        assert "! rss feed fully rotated" in out
        assert self._get_download_calls(argv_file), "must fall through to a quick run"

    def test_feed_overlap_is_trusted(self, monkeypatch, tmp_path, capsys) -> None:
        """Partial overlap bounds the missed tail to zero — no escalation needed."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            archive_entries=["youtube vidAAA", "youtube vidBBB", "youtube vidCCC"],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA", "vidBBB"])
        main(["run", "test-src"])
        capsys.readouterr()
        argv_file.write_text("")

        # vidAAA still present → fewer than one full window of new uploads.
        self._mock_rss(monkeypatch, ["vidAAA", "vidCCC"])
        main(["run", "test-src"])
        out = capsys.readouterr().out
        assert "fully rotated" not in out
        assert self._get_download_calls(argv_file) == []

    def test_channel_id_cached_on_second_run(self, monkeypatch, tmp_path, capsys) -> None:
        """channel_id resolved once and cached; second run reads from DB without re-invoking yt-dlp."""
        config_dir, state_dir, output_dir, argv_file = self._setup(
            monkeypatch,
            tmp_path,
            channel_id="UCfakeChannelId123456789",
            archive_entries=["youtube vidAAA"],
        )
        db_path = state_dir / "state.db"
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        self._mock_rss(monkeypatch, ["vidAAA"])

        # First run — should call yt-dlp --print to resolve channel_id.
        main(["run", "test-src"])
        capsys.readouterr()

        import json
        first_run_calls = []
        if argv_file.exists():
            for line in argv_file.read_text().splitlines():
                if line.strip():
                    first_run_calls.append(json.loads(line))

        resolution_calls_first = [c for c in first_run_calls if "--print" in c and "--playlist-items" in c]
        assert len(resolution_calls_first) == 1, "should resolve channel_id exactly once on first run"

        # Clear the argv file for second run.
        argv_file.write_text("")

        # Set last_full_run_at again (after first run may have updated it).
        set_last_full_run_at(db_path, "test-src", datetime.now(tz=UTC).isoformat())

        # Second run.
        main(["run", "test-src"])
        capsys.readouterr()

        second_run_calls = []
        if argv_file.exists():
            for line in argv_file.read_text().splitlines():
                if line.strip():
                    second_run_calls.append(json.loads(line))

        resolution_calls_second = [c for c in second_run_calls if "--print" in c and "--playlist-items" in c]
        assert resolution_calls_second == [], "channel_id should NOT be re-resolved on second run"


# ---------------------------------------------------------------------------
# 8. suppress / unsuppress
# ---------------------------------------------------------------------------


class TestSuppress:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, update_strategy="full", url="https://example.com/ch")
        assert main(["init"]) == 0
        return config_dir, state_dir, output_dir

    def test_suppress_appends_archive_line(self, monkeypatch, tmp_path, capsys) -> None:
        """suppress appends 'youtube VIDEO_ID' to archive.txt."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)

        result = main(["suppress", "test-src", "dQw4w9WgXcQ"])
        assert result == 0

        archive_set = read_archive(output_dir / "archive.txt")
        assert "youtube dQw4w9WgXcQ" in archive_set

    def test_suppress_creates_backup(self, monkeypatch, tmp_path) -> None:
        """suppress creates a backup of the archive before modifying it."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)
        archive_path = output_dir / "archive.txt"
        archive_path.write_text("youtube existing1234\n")

        main(["suppress", "test-src", "dQw4w9WgXcQ"])

        backups = list(output_dir.glob("archive.txt.bak.*"))
        assert len(backups) == 1
        assert "youtube existing1234" in backups[0].read_text()

    def test_suppress_inserts_suppressions_row(self, monkeypatch, tmp_path) -> None:
        """suppress inserts a row in the suppressions table."""
        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)

        main(["suppress", "test-src", "dQw4w9WgXcQ", "--reason", "test reason"])

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key, reason FROM suppressions WHERE source = 'test-src'"
            ).fetchone()

        assert row is not None
        assert row[0] == "youtube dQw4w9WgXcQ"
        assert row[1] == "test reason"

    def test_suppress_deletes_video_failures_row(self, monkeypatch, tmp_path) -> None:
        """suppress removes the video_failures row for the suppressed video."""
        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Pre-insert a video_failures row.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO video_failures (source, video_id, url, kind, attempts, last_seen, last_error)
                VALUES ('test-src', 'dQw4w9WgXcQ', NULL, 'permanent', 3, '2024-01-01T00:00:00', 'error')
                """
            )

        main(["suppress", "test-src", "dQw4w9WgXcQ"])

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM video_failures WHERE source = 'test-src' AND video_id = 'dQw4w9WgXcQ'"
            ).fetchone()

        assert row is None

    def test_suppress_warns_if_already_present(self, monkeypatch, tmp_path, capsys) -> None:
        """suppress skips append and warns if entry is already in archive."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)
        archive_path = output_dir / "archive.txt"
        archive_path.write_text("youtube dQw4w9WgXcQ\n")

        result = main(["suppress", "test-src", "dQw4w9WgXcQ"])
        assert result == 0

        out = capsys.readouterr().out
        assert "warning" in out.lower() or "already" in out.lower()

    def test_suppress_rejects_invalid_video_id_with_space(self, monkeypatch, tmp_path, capsys) -> None:
        """suppress rejects video_id containing spaces."""
        self._setup(monkeypatch, tmp_path)
        result = main(["suppress", "test-src", "id with space"])
        assert result == 1
        out = capsys.readouterr().out
        assert "error" in out.lower()

    def test_suppress_respects_extractor_flag(self, monkeypatch, tmp_path) -> None:
        """--extractor flag uses a different extractor prefix."""
        _config_dir, state_dir, output_dir = self._setup(monkeypatch, tmp_path)

        main(["suppress", "test-src", "sm9", "--extractor", "niconico"])

        archive_set = read_archive(output_dir / "archive.txt")
        assert "niconico sm9" in archive_set

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key FROM suppressions WHERE source = 'test-src'"
            ).fetchone()
        assert row[0] == "niconico sm9"

    def test_suppress_fails_if_lock_held(self, monkeypatch, tmp_path, capsys) -> None:
        """suppress exits 1 if source lock is already held."""
        from kura import paths
        from kura.locks import source_lock

        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)

        with source_lock("test-src", paths.state_dir()) as acquired:
            assert acquired is True
            result = main(["suppress", "test-src", "dQw4w9WgXcQ"])

        assert result == 1
        out = capsys.readouterr().out
        assert "error" in out.lower()

    def test_suppress_unknown_source_exits_1(self, monkeypatch, tmp_path, capsys) -> None:
        """suppress exits 1 for unknown source."""
        self._setup(monkeypatch, tmp_path)
        result = main(["suppress", "no-such-source", "dQw4w9WgXcQ"])
        assert result == 1

    def test_unsuppress_removes_archive_line(self, monkeypatch, tmp_path) -> None:
        """unsuppress removes the exact archive line."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)

        main(["suppress", "test-src", "dQw4w9WgXcQ"])
        assert "youtube dQw4w9WgXcQ" in read_archive(output_dir / "archive.txt")

        result = main(["unsuppress", "test-src", "dQw4w9WgXcQ"])
        assert result == 0

        assert "youtube dQw4w9WgXcQ" not in read_archive(output_dir / "archive.txt")

    def test_unsuppress_creates_backup(self, monkeypatch, tmp_path) -> None:
        """unsuppress creates a backup before rewriting."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)

        main(["suppress", "test-src", "dQw4w9WgXcQ"])
        # Clear previous backups to count only unsuppress backup.
        for bak in output_dir.glob("archive.txt.bak.*"):
            bak.unlink()

        main(["unsuppress", "test-src", "dQw4w9WgXcQ"])

        backups = list(output_dir.glob("archive.txt.bak.*"))
        assert len(backups) == 1

    def test_unsuppress_deletes_suppressions_row(self, monkeypatch, tmp_path) -> None:
        """unsuppress deletes the suppressions row."""
        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)

        main(["suppress", "test-src", "dQw4w9WgXcQ"])
        main(["unsuppress", "test-src", "dQw4w9WgXcQ"])

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key FROM suppressions WHERE source = 'test-src'"
            ).fetchone()

        assert row is None

    def test_unsuppress_requires_suppressions_row(self, monkeypatch, tmp_path, capsys) -> None:
        """unsuppress exits 1 when no suppression row exists (kura only removes what it added)."""
        _config_dir, _state_dir, output_dir = self._setup(monkeypatch, tmp_path)

        # Manually add archive entry without going through suppress.
        archive_path = output_dir / "archive.txt"
        archive_path.write_text("youtube dQw4w9WgXcQ\n")

        result = main(["unsuppress", "test-src", "dQw4w9WgXcQ"])
        assert result == 1

        out = capsys.readouterr().out
        assert "error" in out.lower()

    def test_full_suppress_unsuppress_lifecycle(self, monkeypatch, tmp_path, capsys) -> None:
        """Full lifecycle: suppress → unsuppress → suppress again works."""
        _config_dir, state_dir, output_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        assert main(["suppress", "test-src", "dQw4w9WgXcQ", "--reason", "gone"]) == 0
        assert "youtube dQw4w9WgXcQ" in read_archive(output_dir / "archive.txt")

        assert main(["unsuppress", "test-src", "dQw4w9WgXcQ"]) == 0
        assert "youtube dQw4w9WgXcQ" not in read_archive(output_dir / "archive.txt")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key FROM suppressions WHERE source = 'test-src'"
            ).fetchone()
        assert row is None

        # Can suppress again.
        assert main(["suppress", "test-src", "dQw4w9WgXcQ"]) == 0
        assert "youtube dQw4w9WgXcQ" in read_archive(output_dir / "archive.txt")
        capsys.readouterr()


# ---------------------------------------------------------------------------
# 9. status with NAME (detailed view)
# ---------------------------------------------------------------------------


class TestStatusDetail:
    def _setup(self, monkeypatch, tmp_path):
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_config_yaml(config_dir, output_dir, update_strategy="full", url="https://example.com/ch")
        assert main(["init"]) == 0
        return config_dir, state_dir, output_dir

    def test_status_name_shows_source_detail(self, monkeypatch, tmp_path, capsys) -> None:
        """kura status NAME shows detailed view for one source."""
        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)

        result = main(["status", "test-src"])
        assert result == 0

        out = capsys.readouterr().out
        assert "test-src" in out
        assert "https://example.com/ch" in out
        assert "0 archived" in out
        assert "next check now" in out

    def test_status_name_shows_runs_failures_and_suppressions(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        """The detail view renders every populated section with the new glyph vocabulary."""
        _config_dir, state_dir, output_dir = self._setup(monkeypatch, tmp_path)
        (output_dir / "archive.txt").write_text("youtube one\nyoutube two\n")
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(state_dir / "state.db") as conn:
            conn.execute(
                "INSERT INTO source_state (source, last_check_at, imported_at) VALUES (?, ?, ?)",
                ("test-src", now, "2026-07-01T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO runs
                    (source, started_at, ended_at, exit_code, new_count, failure_summary)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test-src", now, now, 0, 2, "1 transient"),
            )
            conn.execute(
                """
                INSERT INTO video_failures
                    (source, video_id, kind, attempts, last_error)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("test-src", "abc123", "permanent", 4, "Join this channel to get access"),
            )
            conn.execute(
                "INSERT INTO suppressions (source, archive_key, suppressed_at, reason) VALUES (?, ?, ?, ?)",
                ("test-src", "youtube old", "2026-07-01T00:00:00+00:00", "deleted"),
            )

        assert main(["status", "test-src"]) == 0
        out = capsys.readouterr().out
        assert "● enabled · full · every 6h · av1-archive · imported Jul 01" in out
        assert f"{output_dir} — 2 archived" in out
        assert "  runs" in out
        assert "✓ " in out and "+2 new · 1 transient" in out
        assert "failing permanently" in out
        assert "✗ abc123   4 attempts · Join this channel to get access" in out
        assert "suppress with `kura suppress test-src VIDEO_ID`" in out
        assert "○ youtube old · deleted · Jul 01" in out
        assert "next check in" in out

    def test_status_name_unknown_source_exits_1(self, monkeypatch, tmp_path, capsys) -> None:
        """kura status UNKNOWN exits 1."""
        self._setup(monkeypatch, tmp_path)

        result = main(["status", "no-such-source"])
        assert result == 1

    def test_status_overview_shows_source_state(self, monkeypatch, tmp_path, capsys) -> None:
        """kura status shows the source and its never-run state."""
        self._setup(monkeypatch, tmp_path)

        result = main(["status"])
        assert result == 0

        out = capsys.readouterr().out
        assert "kura · 1 source" in out
        assert "● test-src" in out
        assert "never run" in out

    def test_status_table_hint_when_permanent_failures(self, monkeypatch, tmp_path, capsys) -> None:
        """Summary table shows hint when a source has >= 3-attempt permanent failures."""
        _config_dir, state_dir, _output_dir = self._setup(monkeypatch, tmp_path)
        db_path = state_dir / "state.db"

        # Insert a permanent failure with >= 3 attempts.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO video_failures (source, video_id, url, kind, attempts, last_seen, last_error)
                VALUES ('test-src', 'abc123', NULL, 'permanent', 3, '2024-01-01T00:00:00', 'gone')
                """
            )

        result = main(["status"])
        assert result == 0

        out = capsys.readouterr().out
        assert "1 failing" in out
        assert "run `kura status NAME` for failure details" in out
