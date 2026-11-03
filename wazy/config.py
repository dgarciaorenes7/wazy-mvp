"""Settings for Wazy, read from environment variables and an optional .env file.

Every setting is optional. With nothing set, Wazy runs on this computer only, with the rule-based
reader and no WhatsApp. Real environment variables always win over the .env file.

Fail closed: a setting that is present but wrong (a port that is not a number, a time zone that does
not exist, half of the Twilio settings...) raises ConfigError with a message naming the setting, and
the server refuses to start. Secret values are never repeated in those messages.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is not supported
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent          # the project folder (where .env lives)
DEFAULT_DOTENV = ROOT / ".env"
DEFAULT_DB = str(ROOT / "data" / "wazy.db")
DEFAULT_TZ = "Europe/Madrid"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_PORT = 8765

# What a good value looks like, setting by setting.
WHOLE_NUMBER = re.compile(r"^[0-9]+$")
HOST = re.compile(r"^[A-Za-z0-9.:-]+$")
CODE = re.compile(r"^[A-Za-z0-9_-]+$")                  # safe in a link and in a cookie
API_KEY = re.compile(r"^sk-ant-[A-Za-z0-9_-]+$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
AUTH_TOKEN = re.compile(r"^[0-9a-fA-F]{32}$")
WHATSAPP_FROM = re.compile(r"^whatsapp:\+[0-9]{6,15}$")
CONTENT_SID = re.compile(r"^HX[0-9a-fA-F]{32}$")
DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TWILIO_REQUIRED = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")


class ConfigError(Exception):
    """A setting is present but unusable. The message names the setting; the server will not start."""


@dataclass(frozen=True)
class Config:
    """Every setting Wazy uses. Secrets are hidden from repr() so they never end up in a log."""
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    tz: str = DEFAULT_TZ
    db_path: str = DEFAULT_DB
    anthropic_api_key: str = field(default="", repr=False)
    ai_model: str = DEFAULT_MODEL
    later_minutes: Optional[int] = None
    access_code: str = field(default="", repr=False)
    admin_code: str = field(default="", repr=False)
    twilio_account_sid: str = ""
    twilio_auth_token: str = field(default="", repr=False)
    twilio_from: str = ""
    twilio_content_sid: str = ""
    public_url: str = ""

    @property
    def twilio_enabled(self) -> bool:
        """True when WhatsApp is switched on (load_config makes sure it is all three or none)."""
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)


# ---------------------------------------------------------------------------------------------
# The .env file
# ---------------------------------------------------------------------------------------------
def parse_dotenv(text: str) -> Dict[str, str]:
    """Read KEY=VALUE lines. Blank lines and # comments are skipped; quotes around a value are removed."""
    values: Dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, equals, value = line.partition("=")
        key = key.strip()
        if not equals or not DOTENV_KEY.match(key):
            raise ConfigError(f".env line {number} is not KEY=VALUE.")
        values[key] = _dotenv_value(value.strip(), number)
    return values


def _dotenv_value(value: str, number: int) -> str:
    """'quoted' or "quoted" keeps everything inside the quotes; otherwise ' #' starts a comment."""
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing == -1:
            raise ConfigError(f".env line {number} opens a quote but never closes it.")
        return value[1:closing]
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment]
    return value.strip()


def read_dotenv(path: Path) -> Dict[str, str]:
    """The settings in a .env file, or nothing if there is no such file."""
    if not path.is_file():
        return {}
    return parse_dotenv(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Loading and checking
# ---------------------------------------------------------------------------------------------
def load_config(env: Optional[Mapping[str, str]] = None, dotenv: Optional[Path] = None) -> Config:
    """Build the Config, or raise ConfigError naming the first bad setting.

    env=None reads the real environment plus the project's .env file (the real environment wins).
    Tests pass env (and, if they want, a dotenv path) so nothing outside the test is read.
    """
    if env is None:
        settings = read_dotenv(dotenv or DEFAULT_DOTENV)
        settings.update(os.environ)
    else:
        settings = read_dotenv(dotenv) if dotenv else {}
        settings.update(env)

    def get(name: str) -> str:
        return str(settings.get(name, "")).strip()

    twilio = _twilio(get)
    public_url = _public_url(get("WAZY_PUBLIC_URL"), twilio_on=bool(twilio["twilio_account_sid"]))
    access_code = _code("WAZY_ACCESS_CODE", get("WAZY_ACCESS_CODE"), 8)
    admin_code = _code("WAZY_ADMIN_CODE", get("WAZY_ADMIN_CODE"), 12)
    if admin_code and admin_code == access_code:
        raise ConfigError("WAZY_ADMIN_CODE must be different from WAZY_ACCESS_CODE.")
    return Config(
        host=_host(get("WAZY_HOST")),
        port=_port(get),
        tz=_zone(get("WAZY_TZ")),
        db_path=get("WAZY_DB") or DEFAULT_DB,
        anthropic_api_key=_api_key(get("ANTHROPIC_API_KEY")),
        ai_model=_model(get("WAZY_AI_MODEL")),
        later_minutes=_later_minutes(get("WAZY_LATER_MINUTES")),
        access_code=access_code,
        admin_code=admin_code,
        public_url=public_url,
        **twilio,
    )


def _host(value: str) -> str:
    if not value:
        return "127.0.0.1"
    if not HOST.match(value):
        raise ConfigError(f"WAZY_HOST '{value}' is not an address, e.g. 127.0.0.1 or 0.0.0.0.")
    return value


def _port(get) -> int:
    """WAZY_PORT wins; otherwise PORT, which hosting platforms set for you; otherwise 8765."""
    name = "WAZY_PORT" if get("WAZY_PORT") else "PORT"
    value = get(name)
    if not value:
        return DEFAULT_PORT
    return _whole_number(name, value, 1, 65535)


def _whole_number(name: str, value: str, low: int, high: int) -> int:
    if not WHOLE_NUMBER.match(value) or not low <= int(value) <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high} (got '{value}').")
    return int(value)


def _zone(value: str) -> str:
    """The zone must exist on this computer, or every time Wazy reads would be wrong."""
    name = value or DEFAULT_TZ
    try:
        ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, or ValueError for names like "../etc"
        raise ConfigError(f"WAZY_TZ '{name}' is not a time zone this computer knows, e.g. Europe/Madrid.")
    return name


def _api_key(value: str) -> str:
    if value and not API_KEY.match(value):
        raise ConfigError("ANTHROPIC_API_KEY does not look like an Anthropic key (it must start with sk-ant-).")
    return value


def _model(value: str) -> str:
    if not value:
        return DEFAULT_MODEL
    if not MODEL.match(value):
        raise ConfigError(f"WAZY_AI_MODEL '{value}' is not a model name, e.g. {DEFAULT_MODEL}.")
    return value


def _later_minutes(value: str) -> Optional[int]:
    if not value:
        return None
    return _whole_number("WAZY_LATER_MINUTES", value, 1, 1440)


def _code(name: str, value: str, shortest: int) -> str:
    if value and (len(value) < shortest or not CODE.match(value)):
        raise ConfigError(f"{name} must be at least {shortest} characters, using only letters, digits, - and _.")
    return value


def _twilio(get) -> Dict[str, str]:
    """WhatsApp settings: the three required ones are all set or all empty. Each must look right."""
    missing = [name for name in TWILIO_REQUIRED if not get(name)]
    content_sid = get("TWILIO_CONTENT_SID")
    if missing and (len(missing) < len(TWILIO_REQUIRED) or content_sid):
        raise ConfigError("WhatsApp needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM "
                          f"together. Missing: {', '.join(missing)}.")
    checks = (("TWILIO_ACCOUNT_SID", ACCOUNT_SID, "AC followed by 32 hex characters"),
              ("TWILIO_AUTH_TOKEN", AUTH_TOKEN, "32 hex characters"),
              ("TWILIO_WHATSAPP_FROM", WHATSAPP_FROM, "whatsapp:+ followed by the number's digits"),
              ("TWILIO_CONTENT_SID", CONTENT_SID, "HX followed by 32 hex characters"))
    for name, pattern, looks_like in checks:
        if get(name) and not pattern.match(get(name)):
            raise ConfigError(f"{name} is not valid: it must be {looks_like}.")
    return {"twilio_account_sid": get("TWILIO_ACCOUNT_SID"), "twilio_auth_token": get("TWILIO_AUTH_TOKEN"),
            "twilio_from": get("TWILIO_WHATSAPP_FROM"), "twilio_content_sid": content_sid}


def _public_url(value: str, twilio_on: bool) -> str:
    """The https address people reach this server at. Twilio signs its webhooks with it, so it is required then."""
    if not value:
        if twilio_on:
            raise ConfigError("WAZY_PUBLIC_URL is required when Twilio is set (e.g. https://wazy.example.com).")
        return ""
    parts = urlparse(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() for c in value)):
        raise ConfigError(f"WAZY_PUBLIC_URL '{value}' must be a plain https:// address, e.g. https://wazy.example.com.")
    return value.rstrip("/")
