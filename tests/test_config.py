import logging
from pathlib import Path

import pytest

from tg_bridge.config import load_settings
from tg_bridge.errors import ConfigError

CREDS = {"TG_API_ID": "123", "TG_API_HASH": "h-secret"}


def write_config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def test_defaults_applied(tmp_path):
    settings = load_settings(env=CREDS, config_file=tmp_path / "missing.env")

    home = Path.home()
    assert settings.api_id == 123
    assert settings.api_hash == "h-secret"
    assert settings.session_path == home / ".config/tg-bridge/tg-bridge.session"
    assert settings.timezone.key == "Europe/Warsaw"
    assert settings.max_chars == 12000
    assert settings.export_dir == home / "tg-bridge-data/exports"
    assert settings.download_dir == home / "tg-bridge-data/downloads"


def test_reads_config_file(tmp_path):
    cfg = write_config(
        tmp_path / "config.env",
        "TG_API_ID=7\nTG_API_HASH=abc\nTG_BRIDGE_SESSION=~/s/x.session\nTG_BRIDGE_MAX_CHARS=500\n",
    )

    settings = load_settings(env={}, config_file=cfg)

    assert settings.api_id == 7
    assert settings.session_path == Path.home() / "s/x.session"
    assert settings.max_chars == 500


def test_env_overrides_file(tmp_path):
    cfg = write_config(tmp_path / "config.env", "TG_API_ID=7\nTG_API_HASH=abc\nTG_BRIDGE_TZ=UTC\n")

    settings = load_settings(env={"TG_API_ID": "8", "TG_BRIDGE_TZ": "Europe/Kyiv"}, config_file=cfg)

    assert settings.api_id == 8
    assert settings.api_hash == "abc"
    assert settings.timezone.key == "Europe/Kyiv"


def test_missing_api_id_raises(tmp_path):
    cfg = tmp_path / "config.env"

    with pytest.raises(ConfigError) as exc:
        load_settings(env={"TG_API_HASH": "h"}, config_file=cfg)

    assert "TG_API_ID" in str(exc.value)
    assert str(cfg) in str(exc.value)


def test_non_integer_api_id_raises(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_settings(env={"TG_API_ID": "abc", "TG_API_HASH": "h"}, config_file=tmp_path / "c.env")

    assert "TG_API_ID" in str(exc.value)
    assert "abc" not in str(exc.value)


def test_empty_api_hash_raises(tmp_path):
    with pytest.raises(ConfigError, match="TG_API_HASH"):
        load_settings(env={"TG_API_ID": "1", "TG_API_HASH": "  "}, config_file=tmp_path / "c.env")


def test_invalid_timezone_raises(tmp_path):
    with pytest.raises(ConfigError, match="TG_BRIDGE_TZ"):
        load_settings(env={**CREDS, "TG_BRIDGE_TZ": "Mars/Base"}, config_file=tmp_path / "c.env")


@pytest.mark.parametrize("value", ["0", "-5", "lots", "499"])
def test_max_chars_below_500_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="TG_BRIDGE_MAX_CHARS"):
        load_settings(env={**CREDS, "TG_BRIDGE_MAX_CHARS": value}, config_file=tmp_path / "c.env")


def test_max_chars_500_accepted(tmp_path):
    settings = load_settings(env={**CREDS, "TG_BRIDGE_MAX_CHARS": "500"}, config_file=tmp_path / "c.env")

    assert settings.max_chars == 500


def test_max_download_mb_default_is_200_mib(tmp_path):
    settings = load_settings(env=CREDS, config_file=tmp_path / "c.env")

    assert settings.max_download_bytes == 200 * 1024 * 1024


def test_max_download_mb_is_configurable(tmp_path):
    settings = load_settings(env={**CREDS, "TG_BRIDGE_MAX_DOWNLOAD_MB": "5"}, config_file=tmp_path / "c.env")

    assert settings.max_download_bytes == 5 * 1024 * 1024


@pytest.mark.parametrize("value", ["0", "abc", "-5"])
def test_max_download_mb_must_be_positive_integer(tmp_path, value):
    with pytest.raises(ConfigError, match="TG_BRIDGE_MAX_DOWNLOAD_MB"):
        load_settings(env={**CREDS, "TG_BRIDGE_MAX_DOWNLOAD_MB": value}, config_file=tmp_path / "c.env")


def test_repr_masks_hash(tmp_path):
    settings = load_settings(env=CREDS, config_file=tmp_path / "c.env")

    assert "h-secret" not in repr(settings)


def test_group_readable_config_file_warns(tmp_path, caplog):
    cfg = write_config(tmp_path / "config.env", "TG_API_ID=7\nTG_API_HASH=abc\n", mode=0o644)

    with caplog.at_level(logging.WARNING):
        settings = load_settings(env={}, config_file=cfg)

    assert settings.api_id == 7
    assert any("chmod 600" in r.getMessage() for r in caplog.records)
    assert not any("abc" in r.getMessage() for r in caplog.records)


def test_private_config_file_does_not_warn(tmp_path, caplog):
    cfg = write_config(tmp_path / "config.env", "TG_API_ID=7\nTG_API_HASH=abc\n", mode=0o600)

    with caplog.at_level(logging.WARNING):
        load_settings(env={}, config_file=cfg)

    assert caplog.records == []
