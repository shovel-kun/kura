from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "kura"


def config_dir() -> Path:
    override = os.environ.get("KURA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / APP_NAME


def state_dir() -> Path:
    override = os.environ.get("KURA_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.yaml"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def profile_file(profile_or_config_dir: str | Path, profile: str | None = None, *, ext: str = "conf") -> Path:
    """Return the path to a profile file (default extension ``.conf``).

    Two calling conventions are supported::

        profile_file("av1-archive")                          # uses config_dir()
        profile_file(config_dir, "av1-archive")              # explicit config_dir
        profile_file(config_dir, "niconico-archive", ext="nnconf")
    """
    if profile is None:
        # Single-arg form: profile_or_config_dir is the profile name.
        return profiles_dir() / f"{profile_or_config_dir}.{ext}"
    # Two-arg form: profile_or_config_dir is the config directory.
    return Path(profile_or_config_dir) / "profiles" / f"{profile}.{ext}"


def state_db(state_dir_path: Path | None = None) -> Path:
    base = state_dir_path if state_dir_path is not None else state_dir()
    return base / "state.db"


def locks_dir(state_dir_path: Path | None = None) -> Path:
    base = state_dir_path if state_dir_path is not None else state_dir()
    return base / "locks"


def logs_dir(state_dir_path: Path | None = None) -> Path:
    base = state_dir_path if state_dir_path is not None else state_dir()
    return base / "logs"
