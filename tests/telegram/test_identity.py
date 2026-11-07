from tg_bridge.telegram.identity import client_identity


def test_identity_uses_device_name():
    assert client_identity("tg-bridge (homelab)")["device_model"] == "tg-bridge (homelab)"


def test_identity_keeps_other_keys():
    assert {"system_version", "app_version"} <= client_identity("x").keys()
