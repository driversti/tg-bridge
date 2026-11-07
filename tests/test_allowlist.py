import json
import logging
import stat

from tg_bridge.source.allowlist import Allowlist, allowlist_path


def test_path_sits_next_to_session(tmp_path):
    assert allowlist_path(tmp_path / "x" / "tg-bridge.session") == tmp_path / "x" / "tg-bridge.allowlist.json"


def test_missing_file_is_empty(tmp_path):
    assert Allowlist(tmp_path / "a.json").load() == frozenset()


def test_set_on_off_round_trip(tmp_path):
    al = Allowlist(tmp_path / "d" / "a.json")
    al.set(-5, True)
    al.set(-1001, True)
    assert al.load() == frozenset({-5, -1001})
    al.set(-5, False)
    assert al.load() == frozenset({-1001})
    al.set(-5, False)  # idempotent
    assert al.load() == frozenset({-1001})


def test_file_is_sorted_versioned_json(tmp_path):
    path = tmp_path / "a.json"
    al = Allowlist(path)
    al.set(3, True)
    al.set(-2, True)
    assert json.loads(path.read_text()) == {"version": 1, "chat_ids": [-2, 3]}


def test_permissions(tmp_path):
    path = tmp_path / "newdir" / "a.json"
    Allowlist(path).set(1, True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_no_temp_files_left(tmp_path):
    al = Allowlist(tmp_path / "a.json")
    al.set(1, True)
    al.set(2, True)
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_bad_files_are_empty_with_content_free_warning(tmp_path, caplog):
    path = tmp_path / "a.json"
    for content in ["{not json SECRET", '{"version": 2, "chat_ids": [1]}', '{"version": 1, "chat_ids": [1, "SECRET"]}',
                    '{"version": 1, "chat_ids": [true]}', "[1]", '{"version": 1}']:
        path.write_text(content)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert Allowlist(path).load() == frozenset()
        assert caplog.records, content
        assert "SECRET" not in caplog.text and "1" not in caplog.messages[0].replace("allowlist", "")


def test_set_over_corrupt_file_starts_fresh(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("garbage")
    al = Allowlist(path)
    al.set(7, True)
    assert al.load() == frozenset({7})
