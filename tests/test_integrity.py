"""Tests for download integrity checks (kura.integrity) and their runner hooks."""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from kura import integrity
from kura.cli import main
from kura.runner import run_source

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

MIB = 1 << 20


def make_video(path: Path) -> Path:
    """Write a small but real matroska file (2s of test pattern + tone)."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
         "-f", "lavfi", "-i", "sine", "-t", "2",
         "-c:v", "mpeg4", "-c:a", "aac", "-y", str(path)],
        check=True,
    )
    return path


# ---------------------------------------------------------------------------
# check_media
# ---------------------------------------------------------------------------


def test_check_media_flags_zero_filled_block(tmp_path: Path) -> None:
    f = tmp_path / "x [abc].mkv"
    f.write_bytes(b"\x01" * MIB + bytes(MIB) + b"\x02" * 1000)
    assert integrity.check_media(f) == "zero-filled block at 1 MiB"


@needs_ffmpeg
def test_check_media_accepts_sound_file(tmp_path: Path) -> None:
    assert integrity.check_media(make_video(tmp_path / "ok [abc].mkv")) is None


@needs_ffmpeg
def test_check_media_flags_truncated_file(tmp_path: Path) -> None:
    f = make_video(tmp_path / "cut [abc].mkv")
    data = f.read_bytes()
    f.write_bytes(data[: len(data) * 6 // 10])
    assert integrity.check_media(f) is not None


@needs_ffmpeg
def test_check_media_flags_header_less_file(tmp_path: Path) -> None:
    f = tmp_path / "junk [abc].mkv"
    f.write_bytes(b"\x07" * 5000)
    assert integrity.check_media(f) == "ffprobe found no streams"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def test_media_files_for_key_skips_fragments_and_sidecars(tmp_path: Path) -> None:
    d = tmp_path / "Title [abc123]"
    d.mkdir()
    for name in (
        "Title [abc123].mkv",
        "Title [abc123].f399.mp4",
        "Title [abc123].f251.webm",
        "Title [abc123].temp.mkv",
        "Title [abc123].mp4.part",
        "Title [abc123].info.json",
        "Title [abc123].webp",
        "Other [zzz999].mkv",
    ):
        (d / name).write_bytes(b"x")
    assert integrity.media_files_for_key(tmp_path, "youtube abc123") == [d / "Title [abc123].mkv"]


def test_media_files_for_key_handles_ids_with_glob_chars(tmp_path: Path) -> None:
    (tmp_path / "T [a-b_c].mp4").write_bytes(b"x")
    assert integrity.media_files_for_key(tmp_path, "youtube a-b_c") == [tmp_path / "T [a-b_c].mp4"]
    assert integrity.media_files_for_key(tmp_path, "malformed") == []


def test_fragment_files(tmp_path: Path) -> None:
    target = tmp_path / "T [id].mkv"
    for name in ("T [id].f399.mp4", "T [id].f251.webm", "T [id].temp.mkv", "T [id].webp", "T [id].mkv"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in integrity.fragment_files(target)] == [
        "T [id].f251.webm",
        "T [id].f399.mp4",
        "T [id].temp.mkv",
    ]


# ---------------------------------------------------------------------------
# postprocess_targets — shapes taken from real yt-dlp logs
# ---------------------------------------------------------------------------


def test_postprocess_targets_failed_merge_of_reused_fragments() -> None:
    lines = [
        "[youtube] Extracting URL: https://www.youtube.com/watch?v=kFqcOlIIpfA\n",
        "[download] A [kFqcOlIIpfA]/A [kFqcOlIIpfA].f399.mp4 has already been downloaded\n",
        "[download] A [kFqcOlIIpfA]/A [kFqcOlIIpfA].f251.webm has already been downloaded\n",
        '[Merger] Merging formats into "A [kFqcOlIIpfA]/A [kFqcOlIIpfA].mkv"\n',
        "ERROR: Conversion failed!\n",
        "[EmbedSubtitle] There aren't any subtitles to embed\n",
        "ERROR: 'streams'\n",
        "[download] Downloading item 11 of 182\n",
    ]
    assert integrity.postprocess_targets(lines) == ["A [kFqcOlIIpfA]/A [kFqcOlIIpfA].mkv"]


def test_postprocess_targets_unreadable_merged_output() -> None:
    lines = [
        '[Merger] Merging formats into "B [DU6iJCRskFU]/B [DU6iJCRskFU].mkv"\n',
        "Deleting original file B [DU6iJCRskFU]/B [DU6iJCRskFU].f251.webm (pass -k to keep)\n",
        "[EmbedSubtitle] There aren't any subtitles to embed\n",
        "ERROR: 'streams'\n",
    ]
    assert integrity.postprocess_targets(lines) == ["B [DU6iJCRskFU]/B [DU6iJCRskFU].mkv"]


def test_postprocess_targets_ignores_successes_and_unrelated_errors() -> None:
    lines = [
        '[Merger] Merging formats into "C [ok]/C [ok].mkv"\n',
        "[EmbedThumbnail] ffmpeg: Adding thumbnail to \"C [ok]/C [ok].mkv\"\n",
        "[download] Downloading item 2 of 3\n",
        "ERROR: 'streams'\n",  # no current target: belongs to nothing we know
        "[youtube] Extracting URL: https://www.youtube.com/watch?v=x\n",
        "ERROR: [youtube] x: Video unavailable\n",
    ]
    assert integrity.postprocess_targets(lines) == []


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


def _setup(monkeypatch, tmp_path: Path, script_body: str) -> tuple:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setenv("KURA_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("KURA_STATE_DIR", str(state_dir))
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "av1-archive.conf").write_text("--continue\n")
    (config_dir / "config.yaml").write_text(
        textwrap.dedent(f"""\
            defaults:
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
        """)
    )
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "yt-dlp"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('2099.01.01'); sys.exit(0)\n"
        "archive = args[args.index('--download-archive') + 1]\n" + textwrap.dedent(script_body)
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config

    return load_config().sources[0], config_dir, state_dir, output_dir


def test_run_drops_corrupt_archived_download(monkeypatch, tmp_path: Path, capsys) -> None:
    """A file archived by yt-dlp but zero-filled on disk is un-archived and deleted."""
    source, config_dir, state_dir, output_dir = _setup(
        monkeypatch,
        tmp_path,
        """
        import os
        os.makedirs("Good [good001]", exist_ok=True)
        os.makedirs("Bad [bad002]", exist_ok=True)
        open("Good [good001]/Good [good001].mkv", "wb").write(b"\\x01" * 2048)
        open("Bad [bad002]/Bad [bad002].mkv", "wb").write(b"\\x01" * (1 << 20) + bytes(1 << 20))
        with open(archive, "a") as f:
            f.write("youtube good001\\nyoutube bad002\\n")
        """,
    )
    # Only the zero check matters here; keep the test independent of ffmpeg.
    monkeypatch.setattr(integrity, "_have", lambda _tool: False)

    assert run_source(source, config_dir, state_dir, notify=False) == 0

    archive_lines = (output_dir / "archive.txt").read_text().split()
    assert "good001" in archive_lines
    assert "bad002" not in archive_lines
    assert (output_dir / "Good [good001]" / "Good [good001].mkv").exists()
    assert not (output_dir / "Bad [bad002]" / "Bad [bad002].mkv").exists()
    assert "corrupt download removed" in capsys.readouterr().out

    with sqlite3.connect(state_dir / "state.db") as conn:
        assert conn.execute("SELECT new_count, failure_summary FROM runs").fetchone() == (
            1,
            "1 corrupt",
        )
        kind, error = conn.execute(
            "SELECT kind, last_error FROM video_failures WHERE video_id = 'bad002'"
        ).fetchone()
        assert kind == "transient"
        assert error.startswith("corrupt download: zero-filled block")


def test_run_removes_corrupt_leftovers_of_failed_merge(monkeypatch, tmp_path: Path) -> None:
    """Corrupt fragments and temp files of a failed merge are removed; sound ones stay."""
    source, config_dir, state_dir, output_dir = _setup(
        monkeypatch,
        tmp_path,
        """
        import os
        d = "A [vid1234]"
        os.makedirs(d, exist_ok=True)
        open(d + "/A [vid1234].f399.mp4", "wb").write(bytes(1 << 20))
        open(d + "/A [vid1234].f251.webm", "wb").write(b"\\x01" * 4096)
        open(d + "/A [vid1234].temp.mkv", "wb").write(b"\\x01" * 4096)
        print(f'[Merger] Merging formats into "{d}/A [vid1234].mkv"')
        print("ERROR: Conversion failed!")
        print("ERROR: 'streams'")
        sys.exit(1)
        """,
    )
    monkeypatch.setattr(integrity, "_have", lambda _tool: False)

    assert run_source(source, config_dir, state_dir, notify=False) == 1

    left = sorted(p.name for p in (output_dir / "A [vid1234]").iterdir())
    assert left == ["A [vid1234].f251.webm"]
