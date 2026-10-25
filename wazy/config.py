"""Settings for the Wazy MVP.

Everything is optional. With no environment variables and no .env file the MVP
runs on this computer with the rule-based reader only. Real environment
variables always win over values in .env.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
        self.port = self._int("WAZY_PORT", env("WAZY_PORT", "8765"), 1, 65535)
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

    @property
    def reader_name(self) -> str:
        return "rules + Claude Haiku" if self.anthropic_api_key else "rules"
