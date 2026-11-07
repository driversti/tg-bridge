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


def test_scope_defaults_to_public(tmp_path):
    assert load_settings(env=CREDS, config_file=tmp_path / "m").scope == "public"


@pytest.mark.parametrize("value", ["all", " ALL ", "Public"])
def test_scope_accepts_all_and_public_loosely(tmp_path, value):
    settings = load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": value}, config_file=tmp_path / "m")
    assert settings.scope == value.strip().lower()


@pytest.mark.parametrize("value", ["private", "", "none"])
def test_scope_rejects_unknown_value(tmp_path, value):
    with pytest.raises(ConfigError, match="TG_BRIDGE_SCOPE"):
        load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": value}, config_file=tmp_path / "m")


def test_admin_port_default(tmp_path):
    assert load_settings(env=CREDS, config_file=tmp_path / "m").admin_port == 8765


def test_admin_port_configurable(tmp_path):
    assert load_settings(env={**CREDS, "TG_BRIDGE_ADMIN_PORT": "9000"}, config_file=tmp_path / "m").admin_port == 9000


@pytest.mark.parametrize("value", ["0", "70000", "abc", "-1"])
def test_admin_port_rejected(tmp_path, value):
    with pytest.raises(ConfigError, match="TG_BRIDGE_ADMIN_PORT"):
        load_settings(env={**CREDS, "TG_BRIDGE_ADMIN_PORT": value}, config_file=tmp_path / "m")


# --- network mode settings (feature 11) ---

from tg_bridge.config import check_network_settings, notify_bot_id  # noqa: E402

BOT_TOKEN = "123456:ABC-secret-part"


def _load(tmp_path, **extra):
    return load_settings(env={**CREDS, **extra}, config_file=tmp_path / "missing.env")


def test_network_defaults(tmp_path):
    settings = _load(tmp_path)
    assert settings.device_name == "tg-bridge"
    assert settings.mcp_host is None
    assert settings.mcp_port == 8766
    assert settings.admin_url is None
    assert settings.notify_bot_token is None
    assert settings.notify_chat_id is None
    assert settings.net_allow_all is False


def test_bot_token_not_in_repr(tmp_path):
    settings = _load(tmp_path, TG_BRIDGE_NOTIFY_BOT_TOKEN=BOT_TOKEN)
    assert "ABC-secret-part" not in repr(settings)
    assert notify_bot_id(settings) == 123456


def test_notify_bot_id_none_without_token(tmp_path):
    assert notify_bot_id(_load(tmp_path)) is None


def test_bad_bot_token_names_variable_not_value(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, TG_BRIDGE_NOTIFY_BOT_TOKEN="abc")
    assert "TG_BRIDGE_NOTIFY_BOT_TOKEN" in str(exc.value)
    assert "abc" not in str(exc.value)


@pytest.mark.parametrize("url", ["https://h.ts.net/admin", "https://h.ts.net/?x=1", "ftp://h", "h.ts.net", "https://"])
def test_admin_url_refused(tmp_path, url):
    with pytest.raises(ConfigError) as exc:
        _load(tmp_path, TG_BRIDGE_ADMIN_URL=url)
    assert "TG_BRIDGE_ADMIN_URL" in str(exc.value)


def test_admin_url_accepted(tmp_path):
    settings = _load(tmp_path, TG_BRIDGE_ADMIN_URL="https://tg-bridge.tail4be1a2.ts.net")
    assert settings.admin_url == "https://tg-bridge.tail4be1a2.ts.net"


def test_chat_id_may_be_negative(tmp_path):
    assert _load(tmp_path, TG_BRIDGE_NOTIFY_CHAT_ID="-100123").notify_chat_id == -100123
    with pytest.raises(ConfigError, match="TG_BRIDGE_NOTIFY_CHAT_ID"):
        _load(tmp_path, TG_BRIDGE_NOTIFY_CHAT_ID="x")


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_bad_device_name(tmp_path, name):
    with pytest.raises(ConfigError, match="TG_BRIDGE_DEVICE_NAME"):
        _load(tmp_path, TG_BRIDGE_DEVICE_NAME=name)


@pytest.mark.parametrize("port", ["0", "70000", "x"])
def test_bad_mcp_port(tmp_path, port):
    with pytest.raises(ConfigError, match="TG_BRIDGE_MCP_PORT"):
        _load(tmp_path, TG_BRIDGE_MCP_PORT=port)


def test_bad_allow_all(tmp_path):
    with pytest.raises(ConfigError, match="TG_BRIDGE_NET_ALLOW_ALL"):
        _load(tmp_path, TG_BRIDGE_NET_ALLOW_ALL="yes")


@pytest.mark.parametrize(
    ("extra", "var"),
    [
        ({}, "TG_BRIDGE_MCP_HOST"),
        ({"TG_BRIDGE_MCP_HOST": "0.0.0.0"}, "TG_BRIDGE_MCP_HOST"),
        ({"TG_BRIDGE_MCP_HOST": "::"}, "TG_BRIDGE_MCP_HOST"),
        ({"TG_BRIDGE_MCP_HOST": "*"}, "TG_BRIDGE_MCP_HOST"),
        ({"TG_BRIDGE_MCP_HOST": "192.168.10.22", "TG_BRIDGE_MCP_PORT": "8765"}, "TG_BRIDGE_MCP_PORT"),
        ({"TG_BRIDGE_MCP_HOST": "192.168.10.22", "TG_BRIDGE_SCOPE": "all"}, "TG_BRIDGE_NET_ALLOW_ALL"),
    ],
)
def test_check_network_settings_refuses(tmp_path, extra, var):
    with pytest.raises(ConfigError) as exc:
        check_network_settings(_load(tmp_path, **extra))
    assert var in str(exc.value)


def test_check_network_settings_accepts_lan_ip(tmp_path):
    check_network_settings(_load(tmp_path, TG_BRIDGE_MCP_HOST="192.168.10.22"))


def test_check_network_settings_allows_all_when_forced(tmp_path):
    check_network_settings(
        _load(tmp_path, TG_BRIDGE_MCP_HOST="192.168.10.22", TG_BRIDGE_SCOPE="all", TG_BRIDGE_NET_ALLOW_ALL="1")
    )
