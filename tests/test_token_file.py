import stat
from pathlib import Path

import pytest

from tg_bridge.token_file import BadTokenFile, TokenFile, admin_token_file, mcp_token_file

TOKEN = "t" * 40


def test_rotate_writes_600_and_dir_700(tmp_path):
    tf = TokenFile(tmp_path / "sub" / "x.token")
    token = tf.rotate()
    assert len(token) >= 32 and tf.read() == token
    assert stat.S_IMODE(tf.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tf.path.parent.stat().st_mode) == 0o700


def test_read_strips_whitespace(tmp_path):
    p = tmp_path / "x.token"
    p.write_text(f"{TOKEN}\n  ")
    assert TokenFile(p).read() == TOKEN


@pytest.mark.parametrize("content", ["", "  \n", "short", "x" * 40 + " y" * 5])
def test_empty_short_or_malformed_reads_none(tmp_path, content):
    p = tmp_path / "x.token"
    p.write_text(content)
    assert TokenFile(p).read() is None


def test_missing_reads_none(tmp_path):
    assert TokenFile(tmp_path / "nope").read() is None


def test_read_sees_rotation_by_another_instance(tmp_path):
    a, b = TokenFile(tmp_path / "x.token"), TokenFile(tmp_path / "x.token")
    first = a.rotate()
    assert a.read() == first
    new = b.rotate()
    assert new != first and a.read() == new


def test_read_is_cached_until_file_changes(tmp_path, monkeypatch):
    tf = TokenFile(tmp_path / "x.token")
    tf.rotate()
    calls = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    tf.read()
    tf.read()
    assert len(calls) == 1
    TokenFile(tf.path).rotate()
    tf.read()
    assert len(calls) == 2


def test_load_or_create_creates_then_reuses(tmp_path):
    tf = TokenFile(tmp_path / "x.token")
    first = tf.load_or_create()
    assert tf.load_or_create() == first


@pytest.mark.parametrize("content", ["", "short"])
def test_load_or_create_refuses_bad_file_without_replacing(tmp_path, content):
    p = tmp_path / "x.token"
    p.write_text(content)
    with pytest.raises(BadTokenFile):
        TokenFile(p).load_or_create()
    assert p.read_text() == content


def test_admin_token_file_name_unchanged(tmp_path):
    assert admin_token_file(tmp_path / "tg.session").path.name == "tg.admin-token"


def test_mcp_token_file_name(tmp_path):
    assert mcp_token_file(tmp_path / "tg.session").path.name == "tg.mcp-token"
