"""Tests for nndownload import support and the suppress extractor default."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kura.cli import main

# ---------------------------------------------------------------------------
# Shared helpers (mirrors patterns from test_archive.py / test_nndownload.py)
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


def write_nnconf_profile(config_dir: Path, name: str = "niconico-archive") -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / f"{name}.nnconf").write_text(
        "--dump-metadata\n"
        "--download-thumbnail\n"
        "--download-comments\n"
        '--output-path "{title} [{id}]/{title} [{id}].{ext}"\n'
    )


def make_ytdlp_config(config_dir: Path, output_dir: Path) -> None:
    """Write a minimal config.yaml with one yt-dlp source called 'yt-src'."""
    write_ytdlp_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: quick
  full_reindex_every: 7d
sources:
  - name: yt-src
    url: https://www.youtube.com/@TestChannel
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )


def make_niconico_config(config_dir: Path, output_dir: Path) -> None:
    """Write a minimal config.yaml with one nndownload source called 'nico-src'."""
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
    auth: "--session-cookie /fake/cookie.txt"
    extra_args: []
    enabled: true
"""
    )


def write_nn_sidecar(directory: Path, video_id: str, title: str | None = None) -> Path:
    """Write an nndownload-style metadata sidecar and return its path.

    The sidecar is ``{title} [{video_id}].json`` (or ``{video_id}.json`` when
    *title* is None), mirroring the real nndownload output template.
    """
    stem = f"{title} [{video_id}]" if title else video_id
    directory.mkdir(parents=True, exist_ok=True)
    sidecar = directory / f"{stem}.json"
    data = {
        "id": video_id,
        "title": title or video_id,
        "uploader": "test-uploader",
        "document_url": f"https://nicovideo.jp/watch/{video_id}",
        "duration": 120,
        "view_count": 1000,
    }
    sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return sidecar


def write_nn_comments(directory: Path, stem: str) -> Path:
    """Write an nndownload comments sidecar (should NEVER be parsed as evidence)."""
    comments = directory / f"{stem}.comments.json"
    # Deliberately give it a valid-looking id to verify it is NOT treated as evidence.
    comments.write_text(
        json.dumps({"id": "sm777", "document_url": "https://nicovideo.jp/watch/sm777"}),
        encoding="utf-8",
    )
    return comments


def write_nn_thumbnail(directory: Path, stem: str) -> Path:
    jpg = directory / f"{stem}.jpg"
    jpg.write_bytes(b"\xff\xd8\xff")  # minimal JPEG magic
    return jpg


def write_nn_media(directory: Path, stem: str, ext: str = "mp4") -> Path:
    media = directory / f"{stem}.{ext}"
    media.write_bytes(b"\x00" * 4)
    return media


def write_ytdlp_info_json(directory: Path, video_id: str, extractor_key: str = "Youtube") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"Video {video_id}"
    path = directory / f"{stem}.info.json"
    path.write_text(
        json.dumps({
            "id": video_id,
            "extractor_key": extractor_key,
            "title": stem,
            "_type": "video",
        }),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# _nndownload_archive_key unit tests
# ---------------------------------------------------------------------------


class TestNndownloadArchiveKey:
    """Unit tests for the _nndownload_archive_key helper."""

    def _key(self, path: Path) -> str | None:
        from kura.commands import _nndownload_archive_key
        return _nndownload_archive_key(path)

    def test_valid_sm_id_returns_key(self, tmp_path: Path) -> None:
        sidecar = write_nn_sidecar(tmp_path, "sm9", "Some Video")
        assert self._key(sidecar) == "niconico sm9"

    def test_valid_so_id_returns_key(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "so123.json"
        sidecar.write_text(json.dumps({
            "id": "so123",
            "document_url": "https://nicovideo.jp/watch/so123",
        }), encoding="utf-8")
        assert self._key(sidecar) == "niconico so123"

    def test_valid_nm_id_returns_key(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "nm456.json"
        sidecar.write_text(json.dumps({
            "id": "nm456",
            "document_url": "https://nicovideo.jp/watch/nm456",
        }), encoding="utf-8")
        assert self._key(sidecar) == "niconico nm456"

    def test_document_url_field_accepted(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "test.json"
        sidecar.write_text(json.dumps({
            "id": "sm1234",
            "document_url": "https://nicovideo.jp/watch/sm1234",
        }), encoding="utf-8")
        assert self._key(sidecar) == "niconico sm1234"

    def test_no_nicovideo_url_returns_none(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "nope.json"
        sidecar.write_text(json.dumps({
            "id": "sm1234",
            "document_url": "https://example.com/watch/sm1234",
        }), encoding="utf-8")
        assert self._key(sidecar) is None

    def test_id_not_matching_pattern_returns_none(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "yt.json"
        sidecar.write_text(json.dumps({
            "id": "dQw4w9WgXcQ",
            "document_url": "https://nicovideo.jp/watch/dQw4w9WgXcQ",
        }), encoding="utf-8")
        assert self._key(sidecar) is None

    def test_missing_id_field_returns_none(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "noid.json"
        sidecar.write_text(json.dumps({
            "document_url": "https://nicovideo.jp/watch/sm9",
        }), encoding="utf-8")
        assert self._key(sidecar) is None

    def test_parse_error_returns_none_silently(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.json"
        bad.write_text("{not valid json", encoding="utf-8")
        assert self._key(bad) is None  # no exception raised

    def test_array_json_returns_none(self, tmp_path: Path) -> None:
        arr = tmp_path / "arr.json"
        arr.write_text(json.dumps(["sm9", "sm10"]), encoding="utf-8")
        assert self._key(arr) is None

    def test_unrelated_json_no_id_returns_none(self, tmp_path: Path) -> None:
        """A random JSON object with no 'id' field is silently ignored."""
        rand = tmp_path / "settings.json"
        rand.write_text(json.dumps({"theme": "dark", "version": 3}), encoding="utf-8")
        assert self._key(rand) is None


# ---------------------------------------------------------------------------
# Import mixed-tree integration tests
# ---------------------------------------------------------------------------


class TestImportMixedTree:
    """kura import handles a tree mixing yt-dlp and nndownload directories."""

    def _build_mixed_tree(self, output_dir: Path) -> dict:
        """
        Build a mixed yt-dlp + nndownload tree.

        Layout:
          yt-dir/            — yt-dlp video with .info.json
          nico-sm1/          — nndownload video dir with sidecar+comments+thumbnail+media
          nico-sm2/          — another nndownload video dir
          unrelated/         — a random .json with no id field (should be ignored)
          comments-only/     — media + .comments.json but NO regular sidecar → unverifiable
        """
        # (a) yt-dlp directory
        yt_dir = output_dir / "yt-dir"
        write_ytdlp_info_json(yt_dir, "ytAAAAAAAAAA", "Youtube")
        (yt_dir / "ytAAAAAAAAAA.mp4").write_bytes(b"\x00" * 4)

        # (b) nndownload dirs
        for vid_id in ("sm1001", "sm1002"):
            title = f"Test Video {vid_id}"
            stem = f"{title} [{vid_id}]"
            nd = output_dir / stem
            nd.mkdir(parents=True, exist_ok=True)
            write_nn_sidecar(nd, vid_id, title)
            write_nn_comments(nd, stem)
            write_nn_thumbnail(nd, stem)
            write_nn_media(nd, stem, "mp4")

        # (c) unrelated JSON — no id field, should be silently ignored
        unrelated_dir = output_dir / "unrelated"
        unrelated_dir.mkdir()
        (unrelated_dir / "config.json").write_text(
            json.dumps({"theme": "dark", "setting": 42}),
            encoding="utf-8",
        )

        # (d) comments-only: media + .comments.json, but no regular .json sidecar
        #     → media should be reported as unverifiable
        comments_only_dir = output_dir / "comments-only-dir"
        comments_only_dir.mkdir()
        (comments_only_dir / "orphan_video.mp4").write_bytes(b"\x00" * 4)
        (comments_only_dir / "orphan_video.comments.json").write_text(
            json.dumps({"id": "sm999", "document_url": "https://nicovideo.jp/watch/sm999"}),
            encoding="utf-8",
        )

        return {
            "yt_key": "youtube ytAAAAAAAAAA",
            "nn_keys": ["niconico sm1001", "niconico sm1002"],
            "unverifiable_name": "orphan_video.mp4",
        }

    def test_import_mixed_tree_appends_all_evidence(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Import appends both yt-dlp and nndownload keys."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        meta = self._build_mixed_tree(output_dir)

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        archive_path = output_dir / "archive.txt"
        content = archive_path.read_text(encoding="utf-8")

        # yt-dlp key present
        assert meta["yt_key"] in content
        # nndownload keys present
        for key in meta["nn_keys"]:
            assert key in content, f"{key!r} missing from archive"

    def test_import_niconico_key_format(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Archive keys for nndownload sidecars use 'niconico' prefix."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        stem = "Some Video [sm9]"
        nd = output_dir / stem
        nd.mkdir()
        write_nn_sidecar(nd, "sm9", "Some Video")
        write_nn_media(nd, stem)

        assert main(["import", "yt-src"]) == 0

        archive_path = output_dir / "archive.txt"
        content = archive_path.read_text(encoding="utf-8")
        assert "niconico sm9" in content

    def test_import_comments_json_not_parsed_as_evidence(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """*.comments.json files are never treated as nndownload evidence."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        nd = output_dir / "viddir"
        nd.mkdir()
        # Only a .comments.json — no regular .json sidecar and no .info.json
        (nd / "video.comments.json").write_text(
            json.dumps({"id": "sm777", "document_url": "https://nicovideo.jp/watch/sm777"}),
            encoding="utf-8",
        )
        (nd / "video.mp4").write_bytes(b"\x00" * 4)

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        # sm777 must NOT appear as evidence (comments only)
        archive_path = output_dir / "archive.txt"
        if archive_path.exists():
            assert "niconico sm777" not in archive_path.read_text(encoding="utf-8")

        # video.mp4 should be reported as unverifiable (no qualifying sidecar)
        assert "video.mp4" in out

    def test_import_unrelated_json_ignored_no_warning(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A .json file without a valid id is silently ignored (no warning)."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        unrelated = output_dir / "unrelated.json"
        unrelated.write_text(json.dumps({"theme": "dark", "count": 3}), encoding="utf-8")

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        # No warning for unrelated JSON
        assert "unrelated.json" not in out
        assert "unparseable" not in out.lower()

    def test_import_unrelated_json_parse_error_silently_skipped(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A broken generic .json is silently skipped (NOT warned about)."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        broken = output_dir / "broken.json"
        broken.write_text("{bad json ~~~", encoding="utf-8")

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        # Generic .json parse errors are silently skipped
        assert "broken.json" not in out

    def test_import_info_json_parse_error_still_warned(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """A broken .info.json continues to be warned about (existing behaviour)."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        corrupt_dir = output_dir / "corrupt"
        corrupt_dir.mkdir()
        (corrupt_dir / "bad.info.json").write_text("{not valid", encoding="utf-8")

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out
        assert "unparseable" in out.lower() or "Warnings" in out

    def test_import_media_with_nn_sidecar_not_unverifiable(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Media file with a qualifying nndownload sidecar is NOT unverifiable."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        stem = "My Video [sm42]"
        nd = output_dir / stem
        nd.mkdir()
        write_nn_sidecar(nd, "sm42", "My Video")
        write_nn_media(nd, stem)

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        # The media file should NOT appear as unverifiable
        assert "unverifiable media" not in out or f"{stem}.mp4" not in out

    def test_import_media_with_only_comments_json_is_unverifiable(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Media with only a .comments.json sibling is unverifiable."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        nd = output_dir / "vid"
        nd.mkdir()
        (nd / "vid.mp4").write_bytes(b"\x00" * 4)
        (nd / "vid.comments.json").write_text(
            json.dumps({"id": "sm100", "document_url": "https://nicovideo.jp/watch/sm100"}),
            encoding="utf-8",
        )

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out
        assert "unverifiable media" in out
        assert "vid.mp4" in out

    def test_import_dry_run_nn_no_changes(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """--dry-run with nndownload sidecars reports would-append count, writes nothing."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        nd = output_dir / "vid [sm55]"
        nd.mkdir()
        write_nn_sidecar(nd, "sm55", "vid")
        write_nn_media(nd, "vid [sm55]")

        assert main(["import", "yt-src", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "would append" in out

        archive_path = output_dir / "archive.txt"
        assert not archive_path.exists()

    def test_import_idempotent_nn(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Running import twice appends 0 on second run (idempotent)."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        nd = output_dir / "vid [sm77]"
        nd.mkdir()
        write_nn_sidecar(nd, "sm77", "vid")

        assert main(["import", "yt-src"]) == 0
        capsys.readouterr()

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out
        assert "appended   0" in out

    def test_import_nn_dedupe_same_id_two_sidecars(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Two sidecars with the same video id → only one archive entry."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        for suffix in ("v1", "v2"):
            nd = output_dir / f"dir-{suffix}"
            nd.mkdir()
            write_nn_sidecar(nd, "sm88", f"dup-{suffix}")

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        # Only 1 unique key
        archive_path = output_dir / "archive.txt"
        lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
        assert lines.count("niconico sm88") == 1

    def test_import_mixed_tree_full(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Full mixed-tree test: yt-dlp + nndownload + unrelated + comments-only."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        meta = self._build_mixed_tree(output_dir)

        assert main(["import", "yt-src"]) == 0
        out = capsys.readouterr().out

        archive_path = output_dir / "archive.txt"
        content = archive_path.read_text(encoding="utf-8")

        # yt-dlp key appended
        assert meta["yt_key"] in content
        # nndownload keys appended
        for key in meta["nn_keys"]:
            assert key in content

        # comments-only video is unverifiable
        assert "unverifiable media" in out
        assert meta["unverifiable_name"] in out

        # No spurious warnings about unrelated.json
        assert "unrelated.json" not in out
        # No spurious warnings about config.json
        assert "config.json" not in out

        # sm777 (from comments-only sidecar) must NOT be in archive
        assert "niconico sm777" not in content


# ---------------------------------------------------------------------------
# suppress extractor default from source tests
# ---------------------------------------------------------------------------


class TestSuppressExtractorDefault:
    """kura suppress defaults extractor based on source.downloader."""

    def test_suppress_ytdlp_source_defaults_youtube(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """yt-dlp source → suppress defaults to 'youtube' extractor."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        result = main(["suppress", "yt-src", "dQw4w9WgXcQ"])
        assert result == 0

        out = capsys.readouterr().out
        assert "youtube dQw4w9WgXcQ" in out

        archive_path = output_dir / "archive.txt"
        assert "youtube dQw4w9WgXcQ" in archive_path.read_text(encoding="utf-8")

    def test_suppress_ytdlp_source_suppressions_row_uses_youtube(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """suppress on ytdlp source inserts suppressions row with 'youtube' key."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        main(["suppress", "yt-src", "abc123"])

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key FROM suppressions WHERE source = 'yt-src'"
            ).fetchone()
        assert row is not None
        assert row[0] == "youtube abc123"

    def test_suppress_nndownload_source_defaults_niconico(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """nndownload source → suppress defaults to 'niconico' extractor."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_niconico_config(config_dir, output_dir)
        assert main(["init"]) == 0

        result = main(["suppress", "nico-src", "sm9"])
        assert result == 0

        out = capsys.readouterr().out
        assert "niconico sm9" in out

        archive_path = output_dir / "archive.txt"
        assert "niconico sm9" in archive_path.read_text(encoding="utf-8")

    def test_suppress_nndownload_source_suppressions_row_uses_niconico(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """suppress on nndownload source inserts suppressions row with 'niconico' key."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_niconico_config(config_dir, output_dir)
        assert main(["init"]) == 0

        main(["suppress", "nico-src", "sm9"])

        db_path = state_dir / "state.db"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT archive_key FROM suppressions WHERE source = 'nico-src'"
            ).fetchone()
        assert row is not None
        assert row[0] == "niconico sm9"

    def test_suppress_explicit_extractor_overrides_default(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Explicit --extractor overrides the source-derived default."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_niconico_config(config_dir, output_dir)
        assert main(["init"]) == 0

        # Even though source is nndownload, explicit --extractor youtube should be used.
        result = main(["suppress", "nico-src", "dQw4w9WgXcQ", "--extractor", "youtube"])
        assert result == 0

        out = capsys.readouterr().out
        assert "youtube dQw4w9WgXcQ" in out

        archive_path = output_dir / "archive.txt"
        assert "youtube dQw4w9WgXcQ" in archive_path.read_text(encoding="utf-8")

    def test_suppress_explicit_extractor_overrides_ytdlp_default(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """Explicit --extractor niconico overrides youtube default on ytdlp source."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_ytdlp_config(config_dir, output_dir)
        assert main(["init"]) == 0

        result = main(["suppress", "yt-src", "sm9", "--extractor", "niconico"])
        assert result == 0

        out = capsys.readouterr().out
        assert "niconico sm9" in out

    def test_suppress_output_mentions_effective_extractor(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        """The confirmation message mentions the extractor that was used."""
        config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        make_niconico_config(config_dir, output_dir)
        assert main(["init"]) == 0

        main(["suppress", "nico-src", "sm42"])

        out = capsys.readouterr().out
        # Should mention "niconico" somewhere in the output
        assert "niconico" in out


def test_nndownload_source_defaults_to_niconico_profile(tmp_path, monkeypatch):
    """An nndownload source without an explicit profile must not inherit the
    yt-dlp default profile (which has no .nnconf file)."""
    from kura import config as config_mod

    config_dir = tmp_path / "config"
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "profiles" / "av1-archive.conf").write_text("--continue\n")
    (config_dir / "profiles" / "niconico-archive.nnconf").write_text("--dump-metadata\n")

    data = {
        "sources": [
            {
                "name": "nn",
                "url": "https://www.nicovideo.jp/user/4/video",
                "output_dir": str(tmp_path / "out"),
                "downloader": "nndownload",
                "auth": "-g",
                "update_strategy": "full",
            }
        ]
    }
    cfg = config_mod.config_from_data(data, config_dir)
    assert cfg.sources[0].profile == "niconico-archive"
