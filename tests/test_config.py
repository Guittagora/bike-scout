from __future__ import annotations

import json
from pathlib import Path

import pytest

from bike_scout.config import ConfigError, load_settings

EXAMPLE = Path(__file__).parents[1] / "config" / "settings.example.json"


def write(tmp_path, data) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    return path


def test_example_config_is_valid():
    settings = load_settings(EXAMPLE, env={})
    assert settings.search_url.startswith("https://buycycle.com")
    assert settings.email.enabled is False


def test_env_overrides_secrets(tmp_path):
    path = write(tmp_path, {"search_url": "https://buycycle.com/x", "cookie": "from-file"})
    settings = load_settings(path, env={"BUYCYCLE_COOKIE": "from-env", "GMAIL_APP_PASSWORD": "pw"})
    assert settings.cookie == "from-env"
    assert settings.email.app_password == "pw"


def test_unknown_keys_are_ignored(tmp_path, caplog):
    path = write(tmp_path, {"search_url": "https://buycycle.com/x", "typo_key": 1})
    load_settings(path, env={})
    assert "typo_key" in caplog.text


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "search_url"),
        ({"search_url": "buycycle.com"}, "search_url"),
        ({"search_url": "https://b", "max_listings": 0}, "max_listings"),
        ({"search_url": "https://b", "check_method": "ping"}, "check_method"),
        ({"search_url": "https://b", "email": {"enabled": True}}, "missing"),
    ],
)
def test_invalid_configs(tmp_path, data, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, data), env={})


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.json", env={})
