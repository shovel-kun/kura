"""Tests for the nndownload downloader."""
from __future__ import annotations

import os
import sqlite3
import stat
import textwrap
from pathlib import Path

import pytest

from kura.cli import main
from kura.config import (
    ConfigError,
    Source,
    config_from_data,
    load_nnconf,
)
from kura.failures import (
    classify_nndownload_failure,
    parse_result_line,
)
from kura.runner import build_nndownload_command, run_source

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def write_ytdlp_profile(config_dir: Path, name: str = "av1-archive") -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / f"{name}.conf").write_text("--continue\n")


def write_nnconf_profile(config_dir: Path, name: str = "niconico-archive", extra: str = "") -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / f"{name}.nnconf").write_text(
        "--dump-metadata\n"
        "--download-thumbnail\n"
        "--download-comments\n"
        '--output-path "{title} [{id}]/{title} [{id}].{ext}"\n'
        + extra
    )


def make_niconico_source(
    output_dir: Path,
    *,
    update_strategy: str = "full",
    full_reindex_every: str = "7d",
    profile: str = "niconico-archive",
    auth: str = "--session-cookie /fake/cookie.txt",
    url: str = "https://www.nicovideo.jp/mylist/12345678",
) -> Source:
    return Source(
        name="nico-src",
        url=url,
        output_dir=output_dir,
        archive_file=output_dir / "archive.txt",
        profile=profile,
        cookies=None,
        schedule="0 */12 * * *",
        update_strategy=update_strategy,
        full_reindex_every=full_reindex_every,
        extra_args=[],
        enabled=True,
        downloader="nndownload",
        auth=auth,
    )


def write_fake_nndownload(
    bin_dir: Path,
    *,
    exit_code: int = 0,
    archive_lines: list[str] | None = None,
    result_lines: list[str] | None = None,
    version: str = "1.19.fake",
    list_ids: list[str] | None = None,
    help_text: str = "--download-archive",
) -> None:
    """Write a fake nndownload executable to *bin_dir*.

    Behaviour:
    - ``--version``: prints *version* and exits.
    - ``--help``: prints *help_text* and exits.
    - ``--list-ids`` present: prints *list_ids* (one per line) and exits.
    - Otherwise: appends *archive_lines* to the ``--download-archive`` file,
      prints *result_lines*, and exits with *exit_code*.
    """
    archive_lines = archive_lines or []
    result_lines = result_lines or []
    list_ids = list_ids or []
    lines_repr = repr(archive_lines)
    result_repr = repr(result_lines)
    list_ids_repr = repr(list_ids)

    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys

        args = sys.argv[1:]

        if "--version" in args or args == ["--version"]:
            print({version!r})
            sys.exit(0)

        if "--help" in args or args == ["--help"]:
            print({help_text!r})
            sys.exit(0)

        if "--list-ids" in args:
            for vid_id in {list_ids_repr}:
                print(vid_id)
            sys.exit({exit_code})

        archive = None
        for i, a in enumerate(args):
            if a == "--download-archive" and i + 1 < len(args):
                archive = args[i + 1]
                break

        if archive is not None:
            for line in {lines_repr}:
                with open(archive, "a") as f:
                    f.write(line + "\\n")

        for line in {result_repr}:
            print(line)
        sys.exit({exit_code})
    """)

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "nndownload"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_ytdlp_minimal(bin_dir: Path) -> None:
    """Fake yt-dlp that just responds to --version."""
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        if "--version" in sys.argv:
            print("2099.01.01")
            sys.exit(0)
        sys.exit(0)
    """)
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def make_niconico_config(config_dir: Path, output_dir: Path, *, strategy: str = "full") -> None:
    write_ytdlp_profile(config_dir)
    write_nnconf_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: nico-src
    url: https://www.nicovideo.jp/mylist/12345678
    output_dir: {output_dir}
    downloader: nndownload
    profile: niconico-archive
    update_strategy: {strategy}
    auth: "--session-cookie /fake/cookie.txt"
    extra_args: []
    enabled: true
"""
    )


# ---------------------------------------------------------------------------
# §1 Config validation: downloader enum
# ---------------------------------------------------------------------------


class TestConfigDownloaderEnum:
    def _make_config_dir(self, tmp_path: Path) -> Path:
        write_ytdlp_profile(tmp_path)
        return tmp_path

    def _valid_yt_source(self, **overrides) -> dict:
        src = {
            "name": "yt-src",
            "url": "https://www.youtube.com/@TestChannel",
            "output_dir": "/archive/yt",
            "extra_args": [],
            "enabled": True,
        }
        src.update(overrides)
        return src

    def test_default_downloader_is_ytdlp(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        data = {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [self._valid_yt_source()],
        }
        config = config_from_data(data, config_dir)
        assert config.sources[0].downloader == "ytdlp"
        assert config.sources[0].auth is None

    def test_invalid_downloader_rejected(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        data = {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [self._valid_yt_source(downloader="wget")],
        }
        with pytest.raises(ConfigError, match="invalid downloader"):
            config_from_data(data, config_dir)

    def test_auth_on_ytdlp_source_rejected(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        data = {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [self._valid_yt_source(auth="--session-cookie /x")],
        }
        with pytest.raises(ConfigError, match="auth is only valid for nndownload"):
            config_from_data(data, config_dir)


# ---------------------------------------------------------------------------
# §2 Config validation: auth required for nndownload
# ---------------------------------------------------------------------------


class TestNiconicAuthValidation:
    def _make_config_dir(self, tmp_path: Path) -> Path:
        write_ytdlp_profile(tmp_path)
        write_nnconf_profile(tmp_path)
        return tmp_path

    def _base_data(self, source: dict) -> dict:
        return {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [source],
        }

    def _nn_source(self, **overrides) -> dict:
        src = {
            "name": "nn-src",
            "url": "https://www.nicovideo.jp/mylist/12345",
            "output_dir": "/archive/nn",
            "downloader": "nndownload",
            "profile": "niconico-archive",
            "update_strategy": "full",
            "extra_args": [],
            "enabled": True,
        }
        src.update(overrides)
        return src

    def test_auth_missing_raises(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        with pytest.raises(ConfigError, match="require auth"):
            config_from_data(self._base_data(self._nn_source()), config_dir)

    def test_auth_without_any_known_token_raises(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        with pytest.raises(ConfigError, match="hang prompting"):
            config_from_data(self._base_data(self._nn_source(auth="--proxy socks5://x")), config_dir)

    @pytest.mark.parametrize("auth", [
        "-g",
        "-n",
        "--session-cookie /path/to/cookie",
        "-u user -p pass",
    ])
    def test_valid_auth_tokens_accepted(self, tmp_path: Path, auth: str) -> None:
        config_dir = self._make_config_dir(tmp_path)
        config = config_from_data(self._base_data(self._nn_source(auth=auth)), config_dir)
        assert config.sources[0].auth == auth


# ---------------------------------------------------------------------------
# §3 Config validation: URL types
# ---------------------------------------------------------------------------


class TestNiconicoUrlValidation:
    def _make_config_dir(self, tmp_path: Path) -> Path:
        write_ytdlp_profile(tmp_path)
        write_nnconf_profile(tmp_path)
        return tmp_path

    def _base_data(self, url: str) -> dict:
        return {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [{
                "name": "nn-src",
                "url": url,
                "output_dir": "/archive/nn",
                "downloader": "nndownload",
                "profile": "niconico-archive",
                "update_strategy": "full",
                "auth": "-g",
                "extra_args": [],
                "enabled": True,
            }],
        }

    @pytest.mark.parametrize("url", [
        "https://www.nicovideo.jp/watch/sm9",
        "https://www.nicovideo.jp/mylist/12345678",
        "https://www.nicovideo.jp/user/12345678",
        "https://www.nicovideo.jp/user/12345678/video",
        "https://www.nicovideo.jp/series/12345",
        "https://ch.nicovideo.jp/my-channel",
        "https://ch.nicovideo.jp/my-channel/video",
    ])
    def test_valid_urls_accepted(self, tmp_path: Path, url: str) -> None:
        config_dir = self._make_config_dir(tmp_path)
        config = config_from_data(self._base_data(url), config_dir)
        assert config.sources[0].url == url

    @pytest.mark.parametrize("url", [
        "https://seiga.nicovideo.jp/seiga/im123",
        "https://live.nicovideo.jp/watch/lv123",
        "https://comic.nicovideo.jp/chapter/123",
        "https://nicovideo.jp/seiga/im123",
    ])
    def test_seiga_live_manga_rejected(self, tmp_path: Path, url: str) -> None:
        config_dir = self._make_config_dir(tmp_path)
        with pytest.raises(ConfigError, match="videos only"):
            config_from_data(self._base_data(url), config_dir)

    def test_non_nicovideo_url_rejected(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        with pytest.raises(ConfigError, match="supported nicovideo URL"):
            config_from_data(self._base_data("https://example.com/video"), config_dir)


# ---------------------------------------------------------------------------
# §4 Config validation: strategy restriction
# ---------------------------------------------------------------------------


class TestNiconicoStrategyValidation:
    def _make_config_dir(self, tmp_path: Path) -> Path:
        write_ytdlp_profile(tmp_path)
        write_nnconf_profile(tmp_path)
        return tmp_path

    def _data_with_strategy(self, strategy: str) -> dict:
        return {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [{
                "name": "nn-src",
                "url": "https://www.nicovideo.jp/mylist/12345",
                "output_dir": "/archive/nn",
                "downloader": "nndownload",
                "profile": "niconico-archive",
                "update_strategy": strategy,
                "auth": "-g",
                "extra_args": [],
                "enabled": True,
            }],
        }

    def test_rss_strategy_rejected_for_nndownload(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        with pytest.raises(ConfigError, match="only support full or quick"):
            config_from_data(self._data_with_strategy("rss"), config_dir)

    def test_full_strategy_accepted(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        config = config_from_data(self._data_with_strategy("full"), config_dir)
        assert config.sources[0].update_strategy == "full"

    def test_quick_strategy_accepted(self, tmp_path: Path) -> None:
        config_dir = self._make_config_dir(tmp_path)
        config = config_from_data(self._data_with_strategy("quick"), config_dir)
        assert config.sources[0].update_strategy == "quick"


# ---------------------------------------------------------------------------
# §5 Config validation: .nnconf profile lookup
# ---------------------------------------------------------------------------


class TestNniconicoProfileLookup:
    def test_nnconf_profile_required_for_nndownload(self, tmp_path: Path) -> None:
        """nndownload sources must have a .nnconf profile, not .conf."""
        write_ytdlp_profile(tmp_path, "niconico-archive")  # writes .conf, not .nnconf
        data = {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [{
                "name": "nn-src",
                "url": "https://www.nicovideo.jp/mylist/12345",
                "output_dir": "/archive/nn",
                "downloader": "nndownload",
                "profile": "niconico-archive",
                "update_strategy": "full",
                "auth": "-g",
                "extra_args": [],
                "enabled": True,
            }],
        }
        with pytest.raises(ConfigError, match="unknown profile"):
            config_from_data(data, tmp_path)

    def test_nnconf_profile_accepted(self, tmp_path: Path) -> None:
        write_ytdlp_profile(tmp_path)
        write_nnconf_profile(tmp_path)
        data = {
            "defaults": {"profile": "av1-archive", "schedule": "0 */6 * * *",
                         "update_strategy": "full", "full_reindex_every": "7d"},
            "sources": [{
                "name": "nn-src",
                "url": "https://www.nicovideo.jp/mylist/12345",
                "output_dir": "/archive/nn",
                "downloader": "nndownload",
                "profile": "niconico-archive",
                "update_strategy": "full",
                "auth": "-g",
                "extra_args": [],
                "enabled": True,
            }],
        }
        config = config_from_data(data, tmp_path)
        assert config.sources[0].profile == "niconico-archive"


# ---------------------------------------------------------------------------
# §6 init command writes niconico-archive.nnconf
# ---------------------------------------------------------------------------


def test_init_writes_niconico_profile(monkeypatch, tmp_path: Path, capsys) -> None:
    set_kura_dirs(monkeypatch, tmp_path)
    result = main(["init"])
    assert result == 0

    config_dir = tmp_path / "config"
    nnconf = config_dir / "profiles" / "niconico-archive.nnconf"
    assert nnconf.exists(), "niconico-archive.nnconf should be created by init"
    content = nnconf.read_text()
    assert "--dump-metadata" in content
    assert "--download-thumbnail" in content
    assert "--download-comments" in content
    assert "--output-path" in content


def test_init_idempotent_for_niconico_profile(monkeypatch, tmp_path: Path) -> None:
    set_kura_dirs(monkeypatch, tmp_path)
    main(["init"])
    # Modify the file.
    config_dir = tmp_path / "config"
    nnconf = config_dir / "profiles" / "niconico-archive.nnconf"
    nnconf.write_text("# custom\n")
    # Second init must NOT overwrite.
    main(["init"])
    assert "# custom" in nnconf.read_text()


# ---------------------------------------------------------------------------
# §7 build_nndownload_command: argv shape
# ---------------------------------------------------------------------------


class TestBuildNndownloadCommand:
    def _profile_args(self) -> list[str]:
        return ["--dump-metadata", "--download-thumbnail"]

    def test_full_strategy_no_break_on_existing(self, tmp_path: Path) -> None:
        source = make_niconico_source(tmp_path / "out", update_strategy="full")
        cmd = build_nndownload_command(source, self._profile_args())
        assert "nndownload" == cmd[0]
        assert "--non-interactive" in cmd
        assert "--download-archive" in cmd
        archive_idx = cmd.index("--download-archive")
        assert cmd[archive_idx + 1] == str(source.archive_file)
        assert "--break-on-existing" not in cmd
        assert cmd[-1] == source.url

    def test_quick_strategy_has_break_on_existing(self, tmp_path: Path) -> None:
        source = make_niconico_source(tmp_path / "out", update_strategy="quick")
        cmd = build_nndownload_command(source, self._profile_args())
        assert "--break-on-existing" in cmd
        assert cmd[-1] == source.url

    def test_auth_args_included(self, tmp_path: Path) -> None:
        source = make_niconico_source(
            tmp_path / "out",
            auth="--session-cookie /path/to/cookie.txt",
        )
        cmd = build_nndownload_command(source, self._profile_args())
        assert "--session-cookie" in cmd
        assert "/path/to/cookie.txt" in cmd

    def test_profile_args_order(self, tmp_path: Path) -> None:
        """profile_args appear after auth and before strategy flag."""
        source = make_niconico_source(
            tmp_path / "out",
            update_strategy="quick",
            auth="-g",
        )
        profile_args = ["--dump-metadata"]
        cmd = build_nndownload_command(source, profile_args)
        g_idx = cmd.index("-g")
        meta_idx = cmd.index("--dump-metadata")
        break_idx = cmd.index("--break-on-existing")
        assert g_idx < meta_idx < break_idx

    def test_extra_args_before_url(self, tmp_path: Path) -> None:
        source = make_niconico_source(tmp_path / "out", auth="-g")
        import dataclasses
        source = dataclasses.replace(source, extra_args=["--proxy", "socks5://localhost:1080"])
        cmd = build_nndownload_command(source, [])
        proxy_idx = cmd.index("--proxy")
        url_idx = cmd.index(source.url)
        assert proxy_idx < url_idx
        assert cmd[-1] == source.url


# ---------------------------------------------------------------------------
# §8 load_nnconf: profile parsing
# ---------------------------------------------------------------------------


class TestLoadNnconf:
    def test_comments_and_blanks_stripped(self, tmp_path: Path) -> None:
        nnconf = tmp_path / "test.nnconf"
        nnconf.write_text(
            "# this is a comment\n"
            "\n"
            "--dump-metadata\n"
            "  # indented comment\n"
            "--download-thumbnail\n"
        )
        args = load_nnconf(nnconf)
        assert args == ["--dump-metadata", "--download-thumbnail"]

    def test_quoted_arg_with_spaces_is_one_token(self, tmp_path: Path) -> None:
        """--output-path with a quoted template containing spaces must be ONE arg."""
        nnconf = tmp_path / "test.nnconf"
        nnconf.write_text('--output-path "{title} [{id}]/{title} [{id}].{ext}"\n')
        args = load_nnconf(nnconf)
        assert len(args) == 2
        assert args[0] == "--output-path"
        assert args[1] == "{title} [{id}]/{title} [{id}].{ext}"

    def test_full_niconico_profile(self, tmp_path: Path) -> None:
        nnconf = tmp_path / "niconico-archive.nnconf"
        nnconf.write_text(
            "# profiles/niconico-archive.nnconf\n"
            "--dump-metadata\n"
            "--download-thumbnail\n"
            "--download-comments\n"
            '--output-path "{title} [{id}]/{title} [{id}].{ext}"\n'
        )
        args = load_nnconf(nnconf)
        assert "--dump-metadata" in args
        assert "--download-comments" in args
        assert "--output-path" in args
        # The template with spaces must survive as a single token.
        ot_idx = args.index("--output-path")
        assert " " in args[ot_idx + 1]


# ---------------------------------------------------------------------------
# §9 parse_result_line
# ---------------------------------------------------------------------------


class TestParseResultLine:
    def test_ok_line(self) -> None:
        result = parse_result_line("[result] ok sm12345")
        assert result is not None
        assert result == ("ok", "sm12345", None)

    def test_skip_line(self) -> None:
        result = parse_result_line("[result] skip sm12345 archived")
        assert result is not None
        status, vid, detail = result
        assert status == "skip"
        assert vid == "sm12345"
        assert detail == "archived"

    def test_fail_line(self) -> None:
        result = parse_result_line("[result] fail sm99 FormatNotAvailableException: premium required")
        assert result is not None
        status, vid, detail = result
        assert status == "fail"
        assert vid == "sm99"
        assert "premium" in detail

    def test_non_result_line_returns_none(self) -> None:
        assert parse_result_line("Downloading sm12345") is None
        assert parse_result_line("[info] starting") is None
        assert parse_result_line("") is None

    def test_whitespace_tolerance(self) -> None:
        # leading whitespace
        result = parse_result_line("  [result] ok nm1")
        assert result is not None
        assert result[0] == "ok"


# ---------------------------------------------------------------------------
# §10 classify_nndownload_failure
# ---------------------------------------------------------------------------


class TestClassifyNndownloadFailure:
    @pytest.mark.parametrize("detail,expected", [
        ("deleted from site", "permanent"),
        ("video removed", "permanent"),
        ("private video", "permanent"),
        ("FormatNotAvailableException: premium content", "permanent"),
        ("FormatNotAvailableException: paid content", "permanent"),
        ("region block", "permanent"),
        ("HTTP 500 error", "transient"),
        ("timed out", "transient"),
        ("ParameterExtractionException: site changed", "transient"),
        ("authentication failed", "transient"),
        ("session expired", "transient"),
        ("connection reset", "transient"),
        (None, "transient"),
        ("some unknown error", "transient"),  # default
    ])
    def test_classification(self, detail: str | None, expected: str) -> None:
        assert classify_nndownload_failure(detail) == expected


# ---------------------------------------------------------------------------
# §11 run e2e: successful run, new_count, version, source_state
# ---------------------------------------------------------------------------


def test_run_nndownload_exit0_two_items(monkeypatch, tmp_path: Path, capsys) -> None:
    """Fake nndownload appends 2 archive lines; DB row reflects new_count=2."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(
        bin_dir,
        exit_code=0,
        archive_lines=["niconico sm1", "niconico sm2"],
        result_lines=["[result] ok sm1", "[result] ok sm2"],
        version="1.19.fake",
    )
    write_fake_ytdlp_minimal(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "✓ nico-src — 2 new" in captured.out

    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT exit_code, new_count, ytdlp_version, log_path FROM runs WHERE source = 'nico-src'"
        ).fetchone()
    assert row is not None
    assert row[0] == 0          # exit_code
    assert row[1] == 2          # new_count
    assert row[2] == "nndownload 1.19.fake"   # version column

    # source_state updated.
    with sqlite3.connect(db_path) as conn:
        ss = conn.execute(
            "SELECT last_check_at, last_success_at, last_full_run_at FROM source_state WHERE source = 'nico-src'"
        ).fetchone()
    assert ss is not None
    assert ss[0] is not None  # last_check_at
    assert ss[1] is not None  # last_success_at
    assert ss[2] is not None  # last_full_run_at (full strategy)


def test_run_nndownload_fail_line_inserts_video_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    """Fake nndownload emits a [result] fail line → video_failures row."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(
        bin_dir,
        exit_code=0,
        archive_lines=[],
        result_lines=["[result] fail sm3 FormatNotAvailableException: premium required"],
        version="1.19.fake",
    )
    write_fake_ytdlp_minimal(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]
    run_source(source, config_dir, state_dir)

    db_path = state_dir / "state.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT video_id, kind FROM video_failures WHERE source = 'nico-src'"
        ).fetchone()
    assert row is not None
    assert row[0] == "sm3"
    assert row[1] == "permanent"

    # failure_summary populated.
    with sqlite3.connect(db_path) as conn:
        run_row = conn.execute(
            "SELECT failure_summary FROM runs WHERE source = 'nico-src'"
        ).fetchone()
    assert run_row is not None
    assert "permanent" in (run_row[0] or "")


# ---------------------------------------------------------------------------
# §12 Promotion: quick nndownload with stale last_full_run_at
# ---------------------------------------------------------------------------


def test_run_nndownload_quick_promoted_to_full(monkeypatch, tmp_path: Path, capsys) -> None:
    """Quick nndownload source with stale last_full_run_at runs without --break-on-existing."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir, strategy="quick")
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"

    # Track whether --break-on-existing was passed.
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        args = sys.argv[1:]
        if "--version" in args:
            print("1.19.fake")
            sys.exit(0)
        # Write the args to a temp file so the test can inspect them.
        import json, os, tempfile
        marker = os.path.join(os.path.dirname(sys.argv[0]), "nn_args.json")
        with open(marker, "w") as f:
            json.dump(args, f)
        archive = None
        for i, a in enumerate(args):
            if a == "--download-archive" and i + 1 < len(args):
                archive = args[i + 1]
                break
        sys.exit(0)
    """)
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "nndownload"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    write_fake_ytdlp_minimal(bin_dir)

    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    # Set last_full_run_at to a very old timestamp to trigger promotion.
    import kura.state as _state
    db_path = state_dir / "state.db"
    _state.init_db(db_path)
    stale_ts = "2000-01-01T00:00:00+00:00"
    with _state.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO source_state (source, last_full_run_at) VALUES (?, ?)",
            ("nico-src", stale_ts),
        )

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]
    run_source(source, config_dir, state_dir)

    # Check that --break-on-existing was NOT in the argv (promoted to full).
    import json
    args_file = bin_dir / "nn_args.json"
    assert args_file.exists(), "Fake nndownload should have written nn_args.json"
    with args_file.open() as f:
        recorded_args = json.load(f)
    assert "--break-on-existing" not in recorded_args

    captured = capsys.readouterr()
    assert "promoted" in captured.out


# ---------------------------------------------------------------------------
# §13 verify e2e
# ---------------------------------------------------------------------------


def test_verify_nndownload_pending_two(monkeypatch, tmp_path: Path, capsys) -> None:
    """Fake --list-ids prints 3 ids, 1 already archived → pending 2."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    # Pre-archive one id.
    archive_path = output_dir / "archive.txt"
    archive_path.write_text("niconico sm1\n")

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(bin_dir, list_ids=["sm1", "sm2", "sm3"])
    write_fake_ytdlp_minimal(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["verify", "nico-src"])
    assert result == 0

    captured = capsys.readouterr()
    assert "nico-src — 2 pending" in captured.out


def test_verify_nndownload_zero_ids_with_nonempty_archive_exits_1(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Fake --list-ids prints nothing but archive is non-empty → exit 1."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    # Non-empty archive.
    archive_path = output_dir / "archive.txt"
    archive_path.write_text("niconico sm1\n")

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(bin_dir, list_ids=[])  # prints nothing
    write_fake_ytdlp_minimal(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["verify", "nico-src"])
    assert result == 1

    captured = capsys.readouterr()
    assert "treating as failure" in captured.out


# ---------------------------------------------------------------------------
# §14 doctor
# ---------------------------------------------------------------------------


def test_doctor_unpatched_nndownload_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    """Doctor reports FAIL when nndownload --help lacks --download-archive."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    # Unpatched: help text does NOT mention --download-archive.
    write_fake_nndownload(bin_dir, help_text="usage: nndownload [options] URL")
    write_fake_ytdlp_minimal(bin_dir)
    # Also need a fake ffmpeg.
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\nexit 0\n")
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["doctor"])
    assert result == 1  # has FAIL

    captured = capsys.readouterr()
    assert "✗ nndownload unpatched" in captured.out


def test_doctor_patched_nndownload_ok(monkeypatch, tmp_path: Path, capsys) -> None:
    """Doctor reports ok when nndownload --help has --download-archive."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    make_niconico_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(
        bin_dir,
        help_text="usage: nndownload\n  --download-archive FILE  archive file",
        version="1.19.patched",
    )
    write_fake_ytdlp_minimal(bin_dir)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\nexit 0\n")
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["doctor"])
    # May still have warnings (output_dir doesn't exist, etc.) but key check is ok.
    captured = capsys.readouterr()
    assert "unpatched nndownload" not in captured.out
    assert "nndownload patched" in captured.out


def test_doctor_session_cookie_missing_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    """Doctor FAIL when session-cookie file is missing."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Config with session-cookie pointing to a nonexistent file.
    write_ytdlp_profile(config_dir)
    write_nnconf_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
sources:
  - name: nico-src
    url: https://www.nicovideo.jp/mylist/12345678
    output_dir: {output_dir}
    downloader: nndownload
    profile: niconico-archive
    update_strategy: full
    auth: "--session-cookie /nonexistent/cookie.txt"
    extra_args: []
    enabled: true
"""
    )
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_nndownload(
        bin_dir,
        help_text="--download-archive FILE",
    )
    write_fake_ytdlp_minimal(bin_dir)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text("#!/bin/sh\nexit 0\n")
    ffmpeg.chmod(ffmpeg.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    result = main(["doctor"])
    assert result == 1

    captured = capsys.readouterr()
    assert "session-cookie file not found" in captured.out


# ---------------------------------------------------------------------------
# §15 add command with --downloader / --auth
# ---------------------------------------------------------------------------


def test_add_command_nndownload_flags(monkeypatch, tmp_path: Path, capsys) -> None:
    """kura add with --downloader nndownload --auth adds an nndownload source."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)

    # init first.
    assert main(["init"]) == 0

    # Need an nnconf profile for niconico-archive (init creates it).
    result = main([
        "add", "nico-test",
        "https://www.nicovideo.jp/mylist/12345678",
        "--dir", str(tmp_path / "output"),
        "--downloader", "nndownload",
        "--auth=-g",
        "--profile", "niconico-archive",
        "--strategy", "full",
    ])
    assert result == 0

    from kura.config import load_config
    config = load_config()
    src = next(s for s in config.sources if s.name == "nico-test")
    assert src.downloader == "nndownload"
    assert src.auth == "-g"
