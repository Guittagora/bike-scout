"""Configuration loading and validation.

Settings live in a JSON file (see ``config/settings.example.json``). Secrets can
be kept out of that file and supplied through environment variables instead
(optionally via a ``.env`` file):

* ``BUYCYCLE_COOKIE``      -> ``cookie``
* ``GMAIL_APP_PASSWORD``   -> ``email.app_password``
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHECK_METHODS = ("head", "full")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class EmailSettings:
    enabled: bool = False
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender: str = ""
    app_password: str = ""
    recipient: str = ""
    only_hot_deals: bool = False
    only_good_deals: bool = True
    # A listing is a "good deal" when price < market_price * good_deal_ratio.
    good_deal_ratio: float = 0.8


@dataclass
class Settings:
    search_url: str
    cookie: str = ""
    sheet_name: str = ""
    credentials_path: str = "config/credentials.json"
    max_listings: int = 20
    delay_seconds: float = 2.5
    db_path: str = "data/history.db"
    check_existing_first: bool = True
    check_method: str = "head"
    email: EmailSettings = field(default_factory=EmailSettings)

    def validate(self) -> None:
        if not self.search_url.startswith("http"):
            raise ConfigError("'search_url' must be a full http(s) URL")
        if self.max_listings <= 0:
            raise ConfigError("'max_listings' must be a positive integer")
        if self.delay_seconds < 0:
            raise ConfigError("'delay_seconds' cannot be negative")
        if self.check_method not in CHECK_METHODS:
            raise ConfigError(f"'check_method' must be one of {CHECK_METHODS}")
        if not 0 < self.email.good_deal_ratio <= 1:
            raise ConfigError("'email.good_deal_ratio' must be in (0, 1]")
        if self.email.enabled:
            missing = [k for k in ("sender", "recipient", "app_password") if not getattr(self.email, k)]
            if missing:
                raise ConfigError(f"Email notifications enabled but missing: {', '.join(missing)}")


def _known_kwargs(cls: type, raw: dict[str, Any], section: str) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - names)
    if unknown:
        log.warning("Ignoring unknown %s keys: %s", section, ", ".join(unknown))
    return {k: v for k, v in raw.items() if k in names}


def load_settings(path: str | Path, env: dict[str, str] | None = None) -> Settings:
    """Load settings from ``path`` and apply environment-variable overrides."""
    env = os.environ if env is None else env
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path} (copy config/settings.example.json to get started)") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if "search_url" not in raw:
        raise ConfigError("'search_url' is required")

    email_raw = raw.pop("email", {}) or {}
    email = EmailSettings(**_known_kwargs(EmailSettings, email_raw, "email"))
    settings = Settings(**_known_kwargs(Settings, raw, "settings"), email=email)

    if env.get("BUYCYCLE_COOKIE"):
        settings.cookie = env["BUYCYCLE_COOKIE"]
    if env.get("GMAIL_APP_PASSWORD"):
        settings.email.app_password = env["GMAIL_APP_PASSWORD"]

    settings.validate()
    return settings
