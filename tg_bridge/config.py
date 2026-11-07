"""Settings from the environment and ~/.config/tg-bridge/config.env (env wins)."""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from tg_bridge.errors import ConfigError

log = logging.getLogger(__name__)

# Floor so the "Now:" line reserve can never leave a tool a zero or negative budget.
MIN_MAX_CHARS = 500

DEFAULTS = {
    "TG_BRIDGE_SESSION": "~/.config/tg-bridge/tg-bridge.session",
    "TG_BRIDGE_TZ": "Europe/Warsaw",
    "TG_BRIDGE_MAX_CHARS": "12000",
    "TG_BRIDGE_EXPORT_DIR": "~/tg-bridge-data/exports",
    "TG_BRIDGE_DOWNLOAD_DIR": "~/tg-bridge-data/downloads",
    "TG_BRIDGE_MAX_DOWNLOAD_MB": "200",
    "TG_BRIDGE_SCOPE": "public",
    "TG_BRIDGE_ADMIN_PORT": "8765",
    "TG_BRIDGE_DEVICE_NAME": "tg-bridge",
    "TG_BRIDGE_MCP_PORT": "8766",
    "TG_BRIDGE_NET_ALLOW_ALL": "0",
}

WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})


def default_config_file() -> Path:
    # Resolved per call so tests (and subprocesses with another HOME) never see the real file.
    return Path.home() / ".config" / "tg-bridge" / "config.env"


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    session_path: Path
    timezone: ZoneInfo
    max_chars: int
    export_dir: Path
    download_dir: Path
    max_download_bytes: int
    scope: Literal["all", "public"]
    admin_port: int
    # Network mode (feature 11). Safe defaults so stdio is unaffected.
    device_name: str = "tg-bridge"
    mcp_host: str | None = None
    mcp_port: int = 8766
    admin_url: str | None = None
    notify_bot_token: str | None = field(default=None, repr=False)
    notify_chat_id: int | None = None
    net_allow_all: bool = False


def load_settings(env: Mapping[str, str] | None = None, config_file: Path | None = None) -> Settings:
    """Load and validate settings; raises ConfigError naming the variable, never its value."""
    env = os.environ if env is None else env
    config_file = default_config_file() if config_file is None else config_file

    values: dict[str, str] = dict(DEFAULTS)
    if config_file.exists():
        _warn_if_readable_by_others(config_file)
        values.update({k: v for k, v in dotenv_values(config_file).items() if v is not None})
    values.update(env)

    def required(name: str) -> str:
        value = values.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is not set (environment or {config_file})")
        return value

    api_id_raw = required("TG_API_ID")
    if not (api_id_raw.isascii() and api_id_raw.isdigit()):
        raise ConfigError(f"TG_API_ID must be an integer (check environment or {config_file})")

    try:
        timezone = ZoneInfo(values["TG_BRIDGE_TZ"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("TG_BRIDGE_TZ is not a known time zone") from None

    max_chars_raw = values["TG_BRIDGE_MAX_CHARS"].strip()
    if not (max_chars_raw.isascii() and max_chars_raw.isdigit()) or int(max_chars_raw) < MIN_MAX_CHARS:
        raise ConfigError(f"TG_BRIDGE_MAX_CHARS must be an integer of at least {MIN_MAX_CHARS}")

    max_mb_raw = values["TG_BRIDGE_MAX_DOWNLOAD_MB"].strip()
    if not (max_mb_raw.isascii() and max_mb_raw.isdigit()) or int(max_mb_raw) <= 0:
        raise ConfigError("TG_BRIDGE_MAX_DOWNLOAD_MB must be a positive integer")

    scope = values["TG_BRIDGE_SCOPE"].strip().lower()
    if scope not in ("all", "public"):
        raise ConfigError("TG_BRIDGE_SCOPE must be all or public")

    port_raw = values["TG_BRIDGE_ADMIN_PORT"].strip()
    if not (port_raw.isascii() and port_raw.isdigit()) or not 1 <= int(port_raw) <= 65535:
        raise ConfigError("TG_BRIDGE_ADMIN_PORT must be an integer from 1 to 65535")

    device_name = values["TG_BRIDGE_DEVICE_NAME"].strip()
    if not 1 <= len(device_name) <= 64:
        raise ConfigError("TG_BRIDGE_DEVICE_NAME must be 1 to 64 characters")

    mcp_port_raw = values["TG_BRIDGE_MCP_PORT"].strip()
    if not (mcp_port_raw.isascii() and mcp_port_raw.isdigit()) or not 1 <= int(mcp_port_raw) <= 65535:
        raise ConfigError("TG_BRIDGE_MCP_PORT must be an integer from 1 to 65535")

    admin_url = values.get("TG_BRIDGE_ADMIN_URL", "").strip() or None
    if admin_url is not None:
        admin_url = _check_admin_url(admin_url)

    bot_token = values.get("TG_BRIDGE_NOTIFY_BOT_TOKEN", "").strip() or None
    if bot_token is not None and not re.fullmatch(r"\d+:\S+", bot_token):
        raise ConfigError("TG_BRIDGE_NOTIFY_BOT_TOKEN must look like <digits>:<rest>")

    chat_id_raw = values.get("TG_BRIDGE_NOTIFY_CHAT_ID", "").strip()
    chat_id: int | None = None
    if chat_id_raw:
        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            raise ConfigError("TG_BRIDGE_NOTIFY_CHAT_ID must be an integer") from None

    allow_all_raw = values["TG_BRIDGE_NET_ALLOW_ALL"].strip()
    if allow_all_raw not in ("0", "1"):
        raise ConfigError("TG_BRIDGE_NET_ALLOW_ALL must be 1 or 0")

    return Settings(
        api_id=int(api_id_raw),
        api_hash=required("TG_API_HASH"),
        session_path=_path(values["TG_BRIDGE_SESSION"]),
        timezone=timezone,
        max_chars=int(max_chars_raw),
        export_dir=_path(values["TG_BRIDGE_EXPORT_DIR"]),
        download_dir=_path(values["TG_BRIDGE_DOWNLOAD_DIR"]),
        max_download_bytes=int(max_mb_raw) * 1024 * 1024,
        scope=scope,  # type: ignore[arg-type]  # validated above
        admin_port=int(port_raw),
        device_name=device_name,
        mcp_host=values.get("TG_BRIDGE_MCP_HOST", "").strip() or None,
        mcp_port=int(mcp_port_raw),
        admin_url=admin_url,
        notify_bot_token=bot_token,
        notify_chat_id=chat_id,
        net_allow_all=allow_all_raw == "1",
    )


def check_network_settings(settings: Settings) -> None:
    """Extra rules for the long-running network process; raises ConfigError naming the variable."""
    if settings.mcp_host is None or settings.mcp_host.strip() in WILDCARD_HOSTS:
        raise ConfigError("TG_BRIDGE_MCP_HOST must be an explicit address (not unset, 0.0.0.0, :: or *)")
    if settings.scope == "all" and not settings.net_allow_all:
        raise ConfigError("network mode refuses TG_BRIDGE_SCOPE=all unless TG_BRIDGE_NET_ALLOW_ALL=1")
    if settings.mcp_port == settings.admin_port:
        raise ConfigError("TG_BRIDGE_MCP_PORT must differ from TG_BRIDGE_ADMIN_PORT")


def notify_bot_id(settings: Settings) -> int | None:
    """The Homelab bot's user id: the numeric prefix of its token."""
    if settings.notify_bot_token is None:
        return None
    return int(settings.notify_bot_token.split(":", 1)[0])


def _check_admin_url(raw: str) -> str:
    parts = urlsplit(raw)
    try:
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        port_ok = False
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or not port_ok
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ConfigError("TG_BRIDGE_ADMIN_URL must look like http(s)://host[:port] with no path or query")
    return f"{parts.scheme}://{parts.netloc}"


def _path(raw: str) -> Path:
    return Path(raw).expanduser()


def _warn_if_readable_by_others(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; run: chmod 600 %s", path, path)
