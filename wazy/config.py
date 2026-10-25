"""Settings for the Wazy MVP.

Everything is optional. With no environment variables and no .env file the MVP
runs on this computer with the rule-based reader only. Real environment
variables always win over values in .env.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python older than 3.9
    ZoneInfo = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TZ = "Europe/Madrid"   # our testers' time zone, so "tomorrow 9am" is right on any server


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env into the environment (comments and blanks ignored)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


class ConfigError(SystemExit):
    """Raised when a setting is present but unusable: the server refuses to start (fail closed)."""


class Config:
    def __init__(self) -> None:
        load_dotenv()
        env = os.environ.get
        self.host = env("WAZY_HOST", "127.0.0.1")
        # WAZY_PORT wins; otherwise PORT, which hosting platforms set for you.
        port_name = "WAZY_PORT" if env("WAZY_PORT") else ("PORT" if env("PORT") else "WAZY_PORT")
        self.port = self._int(port_name, env(port_name, "8765"), 1, 65535)
        self.timezone = self._timezone(env("WAZY_TZ", "").strip())
        self.db_path = env("WAZY_DB", str(ROOT / "data" / "wazy.db"))
        self.anthropic_api_key = env("ANTHROPIC_API_KEY", "").strip()
        self.ai_model = env("WAZY_AI_MODEL", "claude-haiku-4-5-20251001")
        later = env("WAZY_LATER_MINUTES", "").strip()
        self.later_minutes = self._int("WAZY_LATER_MINUTES", later, 1, 24 * 60) if later else None
        self.access_code = env("WAZY_ACCESS_CODE", "").strip()
        if self.access_code and len(self.access_code) < 8:
            raise ConfigError("WAZY_ACCESS_CODE must be at least 8 characters. Refusing to start.")
        if self.anthropic_api_key and not self.anthropic_api_key.startswith("sk-"):
            raise ConfigError("ANTHROPIC_API_KEY does not look like an Anthropic key. Refusing to start.")

    @staticmethod
    def _int(name: str, value: str, low: int, high: int) -> int:
        try:
            number = int(value)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number. Refusing to start.")
        if not low <= number <= high:
            raise ConfigError(f"{name} must be between {low} and {high}. Refusing to start.")
        return number

    @staticmethod
    def _timezone(name: str):
        """The zone every time is read and stored in. A wrong WAZY_TZ stops the server (fail closed)."""
        if ZoneInfo is None:
            if name:
                raise ConfigError("WAZY_TZ needs Python 3.9 or newer. Refusing to start.")
            return None
        try:
            return ZoneInfo(name or DEFAULT_TZ)
        except (ZoneInfoNotFoundError, ValueError):
            if name:
                raise ConfigError(f"WAZY_TZ={name} is not a time zone, e.g. Europe/Madrid. Refusing to start.")
            return None         # no time zone data on this computer: use its local time

    @property
    def timezone_name(self) -> str:
        return str(self.timezone) if self.timezone else "this computer's local time"

    def clock(self) -> Callable[[], datetime]:
        """Now, in the chosen zone, as the plain local time the database stores."""
        zone = self.timezone
        if zone is None:
            return lambda: datetime.now().replace(microsecond=0)
        return lambda: datetime.now(zone).replace(tzinfo=None, microsecond=0)

    @property
    def reader_name(self) -> str:
        return "rules + Claude Haiku" if self.anthropic_api_key else "rules"
