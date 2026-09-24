from __future__ import annotations

import os
import re
import shlex
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
VALID_DOWNLOADERS = {"ytdlp", "nndownload"}
NICONICO_VALID_STRATEGIES = {"full", "quick"}

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
--no-write-playlist-metafiles
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

NICONICO_PROFILE_TEXT = """\
# profiles/niconico-archive.nnconf — nndownload arguments, one per line
--dump-metadata
--download-thumbnail
--download-comments
--output-path "{title} [{id}]/{title} [{id}].{ext}"
"""


class ConfigError(ValueError):
    pass


def parse_duration(value: str) -> int:
    """Parse a duration string like ``'7d'`` or ``'12h'`` into seconds.

    Supported units: ``d`` (days) and ``h`` (hours).

    Raises
    ------
    ConfigError
        When *value* is not a valid ``Nd`` or ``Nh`` string.
    """
    m = re.fullmatch(r"(\d+)([dh])", value.strip())
    if not m:
        raise ConfigError(
            f"invalid duration {value!r}: must be a positive integer followed by 'd' or 'h' (e.g. '7d', '12h')"
        )
    amount = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return amount * 86400
    return amount * 3600


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
    full_reindex_every: str
    extra_args: list[str]
    enabled: bool
    downloader: str = "ytdlp"
    auth: str | None = None


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
        "concurrency": 1,
        "notify": True,
    }
    defaults.update(data.get("defaults") or {})

    # Validate full_reindex_every format at load time.
    try:
        parse_duration(str(defaults["full_reindex_every"]))
    except ConfigError as exc:
        raise ConfigError(f"defaults.full_reindex_every: {exc}") from exc

    # Validate concurrency.
    try:
        concurrency = int(defaults["concurrency"])
    except (TypeError, ValueError):
        raise ConfigError(
            f"defaults.concurrency: must be an integer between 1 and 8, got {defaults['concurrency']!r}"
        )
    if not (1 <= concurrency <= 8):
        raise ConfigError(
            f"defaults.concurrency: must be between 1 and 8, got {concurrency}"
        )
    defaults["concurrency"] = concurrency

    # Validate notify.
    if not isinstance(defaults["notify"], bool):
        raise ConfigError(
            f"defaults.notify: must be true or false, got {defaults['notify']!r}"
        )

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
    downloader: str | None = None,
    auth: str | None = None,
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
    if downloader is not None:
        new_item["downloader"] = downloader
    if auth is not None:
        new_item["auth"] = auth
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

    # Downloader field.
    downloader_raw = item.get("downloader") or "ytdlp"
    downloader = str(downloader_raw)
    if downloader not in VALID_DOWNLOADERS:
        raise ConfigError(
            f"source {name!r}: invalid downloader {downloader!r}; must be one of {sorted(VALID_DOWNLOADERS)}"
        )

    # Auth field.
    auth_raw = item.get("auth")
    auth = str(auth_raw) if auth_raw is not None else None

    if auth is not None and downloader == "ytdlp":
        raise ConfigError(
            f"source {name!r}: auth is only valid for nndownload sources; "
            "use cookies for yt-dlp sources"
        )

    if downloader == "nndownload":
        # Validate auth is present and contains an unattended-auth token.
        if not auth:
            raise ConfigError(
                f"source {name!r}: nndownload sources require auth; "
                "without it nndownload would hang prompting for credentials"
            )
        _validate_niconico_auth(name, auth)
        # Validate URL is a supported niconico video URL.
        _validate_niconico_url(name, str(url))

    # The global default profile is a yt-dlp profile; nndownload sources
    # without an explicit profile get the niconico default instead.
    if downloader == "nndownload":
        profile = str(item.get("profile") or "niconico-archive")
    else:
        profile = str(item.get("profile") or defaults["profile"])
    # Profile file extension depends on downloader.
    if downloader == "nndownload":
        profile_path = config_dir / "profiles" / f"{profile}.nnconf"
    else:
        profile_path = config_dir / "profiles" / f"{profile}.conf"
    if not profile_path.exists():
        raise ConfigError(f"source {name!r}: unknown profile {profile!r}")

    schedule = str(item.get("schedule") or defaults["schedule"])
    if not croniter.is_valid(schedule):
        raise ConfigError(f"source {name!r}: invalid cron schedule {schedule!r}")

    update_strategy = str(item.get("update_strategy") or defaults["update_strategy"])
    if update_strategy not in VALID_UPDATE_STRATEGIES:
        raise ConfigError(f"source {name!r}: invalid update_strategy {update_strategy!r}")

    if downloader == "nndownload":
        # nndownload only supports full and quick strategies.
        if update_strategy not in NICONICO_VALID_STRATEGIES:
            raise ConfigError(
                f"source {name!r}: nndownload sources only support full or quick update_strategy "
                f"(got {update_strategy!r})"
            )
    else:
        # ytdlp: quick/rss require a YouTube channel URL.
        if update_strategy in ("rss", "quick") and not is_youtube_channel_url(str(url)):
            raise ConfigError(
                f"source {name!r}: {update_strategy} update_strategy requires a YouTube channel URL"
            )

    # Per-source full_reindex_every (falls back to defaults).
    full_reindex_every_raw = item.get("full_reindex_every") or defaults["full_reindex_every"]
    full_reindex_every = str(full_reindex_every_raw)
    try:
        parse_duration(full_reindex_every)
    except ConfigError as exc:
        raise ConfigError(f"source {name!r}: full_reindex_every: {exc}") from exc

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
        full_reindex_every=full_reindex_every,
        extra_args=list(extra_args),
        enabled=enabled,
        downloader=downloader,
        auth=auth,
    )


def _validate_niconico_auth(name: str, auth: str) -> None:
    """Check that *auth* contains at least one unattended-login token.

    Raises ConfigError with a message explaining the hang-on-prompt risk
    when no recognized token is found.
    """
    # Acceptable tokens: -g, -n, --session-cookie, or both -u and -p.
    has_g = re.search(r"(?:^|\s)-g(?:\s|$)", auth) is not None
    has_n = re.search(r"(?:^|\s)-n(?:\s|$)", auth) is not None
    has_session_cookie = "--session-cookie" in auth
    has_u = re.search(r"(?:^|\s)-u(?:\s|$)", auth) is not None
    has_p = re.search(r"(?:^|\s)-p(?:\s|$)", auth) is not None
    if not (has_g or has_n or has_session_cookie or (has_u and has_p)):
        raise ConfigError(
            f"source {name!r}: nndownload auth must contain one of -g, -n, --session-cookie, "
            "or both -u and -p; without recognized auth nndownload would hang prompting for credentials"
        )


def _validate_niconico_url(name: str, url: str) -> None:
    """Validate that *url* is a supported niconico video-type URL.

    Accepted:
    - www.nicovideo.jp/watch/<id>
    - www.nicovideo.jp/mylist/<id>
    - www.nicovideo.jp/user/<id>  (and /user/<id>/video)
    - www.nicovideo.jp/series/<id>
    - ch.nicovideo.jp/<slug>  (and /<slug>/video)

    Rejected with "videos only":
    - seiga.nicovideo.jp
    - live.nicovideo.jp
    - nicovideo.jp/manga (or /seiga, /live, /comic)
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    host = host.removeprefix("www.")
    path = parsed.path.rstrip("/")

    # Reject known non-video services.
    if host in {"seiga.nicovideo.jp", "live.nicovideo.jp", "comic.nicovideo.jp"}:
        raise ConfigError(
            f"source {name!r}: URL {url!r} is not a video URL — "
            "seiga/live/manga are videos only (use a watch/mylist/user/series/channel URL)"
        )
    if host == "nicovideo.jp" and re.match(r"^/(seiga|live|manga|comic)", path):
        raise ConfigError(
            f"source {name!r}: URL {url!r} is not a video URL — "
            "seiga/live/manga are videos only (use a watch/mylist/user/series/channel URL)"
        )

    # Accept ch.nicovideo.jp/<slug> and /<slug>/video.
    if host == "ch.nicovideo.jp":
        # Must have a non-empty slug segment.
        if re.match(r"^/[^/]+(/video)?/?$", path):
            return
        raise ConfigError(
            f"source {name!r}: URL {url!r} is not a supported nicovideo URL "
            "(use watch/mylist/user/series or ch.nicovideo.jp/<slug>)"
        )

    if host == "nicovideo.jp":
        accepted = (
            re.match(r"^/watch/", path)
            or re.match(r"^/mylist/", path)
            or re.match(r"^/user/\d+(/video)?$", path)
            or re.match(r"^/series/", path)
        )
        if accepted:
            return
        raise ConfigError(
            f"source {name!r}: URL {url!r} is not a supported nicovideo URL "
            "(use watch/mylist/user/series or ch.nicovideo.jp/<slug>)"
        )

    raise ConfigError(
        f"source {name!r}: URL {url!r} is not a supported nicovideo URL "
        "(use watch/mylist/user/series or ch.nicovideo.jp/<slug>)"
    )


def load_nnconf(profile_path: Path) -> list[str]:
    """Parse an nndownload ``.nnconf`` profile file into an argv list.

    Each non-blank, non-comment line is shlex-split and the resulting
    tokens are concatenated in order.
    """
    args: list[str] = []
    for line in profile_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        args.extend(shlex.split(stripped))
    return args


def is_youtube_channel_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    host = host.removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com"}:
        return False
    path = parsed.path.rstrip("/")
    return path.startswith(("/@", "/channel/", "/c/", "/user/"))


def set_source_enabled(name: str, *, enabled: bool, config_path: Path | None = None) -> None:
    """Flip the ``enabled`` flag for *name* in config.yaml, preserving comments.

    Parameters
    ----------
    name:
        The source name to update.
    enabled:
        The new value for the ``enabled`` field.
    config_path:
        Path to config.yaml; defaults to ``paths.config_file()``.

    Raises
    ------
    ConfigError
        If the config file is not found or *name* is not a known source.
    """
    config_path = config_path or paths.config_file()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}; run `kura init` first")

    yaml = make_yaml()
    data = yaml.load(config_path.read_text()) or CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError("config.yaml must contain a mapping")

    sources_seq = data.get("sources") or []
    for item in sources_seq:
        if isinstance(item, dict) and item.get("name") == name:
            item["enabled"] = enabled
            _atomic_yaml_dump(yaml, data, config_path)
            return

    raise ConfigError(f"source {name!r}: not found in config")


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
