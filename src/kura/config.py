from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from croniter import croniter
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import paths


DEFAULT_PROFILE = "av1-archive"
DEFAULT_SCHEDULE = "0 */6 * * *"
DEFAULT_UPDATE_STRATEGY = "quick"
DEFAULT_FULL_REINDEX_EVERY = "7d"
VALID_UPDATE_STRATEGIES = {"full", "quick", "rss"}

DEFAULT_PROFILE_TEXT = """--continue
--retries 4
--ignore-errors
--write-info-json
--write-description
--write-thumbnail
--write-subs
--sub-langs all
--write-comments
--format vcodec=av01/bestvideo*+bestaudio/best
--format-sort quality,res,fps,hdr:12,vcodec:av01,channels,acodec,abr,asr,proto,ext,hasaud,source,id
--embed-subs
--embed-thumbnail
--embed-chapters
--embed-metadata
--prefer-free-formats
--output "%(title)s [%(id)s]/%(title)s [%(id)s].%(ext)s"
--trim-filenames 200
--match-filter "live_status != 'is_live' & live_status != 'is_upcoming' & live_status != 'post_live'"
"""

DEFAULT_CONFIG_TEXT = """# kura configuration
# Profiles are raw yt-dlp config files in profiles/<name>.conf.
defaults:
  profile: av1-archive
  schedule: "0 */6 * * *"
  update_strategy: quick
  full_reindex_every: 7d

# Add sources with `kura add NAME URL --dir /absolute/path`.
sources: []
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    output_dir: Path
    archive_file: Path
    profile: str
    cookies: str | None
    schedule: str
    update_strategy: str
    extra_args: list[str]
    enabled: bool


@dataclass(frozen=True)
class Config:
    defaults: dict[str, Any]
    sources: list[Source]


def make_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    return yaml


def load_config(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}; run `kura init` first")
    yaml = make_yaml()
    data = yaml.load(path.read_text()) or CommentedMap()
    return config_from_data(data, path.parent)


def config_from_data(data: Any, config_dir: Path | None = None) -> Config:
    config_dir = config_dir or paths.config_dir()
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must contain a mapping")

    defaults = {
        "profile": DEFAULT_PROFILE,
        "schedule": DEFAULT_SCHEDULE,
        "update_strategy": DEFAULT_UPDATE_STRATEGY,
        "full_reindex_every": DEFAULT_FULL_REINDEX_EVERY,
    }
    defaults.update(data.get("defaults") or {})

    sources_data = data.get("sources") or []
    if not isinstance(sources_data, list):
        raise ConfigError("sources must be a list")

    seen_names: set[str] = set()
    sources: list[Source] = []
    for item in sources_data:
        if not isinstance(item, dict):
            raise ConfigError("each source must be a mapping")
        source = _source_from_mapping(item, defaults, config_dir)
        if source.name in seen_names:
            raise ConfigError(f"source {source.name!r}: duplicate name")
        seen_names.add(source.name)
        sources.append(source)

    return Config(defaults=defaults, sources=sources)


def append_source(
    *,
    name: str,
    url: str,
    output_dir: Path,
    profile: str | None = None,
    schedule: str | None = None,
    strategy: str | None = None,
    cookies: str | None = None,
    config_path: Path | None = None,
) -> Source:
    config_path = config_path or paths.config_file()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}; run `kura init` first")
    if not output_dir.is_absolute():
        raise ConfigError(f"source {name!r}: output_dir must be absolute")

    yaml = make_yaml()
    data = yaml.load(config_path.read_text()) or CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError("config.yaml must contain a mapping")

    sources_seq = data.get("sources")
    if sources_seq is None:
        sources_seq = CommentedSeq()
        data["sources"] = sources_seq
    if not isinstance(sources_seq, list):
        raise ConfigError("sources must be a list")

    existing = config_from_data(data, config_path.parent)
    for source in existing.sources:
        if source.name == name:
            raise ConfigError(f"source {name!r}: duplicate name")
        if source.output_dir == output_dir:
            raise ConfigError(f"source {name!r}: output_dir already used by source {source.name!r}")

    new_item = CommentedMap()
    new_item["name"] = name
    new_item["url"] = url
    new_item["output_dir"] = str(output_dir)
    if profile is not None:
        new_item["profile"] = profile
    if schedule is not None:
        new_item["schedule"] = schedule
    if strategy is not None:
        new_item["update_strategy"] = strategy
    if cookies is not None:
        new_item["cookies"] = cookies
    new_item["extra_args"] = CommentedSeq()
    new_item["enabled"] = True
    sources_seq.append(new_item)

    resolved = config_from_data(data, config_path.parent).sources[-1]
    _atomic_yaml_dump(yaml, data, config_path)
    return resolved


def _source_from_mapping(item: dict[str, Any], defaults: dict[str, Any], config_dir: Path) -> Source:
    name = item.get("name")
    if not name:
        raise ConfigError("source <missing name>: name is required")
    url = item.get("url")
    if not url:
        raise ConfigError(f"source {name!r}: url is required")
    output_dir_value = item.get("output_dir")
    if not output_dir_value:
        raise ConfigError(f"source {name!r}: output_dir is required")

    output_dir = Path(str(output_dir_value)).expanduser()
    if not output_dir.is_absolute():
        raise ConfigError(f"source {name!r}: output_dir must be absolute")

    archive_file_value = item.get("archive_file")
    archive_file = Path(str(archive_file_value)).expanduser() if archive_file_value else output_dir / "archive.txt"
    if not archive_file.is_absolute():
        raise ConfigError(f"source {name!r}: archive_file must be absolute")

    profile = str(item.get("profile") or defaults["profile"])
    if not (config_dir / "profiles" / f"{profile}.conf").exists():
        raise ConfigError(f"source {name!r}: unknown profile {profile!r}")

    schedule = str(item.get("schedule") or defaults["schedule"])
    if not croniter.is_valid(schedule):
        raise ConfigError(f"source {name!r}: invalid cron schedule {schedule!r}")

    update_strategy = str(item.get("update_strategy") or defaults["update_strategy"])
    if update_strategy not in VALID_UPDATE_STRATEGIES:
        raise ConfigError(f"source {name!r}: invalid update_strategy {update_strategy!r}")
    if update_strategy == "rss" and not is_youtube_channel_url(str(url)):
        raise ConfigError(f"source {name!r}: rss update_strategy requires a YouTube channel URL")

    extra_args = item.get("extra_args", [])
    if extra_args is None:
        extra_args = []
    if not isinstance(extra_args, list) or not all(isinstance(arg, str) for arg in extra_args):
        raise ConfigError(f"source {name!r}: extra_args must be a list of strings")

    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"source {name!r}: enabled must be true or false")

    cookies = item.get("cookies")
    return Source(
        name=str(name),
        url=str(url),
        output_dir=output_dir,
        archive_file=archive_file,
        profile=profile,
        cookies=str(cookies) if cookies is not None else None,
        schedule=schedule,
        update_strategy=update_strategy,
        extra_args=list(extra_args),
        enabled=enabled,
    )


def is_youtube_channel_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"youtube.com", "m.youtube.com"}:
        return False
    path = parsed.path.rstrip("/")
    return (
        path.startswith("/@")
        or path.startswith("/channel/")
        or path.startswith("/c/")
        or path.startswith("/user/")
    )


def _atomic_yaml_dump(yaml: YAML, data: Any, path: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as tmp:
            yaml.dump(data, tmp)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
