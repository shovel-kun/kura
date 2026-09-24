"""Tests for the indexed video library: `kura search`, `kura show`, fzf mode."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from kura import library, ui
from kura.cli import main
from kura.config import load_config

# ---------------------------------------------------------------------------
# Shared helpers (patterns mirror test_n2_nndownload_import.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_color(monkeypatch):
    monkeypatch.setenv("KURA_COLOR", "never")
    ui._reset()
    yield
    ui._reset()


def set_kura_dirs(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    return config_dir, state_dir


def write_profiles(config_dir: Path) -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "av1-archive.conf").write_text("--continue\n")
    (profiles / "niconico-archive.nnconf").write_text("--dump-metadata\n")


def make_config(config_dir: Path, sources: list[dict]) -> None:
    """Write config.yaml with the given source mappings."""
    write_profiles(config_dir)
    lines = [
        "defaults:",
        "  profile: av1-archive",
        '  schedule: "0 */6 * * *"',
        "  update_strategy: quick",
        "  full_reindex_every: 7d",
        "sources:",
    ]
    for src in sources:
        lines.append(f"  - name: {src['name']}")
        if src.get("downloader") == "nndownload":
            lines += [
                "    url: https://www.nicovideo.jp/mylist/12345678",
                f"    output_dir: {src['output_dir']}",
                "    downloader: nndownload",
                "    profile: niconico-archive",
                "    update_strategy: full",
                '    auth: "--session-cookie /fake/cookie.txt"',
            ]
        else:
            lines += [
                "    url: https://www.youtube.com/@TestChannel",
                f"    output_dir: {src['output_dir']}",
            ]
        lines += ["    extra_args: []", "    enabled: true"]
    (config_dir / "config.yaml").write_text("\n".join(lines) + "\n")


def write_yt_video(
    output_dir: Path,
    video_id: str,
    title: str,
    *,
    uploader: str | None = "chan-A",
    upload_date: str | None = "20260701",
    duration: int | None = 724,
    media_bytes: int | None = 1234,
    mtime: float | None = None,
) -> Path:
    """Create a yt-dlp style video dir with sidecar (+ optional media). Returns the dir."""
    d = output_dir / f"{title} [{video_id}]"
    d.mkdir(parents=True, exist_ok=True)
    info: dict = {"id": video_id, "title": title, "extractor_key": "Youtube", "_type": "video"}
    if uploader is not None:
        info["uploader"] = uploader
    if upload_date is not None:
        info["upload_date"] = upload_date
    if duration is not None:
        info["duration"] = duration
    sidecar = d / f"{title} [{video_id}].info.json"
    sidecar.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    if media_bytes is not None:
        (d / f"{title} [{video_id}].mkv").write_bytes(b"\x00" * media_bytes)
    if mtime is not None:
        os.utime(sidecar, (mtime, mtime))
    return d


def write_nn_video(
    output_dir: Path,
    video_id: str,
    title: str,
    *,
    uploader: str | None = "nico-user",
    published: str | None = "2007/03/06 00:33",
    duration: int | None = 320,
    media_bytes: int | None = 45_000,
    mtime: float | None = None,
) -> Path:
    """Create an nndownload style video dir with sidecar (+ optional media)."""
    d = output_dir / f"{title} [{video_id}]"
    d.mkdir(parents=True, exist_ok=True)
    data: dict = {"id": video_id, "title": title, "document_url": f"https://nicovideo.jp/watch/{video_id}"}
    if uploader is not None:
        data["uploader"] = uploader
    if published is not None:
        data["published"] = published
    if duration is not None:
        data["duration"] = duration
    sidecar = d / f"{title} [{video_id}].json"
    sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if media_bytes is not None:
        (d / f"{title} [{video_id}].mp4").write_bytes(b"\x00" * media_bytes)
    if mtime is not None:
        os.utime(sidecar, (mtime, mtime))
    return d


def db_rows(state_dir: Path) -> list[dict]:
    with sqlite3.connect(state_dir / "state.db") as conn:
        cols = ", ".join(library.VIDEO_COLUMNS)
        rows = conn.execute(f"SELECT {cols} FROM videos ORDER BY source, archive_key").fetchall()
    return [dict(zip(library.VIDEO_COLUMNS, r)) for r in rows]


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class TestIndexer:
    def _mixed_setup(self, monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        yt_dir = tmp_path / "yt"
        nn_dir = tmp_path / "nn"
        make_config(config_dir, [
            {"name": "yt-src", "output_dir": yt_dir},
            {"name": "nico-src", "output_dir": nn_dir, "downloader": "nndownload"},
        ])
        return config_dir, state_dir, yt_dir, nn_dir

    def test_index_creates_rows_with_correct_fields(self, monkeypatch, tmp_path: Path) -> None:
        _c, state_dir, yt_dir, nn_dir = self._mixed_setup(monkeypatch, tmp_path)
        yt_video_dir = write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video", media_bytes=1234)
        write_nn_video(nn_dir, "sm9", "Nico Video")
        # Playlist-level info.json must be skipped.
        (yt_dir / "channel.info.json").write_text(
            json.dumps({"id": "UCxx", "extractor_key": "YoutubeTab", "_type": "playlist"}),
            encoding="utf-8",
        )
        # Media-less sidecar → size_bytes NULL.
        write_yt_video(yt_dir, "ytBBBBBBBBBB", "No Media", media_bytes=None,
                       uploader=None, upload_date=None, duration=None)
        # Broken info.json must be tolerated silently.
        (yt_dir / "broken.info.json").write_text("{not json", encoding="utf-8")

        assert main(["search"]) == 0
        rows = {r["archive_key"]: r for r in db_rows(state_dir)}

        assert set(rows) == {"youtube ytAAAAAAAAAA", "youtube ytBBBBBBBBBB", "niconico sm9"}

        yt = rows["youtube ytAAAAAAAAAA"]
        assert yt["source"] == "yt-src"
        assert yt["video_id"] == "ytAAAAAAAAAA"
        assert yt["title"] == "Alpha Video"
        assert yt["uploader"] == "chan-A"
        assert yt["upload_date"] == "2026-07-01"  # YYYYMMDD → ISO
        assert yt["duration_s"] == 724
        assert yt["size_bytes"] == 1234
        assert yt["dir"] == str(yt_video_dir.resolve())
        assert yt["downloaded_at"] is not None and "T" in yt["downloaded_at"]

        nn = rows["niconico sm9"]
        assert nn["source"] == "nico-src"
        assert nn["upload_date"] == "2007-03-06"  # parsed from published
        assert nn["duration_s"] == 320
        assert nn["size_bytes"] == 45_000

        no_media = rows["youtube ytBBBBBBBBBB"]
        assert no_media["size_bytes"] is None
        assert no_media["uploader"] is None
        assert no_media["upload_date"] is None
        assert no_media["duration_s"] is None

    def test_incremental_scan_picks_up_only_new_sidecars(self, monkeypatch, tmp_path: Path) -> None:
        _c, state_dir, yt_dir, _n = self._mixed_setup(monkeypatch, tmp_path)
        past = time.time() - 3600
        write_yt_video(yt_dir, "ytOLD1111111", "Old One", mtime=past)
        write_yt_video(yt_dir, "ytOLD2222222", "Old Two", mtime=past)

        source = load_config().sources[0]
        assert library.refresh_index(source) == 2
        assert len(db_rows(state_dir)) == 2

        write_yt_video(yt_dir, "ytNEW3333333", "New Three")
        assert library.refresh_index(source) == 1  # single upsert: only the new sidecar
        assert len(db_rows(state_dir)) == 3

    def test_reindex_rebuilds_and_drops_deleted(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _c, state_dir, yt_dir, _n = self._mixed_setup(monkeypatch, tmp_path)
        keep_dir = write_yt_video(yt_dir, "ytKEEP111111", "Keeper")
        gone_dir = write_yt_video(yt_dir, "ytGONE222222", "Goner")

        assert main(["search"]) == 0
        assert len(db_rows(state_dir)) == 2

        # Deleted-from-disk rows survive incremental scans (known behavior)...
        shutil.rmtree(gone_dir)
        assert main(["search"]) == 0
        assert {r["video_id"] for r in db_rows(state_dir)} == {"ytKEEP111111", "ytGONE222222"}

        # ...until --reindex rebuilds the source's rows from disk.
        assert main(["search", "--reindex"]) == 0
        assert {r["video_id"] for r in db_rows(state_dir)} == {"ytKEEP111111"}
        assert keep_dir.exists()


# ---------------------------------------------------------------------------
# kura search
# ---------------------------------------------------------------------------


class TestSearch:
    def _setup(self, monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        yt_dir = tmp_path / "yt"
        nn_dir = tmp_path / "nn"
        make_config(config_dir, [
            {"name": "yt-src", "output_dir": yt_dir},
            {"name": "nico-src", "output_dir": nn_dir, "downloader": "nndownload"},
        ])
        return yt_dir, nn_dir, state_dir

    def test_free_text_and_matching_title_and_uploader(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Beta Session", uploader="ChanOne")
        write_yt_video(yt_dir, "ytBBBBBBBBBB", "Alpha Gamma Session", uploader="ChanTwo")

        # Both terms must match (AND); case-insensitive.
        assert main(["search", "ALPHA", "beta"]) == 0
        out = capsys.readouterr().out
        assert "Alpha Beta Session" in out
        assert "Alpha Gamma Session" not in out
        assert "1 match" in out

        # A term may match the uploader instead of the title.
        assert main(["search", "alpha", "chantwo"]) == 0
        out = capsys.readouterr().out
        assert "Alpha Gamma Session" in out
        assert "Alpha Beta Session" not in out

    def test_source_filter(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, nn_dir, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "YT Video")
        write_nn_video(nn_dir, "sm9", "Nico Video")

        assert main(["search", "--source", "nico-src"]) == 0
        out = capsys.readouterr().out
        assert "Nico Video" in out
        assert "YT Video" not in out

    def test_unknown_source_errors(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        assert main(["search", "--source", "nope"]) == 1
        assert "unknown source" in capsys.readouterr().out

    def test_since_duration_form(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytRECENT1111", "Recent Video")
        write_yt_video(yt_dir, "ytOLDER22222", "Older Video", mtime=time.time() - 10 * 86400)

        assert main(["search", "--since", "7d"]) == 0
        out = capsys.readouterr().out
        assert "Recent Video" in out
        assert "Older Video" not in out

        # --until 7d → only things downloaded more than 7 days ago.
        assert main(["search", "--until", "7d"]) == 0
        out = capsys.readouterr().out
        assert "Older Video" in out
        assert "Recent Video" not in out

    def test_since_date_form(self, monkeypatch, tmp_path: Path, capsys) -> None:
        from datetime import UTC, datetime, timedelta

        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytRECENT1111", "Recent Video")
        write_yt_video(yt_dir, "ytOLDER22222", "Older Video", mtime=time.time() - 10 * 86400)

        cutoff = (datetime.now(tz=UTC).date() - timedelta(days=3)).isoformat()
        assert main(["search", "--since", cutoff]) == 0
        out = capsys.readouterr().out
        assert "Recent Video" in out
        assert "Older Video" not in out

    def test_uploaded_switch_matches_upload_date(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        # Both downloaded "now" — only upload_date distinguishes them.
        write_yt_video(yt_dir, "ytNEWUP11111", "New Upload", upload_date="20260601")
        write_yt_video(yt_dir, "ytOLDUP22222", "Old Upload", upload_date="20200101")

        assert main(["search", "--since", "2024-01-01", "--uploaded"]) == 0
        out = capsys.readouterr().out
        assert "New Upload" in out
        assert "Old Upload" not in out

        assert main(["search", "--until", "2024-01-01", "--uploaded"]) == 0
        out = capsys.readouterr().out
        assert "Old Upload" in out
        assert "New Upload" not in out

    def test_sort_title_ascending(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytZZZZZZZZZZ", "Zebra")
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "aardvark")

        assert main(["search", "--sort", "title"]) == 0
        out = capsys.readouterr().out
        assert out.index("aardvark") < out.index("Zebra")  # case-insensitive asc

    def test_sort_size_descending(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytSMALL11111", "Small File", media_bytes=100)
        write_yt_video(yt_dir, "ytBIG2222222", "Big File", media_bytes=5_000_000)

        assert main(["search", "--sort", "size"]) == 0
        out = capsys.readouterr().out
        assert out.index("Big File") < out.index("Small File")

    def test_n_limits_and_prints_more_note(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        for i in range(3):
            write_yt_video(yt_dir, f"ytVID{i}0000000"[:12], f"Video {i}")

        assert main(["search", "-n", "1"]) == 0
        out = capsys.readouterr().out
        assert "3 matches" in out
        assert "… and 2 more (-n 0 for all)" in out
        assert sum(1 for line in out.splitlines() if "Video " in line) == 1

        assert main(["search", "-n", "0"]) == 0
        out = capsys.readouterr().out
        assert "more (-n 0 for all)" not in out
        assert sum(1 for line in out.splitlines() if "Video " in line) == 3

    def test_path_prints_bare_dirs_only(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        video_dir = write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["search", "--path"]) == 0
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l]
        assert lines == [str(video_dir.resolve())]
        assert "indexed" not in out

    def test_json_output_is_valid_and_ansi_free(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["search", "--json"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        data = json.loads(out)
        assert isinstance(data, list) and len(data) == 1
        assert set(data[0]) == set(library.VIDEO_COLUMNS)
        assert data[0]["video_id"] == "ytAAAAAAAAAA"

    def test_no_matches_exits_0(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["search", "zzz-not-there"]) == 0
        out = capsys.readouterr().out
        assert "1 indexed" in out
        assert "no matches" in out

    def test_single_source_omits_source_in_meta(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
        yt_dir = tmp_path / "yt"
        make_config(config_dir, [{"name": "yt-src", "output_dir": yt_dir}])
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["search"]) == 0
        out = capsys.readouterr().out
        assert "Alpha Video" in out
        assert "yt-src" not in out  # source cell omitted for single-source configs

    def test_multi_source_shows_source_in_meta(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n, _s = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["search"]) == 0
        out = capsys.readouterr().out
        assert "yt-src" in out


# ---------------------------------------------------------------------------
# kura show
# ---------------------------------------------------------------------------


class TestShow:
    def _setup(self, monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
        config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
        yt_dir = tmp_path / "yt"
        nn_dir = tmp_path / "nn"
        make_config(config_dir, [
            {"name": "yt-src", "output_dir": yt_dir},
            {"name": "nico-src", "output_dir": nn_dir, "downloader": "nndownload"},
        ])
        return yt_dir, nn_dir

    def test_show_by_source_and_id(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir, _n = self._setup(monkeypatch, tmp_path)
        video_dir = write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")

        assert main(["show", "yt-src", "ytAAAAAAAAAA"]) == 0
        out = capsys.readouterr().out
        assert "Alpha Video" in out
        assert "ytAAAAAAAAAA" in out
        assert "youtube" in out
        assert "uploader" in out and "chan-A" in out
        assert "2026-07-01" in out
        assert "downloaded" in out
        assert str(video_dir.resolve()) in out
        assert ".mkv" in out and ".info.json" in out

    def test_show_bare_id_when_unambiguous(self, monkeypatch, tmp_path: Path, capsys) -> None:
        _y, nn_dir = self._setup(monkeypatch, tmp_path)
        write_nn_video(nn_dir, "sm9", "Nico Video")

        assert main(["show", "sm9"]) == 0
        out = capsys.readouterr().out
        assert "Nico Video" in out
        assert "niconico" in out

    def test_show_ambiguous_bare_id_lists_candidates(self, monkeypatch, tmp_path: Path, capsys) -> None:
        config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        make_config(config_dir, [
            {"name": "src-a", "output_dir": dir_a},
            {"name": "src-b", "output_dir": dir_b},
        ])
        write_yt_video(dir_a, "ytDUPE111111", "Copy A")
        write_yt_video(dir_b, "ytDUPE111111", "Copy B")

        assert main(["show", "ytDUPE111111"]) == 1
        out = capsys.readouterr().out
        assert "ambiguous" in out
        assert "src-a" in out and "src-b" in out

    def test_show_unknown_id_exits_1(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        assert main(["show", "nope123"]) == 1
        out = capsys.readouterr().out
        assert "✗" in out

    def test_show_unknown_source_exits_1(self, monkeypatch, tmp_path: Path, capsys) -> None:
        self._setup(monkeypatch, tmp_path)
        assert main(["show", "ghost-src", "ytAAAAAAAAAA"]) == 1
        assert "unknown source" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fzf interactive mode
# ---------------------------------------------------------------------------


class TestFzf:
    def _setup(self, monkeypatch, tmp_path: Path) -> Path:
        config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
        yt_dir = tmp_path / "yt"
        make_config(config_dir, [{"name": "yt-src", "output_dir": yt_dir}])
        return yt_dir

    def _install_fake_fzf(self, monkeypatch, tmp_path: Path, body: str) -> Path:
        """Install a fake fzf on PATH; returns the capture directory."""
        bin_dir = tmp_path / "fakebin"
        capture_dir = tmp_path / "capture"
        bin_dir.mkdir(exist_ok=True)
        capture_dir.mkdir(exist_ok=True)
        script = bin_dir / "fzf"
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FZF_CAPTURE_DIR", str(capture_dir))
        return capture_dir

    def test_missing_fzf_exits_1_with_message(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")
        monkeypatch.setattr(shutil, "which", lambda cmd: None)

        assert main(["search", "-i"]) == 1
        out = capsys.readouterr().out
        assert "fzf not found" in out
        assert "install fzf for interactive mode" in out

    def test_selection_prints_dir(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir = self._setup(monkeypatch, tmp_path)
        video_dir = write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")
        capture_dir = self._install_fake_fzf(
            monkeypatch,
            tmp_path,
            '#!/bin/sh\n'
            'printf \'%s\\n\' "$@" > "$FZF_CAPTURE_DIR/args"\n'
            'cat > "$FZF_CAPTURE_DIR/stdin"\n'
            'head -n 1 "$FZF_CAPTURE_DIR/stdin"\n',
        )

        assert main(["search", "-i"]) == 0
        out = capsys.readouterr().out
        assert str(video_dir.resolve()) in out

        # Rows fed as SOURCE\tVIDEO_ID\tTITLE\tMETA on stdin.
        stdin_text = (capture_dir / "stdin").read_text()
        assert stdin_text.startswith("yt-src\tytAAAAAAAAAA\tAlpha Video\t")

        # fzf invoked with the specified args, including the show preview.
        args_text = (capture_dir / "args").read_text()
        assert "--delimiter" in args_text
        assert "--with-nth" in args_text
        assert "show {1} {2}" in args_text
        assert "--preview-window" in args_text
        assert "right,50%,wrap" in args_text
        assert "--ansi" in args_text

    def test_filters_applied_before_fzf(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")
        write_yt_video(yt_dir, "ytBBBBBBBBBB", "Bravo Video")
        capture_dir = self._install_fake_fzf(
            monkeypatch,
            tmp_path,
            '#!/bin/sh\n'
            'cat > "$FZF_CAPTURE_DIR/stdin"\n'
            'head -n 1 "$FZF_CAPTURE_DIR/stdin"\n',
        )

        assert main(["search", "bravo", "-i"]) == 0
        stdin_text = (capture_dir / "stdin").read_text()
        assert "Bravo Video" in stdin_text
        assert "Alpha Video" not in stdin_text

    def test_fzf_esc_exit_130_is_silent_success(self, monkeypatch, tmp_path: Path, capsys) -> None:
        yt_dir = self._setup(monkeypatch, tmp_path)
        write_yt_video(yt_dir, "ytAAAAAAAAAA", "Alpha Video")
        self._install_fake_fzf(monkeypatch, tmp_path, "#!/bin/sh\ncat > /dev/null\nexit 130\n")

        assert main(["search", "-i"]) == 0
        assert capsys.readouterr().out == ""
