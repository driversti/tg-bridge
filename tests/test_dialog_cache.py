import json
import stat

from tests.fakes import make_chat, make_person, make_user_chat
from tg_bridge.source.dialog_cache import DialogCache


def chats():
    return [
        make_chat(id=-1001, title="Family 🏠", username="famchat", is_forum=True),
        make_user_chat(make_person(id=7, phone="48123", is_deleted=True), is_archived=True, last_message_at=None),
    ]


def test_round_trip(tmp_path):
    cache = DialogCache(tmp_path / "d.json")

    cache.save(chats(), now=1000.0)

    assert cache.load(now=1100.0, max_age=300) == (chats(), 1000.0)


def test_file_is_private(tmp_path):
    path = tmp_path / "cfg" / "d.json"

    DialogCache(path).save(chats(), now=1.0)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["d.json"]  # no temp file left behind


def test_missing_file_is_a_miss(tmp_path):
    assert DialogCache(tmp_path / "d.json").load(now=1.0, max_age=300) is None


def test_stale_is_a_miss(tmp_path):
    cache = DialogCache(tmp_path / "d.json")
    cache.save(chats(), now=1000.0)

    assert cache.load(now=1301.0, max_age=300) is None


def test_saved_in_the_future_is_a_miss(tmp_path):
    cache = DialogCache(tmp_path / "d.json")
    cache.save(chats(), now=2000.0)

    assert cache.load(now=1000.0, max_age=300) is None


def test_corrupt_file_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{not json")

    assert DialogCache(path).load(now=1.0, max_age=300) is None


def test_other_version_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    cache = DialogCache(path)
    cache.save(chats(), now=1.0)
    data = json.loads(path.read_text())
    data["version"] = 999
    path.write_text(json.dumps(data))

    assert cache.load(now=1.0, max_age=300) is None


def test_unexpected_shape_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"version": 1, "saved_at": 1.0, "chats": [{"id": 1}]}))

    assert DialogCache(path).load(now=1.0, max_age=300) is None
