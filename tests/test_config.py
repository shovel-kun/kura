from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from kura.config import ConfigError, append_source, config_from_data, load_config


def write_profile(config_dir: Path, name: str = "av1-archive") -> None:
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / f"{name}.conf").write_text("--continue\n")


def base_config(source: dict | None = None) -> dict:
    return {
        "defaults": {
            "profile": "av1-archive",
            "schedule": "0 */6 * * *",
            "update_strategy": "quick",
            "full_reindex_every": "7d",
        },
        "sources": [source] if source else [],
    }


def valid_source(**overrides) -> dict:
    source = {
        "name": "test-channel",
        "url": "https://www.youtube.com/@TestChannel",
        "output_dir": "/archive/test-channel",
        "extra_args": [],
        "enabled": True,
    }
    source.update(overrides)
    return source


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    write_profile(tmp_path)
    return tmp_path


def test_defaults_materialized(config_dir: Path) -> None:
    config = config_from_data(base_config(valid_source()), config_dir)

    source = config.sources[0]
    assert source.profile == "av1-archive"
    assert source.schedule == "0 */6 * * *"
    assert source.update_strategy == "quick"
    assert source.archive_file == Path("/archive/test-channel/archive.txt")
    assert source.enabled is True


def test_notify_defaults_to_true(config_dir: Path) -> None:
    config = config_from_data(base_config(valid_source()), config_dir)
    assert config.defaults["notify"] is True


def test_notify_can_be_disabled(config_dir: Path) -> None:
    data = base_config(valid_source())
    data["defaults"]["notify"] = False
    config = config_from_data(data, config_dir)
    assert config.defaults["notify"] is False


def test_notify_rejects_non_bool(config_dir: Path) -> None:
    data = base_config(valid_source())
    data["defaults"]["notify"] = "yes"
    with pytest.raises(ConfigError, match="defaults.notify"):
        config_from_data(data, config_dir)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (valid_source(name="dupe"), "duplicate name"),
        (valid_source(output_dir="relative/path"), "output_dir must be absolute"),
        (valid_source(archive_file="relative/archive.txt"), "archive_file must be absolute"),
        (valid_source(profile="missing"), "unknown profile"),
        (valid_source(schedule="not cron"), "invalid cron schedule"),
        (valid_source(update_strategy="rss", url="https://www.youtube.com/playlist?list=PL123"), "rss update_strategy"),
    ],
)
def test_validation_errors(config_dir: Path, source: dict, message: str) -> None:
    data = base_config(source)
    if source["name"] == "dupe":
        data["sources"].append(valid_source(name="dupe", output_dir="/archive/other"))

    with pytest.raises(ConfigError, match=message):
        config_from_data(data, config_dir)


def test_add_preserves_comments_and_round_trips(tmp_path: Path) -> None:
    write_profile(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """# top comment
defaults:
  profile: av1-archive  # profile comment
  schedule: "0 */6 * * *"
  update_strategy: quick
  full_reindex_every: 7d

# sources comment
sources: []
"""
    )

    source = append_source(
        name="test-channel",
        url="https://www.youtube.com/@TestChannel",
        output_dir=Path("/archive/test-channel"),
        schedule="0 0 * * *",
        strategy="rss",
        config_path=config_path,
    )

    text = config_path.read_text()
    assert "# top comment" in text
    assert "# profile comment" in text
    assert "# sources comment" in text
    assert source.name == "test-channel"

    loaded = load_config(config_path)
    assert loaded.sources[0].schedule == "0 0 * * *"
    assert loaded.sources[0].update_strategy == "rss"


def test_add_rejects_duplicate_name_and_output_dir(tmp_path: Path) -> None:
    write_profile(tmp_path)
    yaml = YAML()
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as file:
        yaml.dump(base_config(valid_source()), file)

    with pytest.raises(ConfigError, match="duplicate name"):
        append_source(
            name="test-channel",
            url="https://www.youtube.com/@Other",
            output_dir=Path("/archive/other"),
            config_path=config_path,
        )

    with pytest.raises(ConfigError, match="output_dir already used"):
        append_source(
            name="other",
            url="https://www.youtube.com/@Other",
            output_dir=Path("/archive/test-channel"),
            config_path=config_path,
        )
