"""Shared fixtures for the kura test suite."""

import os
import stat

import pytest

from kura import runner


@pytest.fixture(scope="session")
def _notify_send_stub_dir(tmp_path_factory):
    """A directory holding a no-op ``notify-send``."""
    bin_dir = tmp_path_factory.mktemp("notify-stub")
    stub = bin_dir / "notify-send"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


@pytest.fixture(autouse=True)
def no_desktop_notifications(_notify_send_stub_dir, monkeypatch):
    """Keep the suite from popping real desktop notifications.

    Most runner tests call run_source with notify left on, and the real
    ``notify-send`` would otherwise post to the developer's desktop.  A
    no-op stub goes first on PATH (tests that install their own fake
    prepend it ahead of this one), and the session bus address points
    nowhere, so a real ``notify-send`` still reachable through a PATH a
    test rebuilds itself (e.g. python3's dir, often /usr/bin) can't
    connect either.  Tests that exercise the DBUS fallback override the
    variable themselves.
    """
    monkeypatch.setenv("PATH", f"{_notify_send_stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/kura-test-bus")


@pytest.fixture
def real_ytdlp_prefix():
    """Opt-out marker for ytdlp_prefix_via_path; request it to keep the
    stock runner.ytdlp_prefix (``sys.executable -m yt_dlp``)."""


@pytest.fixture(autouse=True)
def ytdlp_prefix_via_path(request, monkeypatch):
    """Point runner.ytdlp_prefix at PATH's ``yt-dlp``.

    The suite fakes yt-dlp as an executable script on PATH, which only
    works while the runner's argv starts with a bare ``yt-dlp``.  In
    production the prefix resolves to ``sys.executable -m yt_dlp`` so the
    subprocess and kura's in-process yt_dlp share one version.
    """
    if "real_ytdlp_prefix" in request.fixturenames:
        return
    monkeypatch.setattr(runner, "ytdlp_prefix", lambda: ["yt-dlp"])
