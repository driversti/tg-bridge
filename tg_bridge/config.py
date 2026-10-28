"""Settings from the environment and ~/.config/tg-bridge/config.env (env wins)."""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
}


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

    return Settings(
        api_id=int(api_id_raw),
        api_hash=required("TG_API_HASH"),
        session_path=_path(values["TG_BRIDGE_SESSION"]),
        timezone=timezone,
        max_chars=int(max_chars_raw),
        export_dir=_path(values["TG_BRIDGE_EXPORT_DIR"]),
        download_dir=_path(values["TG_BRIDGE_DOWNLOAD_DIR"]),
        max_download_bytes=int(max_mb_raw) * 1024 * 1024,
    )


def _path(raw: str) -> Path:
    return Path(raw).expanduser()


def _warn_if_readable_by_others(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; run: chmod 600 %s", path, path)
