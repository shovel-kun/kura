from __future__ import annotations

import shutil
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen

from kura.archive import MEDIA_EXTENSIONS


def media_target(directory: Path) -> Path:
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lstrip(".").lower() in MEDIA_EXTENSIONS
    ]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else directory


def play(directory: Path) -> None:
    target = media_target(directory)
    command = "mpv" if shutil.which("mpv") else "xdg-open"
    Popen(
        [command, str(target)],
        start_new_session=True,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )


def open_folder(directory: Path) -> None:
    Popen(
        ["xdg-open", str(directory)],
        start_new_session=True,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )


def copy_path(path: Path) -> bool:
    commands = []
    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    for command in commands:
        try:
            process = Popen(command, stdin=PIPE, text=True)
            process.communicate(str(path))
        except OSError:
            continue
        if process.returncode == 0:
            return True
    return False
