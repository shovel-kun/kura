"""Shared fixtures for the kura test suite."""

import pytest

from kura import runner


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
