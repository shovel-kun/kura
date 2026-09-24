"""Tests for desktop notifications (notify.py) and their runner integration."""
from __future__ import annotations

import json
import os
import shutil
import stat
import textwrap
from datetime import UTC
from pathlib import Path

from kura.cli import main
from kura.config import Source
from kura.notify import send_notification
from kura.runner import run_source


def _python3_dir() -> str:
    """Directory containing the ``python3`` used to run fake-executable scripts.

    Fake executables below use a ``#!/usr/bin/env python3`` shebang, so
    ``python3`` must remain resolvable even in tests that otherwise use a
    tightly restricted PATH (e.g. to guarantee the *real* ``notify-send``
    is not reachable).
    """
    python3 = shutil.which("python3")
    assert python3 is not None, "python3 must be on PATH to run fake executables"
    return str(Path(python3).parent)


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


def make_config(config_dir: Path, output_dir: Path, *, notify: bool | None = None) -> None:
    """Write a minimal config.yaml with one source called 'test-src'."""
    write_profile(config_dir)
    notify_line = f"  notify: {str(notify).lower()}\n" if notify is not None else ""
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: full
  full_reindex_every: 7d
{notify_line}sources:
  - name: test-src
    url: https://www.youtube.com/@TestChannel
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )


def make_source(output_dir: Path) -> Source:
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


def write_fake_ytdlp(
    bin_dir: Path,
    exit_code: int = 0,
    archive_lines: list[str] | None = None,
    extra_output_lines: list[str] | None = None,
) -> None:
    """Write a fake yt-dlp executable to *bin_dir*.

    Appends *archive_lines* to the ``--download-archive`` file and prints
    *extra_output_lines* (e.g. classifiable ERROR lines) before exiting with
    *exit_code*.
    """
    archive_lines = archive_lines or []
    extra_output_lines = extra_output_lines or []
    lines_repr = repr(archive_lines)
    extra_repr = repr(extra_output_lines)

    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys

        args = sys.argv[1:]

        if args == ["--version"]:
            print("2099.01.01")
            sys.exit(0)

        archive = None
        for i, a in enumerate(args):
            if a == "--download-archive":
                archive = args[i + 1]
                break

        if archive is not None:
            for line in {lines_repr}:
                with open(archive, "a") as f:
                    f.write(line + "\\n")

        for line in {extra_repr}:
            print(line)

        sys.exit({exit_code})
    """)

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "yt-dlp"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_notify_send(bin_dir: Path, capture_file: Path, env_capture_file: Path | None = None) -> None:
    """Write a fake notify-send that appends its argv (one JSON line) to *capture_file*.

    If *env_capture_file* is given, also dumps its received environment
    (as JSON) to that file — used to verify the DBUS fallback plumbing.
    """
    env_dump_line = ""
    if env_capture_file is not None:
        env_dump_line = (
            f'json.dump(dict(os.environ), open({str(env_capture_file)!r}, "w"))\n'
        )

    script = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "\n"
        f'with open({str(capture_file)!r}, "a") as f:\n'
        '    f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        f"{env_dump_line}"
        "sys.exit(0)\n"
    )

    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "notify-send"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _read_calls(capture_file: Path) -> list[list[str]]:
    if not capture_file.exists():
        return []
    return [json.loads(line) for line in capture_file.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Runner integration tests
# ---------------------------------------------------------------------------


def test_new_videos_sends_normal_notification(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123", "youtube def456"])
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0

    calls = _read_calls(capture_file)
    assert len(calls) == 1
    argv = calls[0]
    assert "--app-name=kura" in argv
    assert "--urgency=critical" not in argv
    assert "kura: test-src" in argv
    assert any("2 new video(s) archived" in a for a in argv)


def test_crash_exit_sends_critical_notification(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(bin_dir, exit_code=3)
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 3

    calls = _read_calls(capture_file)
    assert len(calls) == 1
    argv = calls[0]
    assert "--urgency=critical" in argv
    assert any("kura: test-src run failed" in a for a in argv)
    assert any("exit 3" in a and "kura logs test-src" in a for a in argv)


def test_nothing_new_no_failures_does_not_notify(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=[])
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0

    assert _read_calls(capture_file) == []


def test_nothing_new_with_failures_notifies(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(
        bin_dir,
        exit_code=0,
        archive_lines=[],
        extra_output_lines=["ERROR: [youtube] abc123: Private video"],
    )
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0

    calls = _read_calls(capture_file)
    assert len(calls) == 1
    argv = calls[0]
    assert "--urgency=critical" not in argv
    assert any("no new videos; 1 permanent" in a for a in argv)


def test_defaults_notify_false_never_calls_notify_send(monkeypatch, tmp_path: Path) -> None:
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir, notify=False)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123"])
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    assert config.defaults["notify"] is False
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0

    assert _read_calls(capture_file) == []


def test_run_command_wires_defaults_notify_false(monkeypatch, tmp_path: Path) -> None:
    """End-to-end: `kura run NAME` respects defaults.notify=false via run_command."""
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir, notify=False)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123"])
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    assert main(["run", "test-src"]) == 0
    assert _read_calls(capture_file) == []


def test_rss_nothing_new_skip_path_does_not_notify(monkeypatch, tmp_path: Path, capsys) -> None:
    """The rss strategy's no-op 'nothing new' check never invokes yt-dlp and never notifies."""
    import kura.runner as runner_mod

    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    write_profile(config_dir)
    (config_dir / "config.yaml").write_text(
        f"""defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: rss
  full_reindex_every: 7d
sources:
  - name: test-src
    url: https://www.youtube.com/@TestChannel
    output_dir: {output_dir}
    extra_args: []
    enabled: true
"""
    )
    assert main(["init"]) == 0

    archive_path = output_dir / "archive.txt"
    archive_path.write_text("youtube vidAAA\n")

    from datetime import datetime

    from kura.state import connect
    db_path = state_dir / "state.db"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO source_state (source, last_full_run_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_full_run_at = excluded.last_full_run_at
            """,
            ("test-src", datetime.now(tz=UTC).isoformat()),
        )

    def fake_resolve_channel_id(source, db):
        return "UCfakeChannelId123456789"

    def fake_fetch(channel_id: str, *, timeout: int = 30) -> list[str]:
        return ["vidAAA"]  # already archived → nothing new

    monkeypatch.setattr(runner_mod, "_resolve_channel_id", fake_resolve_channel_id)
    monkeypatch.setattr(runner_mod, "_fetch_rss_video_ids", fake_fetch)

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "✓ test-src — nothing new" in out
    assert _read_calls(capture_file) == []


def test_notify_send_absent_from_path_run_still_succeeds(monkeypatch, tmp_path: Path) -> None:
    """No notify-send on PATH → send_notification no-ops, run is unaffected."""
    config_dir, state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0

    bin_dir = tmp_path / "bin"
    write_fake_ytdlp(bin_dir, exit_code=0, archive_lines=["youtube abc123"])
    # Deliberately do NOT write a fake notify-send, and exclude the real one
    # (e.g. /usr/bin) from PATH — only bin_dir and python3's own directory
    # (needed to exec the fake yt-dlp's "#!/usr/bin/env python3" shebang).
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + _python3_dir())

    from kura.config import load_config
    config = load_config()
    source = config.sources[0]

    exit_code = run_source(source, config_dir, state_dir, notify=config.defaults.get("notify", True))
    assert exit_code == 0  # no exception raised despite missing notify-send


# ---------------------------------------------------------------------------
# send_notification unit tests
# ---------------------------------------------------------------------------


def test_send_notification_noop_when_notify_send_missing(monkeypatch, tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))  # empty dir, no notify-send
    # Should not raise.
    send_notification("title", "body")


def test_send_notification_dbus_fallback_added_when_missing(monkeypatch, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    env_capture_file = tmp_path / "env.json"
    write_fake_notify_send(bin_dir, capture_file, env_capture_file=env_capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + _python3_dir())

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    send_notification("hello", "world")

    assert env_capture_file.exists()
    child_env = json.loads(env_capture_file.read_text())
    assert child_env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/1000/bus"


def test_send_notification_preserves_existing_dbus_address(monkeypatch, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    env_capture_file = tmp_path / "env.json"
    write_fake_notify_send(bin_dir, capture_file, env_capture_file=env_capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + _python3_dir())

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/existing/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    send_notification("hello", "world")

    child_env = json.loads(env_capture_file.read_text())
    assert child_env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/existing/bus"


# ---------------------------------------------------------------------------
# doctor integration
# ---------------------------------------------------------------------------


def test_doctor_reports_notify_send_found(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0
    capsys.readouterr()

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    main(["doctor"])
    out = capsys.readouterr().out
    assert "✓ notify-send" in out
    # doctor only checks presence — must never invoke it.
    assert _read_calls(capture_file) == []


def test_doctor_warns_when_notify_send_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir)
    assert main(["init"]) == 0
    capsys.readouterr()

    # Simulate notify-send being absent without assuming the python3 directory
    # does not also contain notify-send.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: None if command == "notify-send" else real_which(command),
    )

    main(["doctor"])
    out = capsys.readouterr().out
    assert "! notify-send not found" in out
    assert "libnotify" in out


def test_doctor_skips_notify_check_when_disabled(monkeypatch, tmp_path: Path, capsys) -> None:
    config_dir, _state_dir = set_kura_dirs(monkeypatch, tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    make_config(config_dir, output_dir, notify=False)
    assert main(["init"]) == 0
    capsys.readouterr()

    bin_dir = tmp_path / "bin"
    capture_file = tmp_path / "calls.jsonl"
    write_fake_notify_send(bin_dir, capture_file)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + os.environ.get("PATH", ""))

    main(["doctor"])
    out = capsys.readouterr().out
    assert "notify-send" not in out
