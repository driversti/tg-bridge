import json
import stat

from tests.fakes import make_chat, make_folder, make_person, make_user_chat
from tg_bridge.source.dialog_cache import DialogCache


def chats():
    return [
        make_chat(
            id=-1001, title="Family 🏠", username="famchat", is_forum=True, is_muted=True, is_unread=True, has_unread_mention=True,
            is_creator=True, photo_id=123, top_message_id=4321,
        ),
        make_user_chat(
            make_person(id=7, phone="48123", is_deleted=True, is_contact=True), is_archived=True, last_message_at=None,
            is_self=True,
        ),
    ]


def folders():
    return [
        make_folder(id=2, title="WAR 🪖", emoticon="🪖", groups=True, exclude_read=True, pinned_ids=(7,), include_ids=(-1001, 5)),
        make_folder(id=5, title="F1", kind="shared", exclude_ids=(9,)),
    ]


def test_round_trip(tmp_path):
    cache = DialogCache(tmp_path / "d.json")

    cache.save(chats(), folders(), now=1000.0)

    assert cache.load(now=1100.0, max_age=300) == (chats(), folders(), 1000.0)


def test_file_is_private(tmp_path):
    path = tmp_path / "cfg" / "d.json"

    DialogCache(path).save(chats(), folders(), now=1.0)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["d.json"]  # no temp file left behind


def test_missing_file_is_a_miss(tmp_path):
    assert DialogCache(tmp_path / "d.json").load(now=1.0, max_age=300) is None


def test_stale_is_a_miss(tmp_path):
    cache = DialogCache(tmp_path / "d.json")
    cache.save(chats(), folders(), now=1000.0)

    assert cache.load(now=1301.0, max_age=300) is None


def test_saved_in_the_future_is_a_miss(tmp_path):
    cache = DialogCache(tmp_path / "d.json")
    cache.save(chats(), folders(), now=2000.0)

    assert cache.load(now=1000.0, max_age=300) is None


def test_corrupt_file_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    path.write_text("{not json")

    assert DialogCache(path).load(now=1.0, max_age=300) is None


def test_other_version_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    cache = DialogCache(path)
    cache.save(chats(), folders(), now=1.0)
    data = json.loads(path.read_text())
    data["version"] = 999
    path.write_text(json.dumps(data))

    assert cache.load(now=1.0, max_age=300) is None


def test_unexpected_shape_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"version": 2, "saved_at": 1.0, "chats": [{"id": 1}], "folders": []}))

    assert DialogCache(path).load(now=1.0, max_age=300) is None


def test_version_1_file_is_a_miss(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"version": 1, "saved_at": 1.0, "chats": []}))

    assert DialogCache(path).load(now=1.0, max_age=300) is None


def test_corrupt_folder_entry_is_a_miss_with_warning(tmp_path, caplog):
    path = tmp_path / "d.json"
    cache = DialogCache(path)
    cache.save(chats(), folders(), now=1.0)
    data = json.loads(path.read_text())
    data["folders"][0] = {"id": 2}
    path.write_text(json.dumps(data))

    with caplog.at_level("WARNING"):
        assert cache.load(now=1.0, max_age=300) is None

    assert "unreadable" in caplog.text
    assert "🪖" not in caplog.text


def test_old_file_without_new_fields_loads_with_defaults(tmp_path):
    path = tmp_path / "d.json"
    cache = DialogCache(path)
    cache.save(chats(), folders(), now=1.0)
    data = json.loads(path.read_text())
    for c in data["chats"]:
        for key in ("is_self", "is_creator", "photo_id", "top_message_id"):
            del c[key]
    path.write_text(json.dumps(data))

    loaded, _, _ = cache.load(now=1.0, max_age=300)

    assert all((c.is_self, c.is_creator, c.photo_id, c.top_message_id) == (False, False, None, None) for c in loaded)
