from datetime import UTC, datetime

import pytest

from tg_bridge.cursor import (
    Cursor,
    ExportCursor,
    decode_cursor,
    decode_export_cursor,
    encode_cursor,
    encode_export_cursor,
)
from tg_bridge.errors import CursorError

START = datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
END = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
CHAT = -1001125352795


def full(**overrides) -> Cursor:
    fields = dict(
        chat_id=CHAT, direction="forward", boundary_id=4807, start=START, end=END, topic_id=12, from_me=True,
    )
    fields.update(overrides)
    return Cursor(**fields)


def test_round_trip():
    assert decode_cursor(encode_cursor(full()), CHAT) == full()
    assert decode_cursor(encode_cursor(full()), CHAT).start.tzinfo is UTC


def test_round_trip_nones():
    c = full(direction="backward", start=None, end=None, topic_id=None, from_me=False)

    assert decode_cursor(encode_cursor(c), CHAT) == c


def test_short_readable_format():
    # Small models garble long opaque strings; a short field-per-letter token copies back reliably.
    c = full(boundary_id=6782037, start=datetime.fromtimestamp(1792969200, UTC), end=None, topic_id=None,
             from_me=False)

    assert encode_cursor(c) == "f6782037_s1792969200_c795"
    assert encode_cursor(full()) == "f4807_s1792969200_e1793142000_t12_m_c795"
    assert encode_cursor(full(direction="backward", start=None, end=None, topic_id=None, from_me=False)) == "b4807_c795"


def test_chat_tag_is_zero_padded():
    assert encode_cursor(full(chat_id=-1001000000007, start=None, end=None, topic_id=None, from_me=False)) == \
        "f4807_c007"


def test_cursor_from_another_chat_says_so():
    token = encode_cursor(full(chat_id=-1001032086354))

    with pytest.raises(CursorError, match="another chat"):
        decode_cursor(token, CHAT)


@pytest.mark.parametrize("token", ["hello!", "", "x4807_c795", "f_c795", "f4807", "f4807_c79", "f4807_q1_c795",
                                   "f4807_s1_s2_c795", "f4807_sabc_c795", "f-5_c795",
                                   "eyJ2IjoxLCJjIjotMTAwMTEyNTM1Mjc5NSJ9"])
def test_decode_rejects_malformed(token):
    with pytest.raises(CursorError, match="Invalid cursor"):
        decode_cursor(token, CHAT)


def test_decode_strips_surrounding_whitespace_and_quotes():
    token = encode_cursor(full())

    assert decode_cursor(f' "{token}" ', CHAT) == full()
    assert decode_cursor(f"`{token}`", CHAT) == full()


# --- search cursor ---

from datetime import UTC, datetime  # noqa: E402

from tg_bridge.cursor import SearchCursor, decode_search_cursor, encode_search_cursor, query_tag  # noqa: E402
from tg_bridge.source import SearchOffset  # noqa: E402

_D = datetime(2026, 10, 20, 9, 10, tzinfo=UTC)


def _sc(scope="chat", chat_id=123456, **kw):
    fields = dict(
        scope=scope, after=SearchOffset(chat_id=chat_id, message_id=4790, date=_D), start=None, end=None, from_me=False
    )
    fields.update(kw)
    return SearchCursor(**fields)


@pytest.mark.parametrize("scope", ["chat", "global"])
@pytest.mark.parametrize("chat_id", [123456, -1001234567890])
def test_search_cursor_round_trip(scope, chat_id):
    c = _sc(scope, chat_id)
    assert decode_search_cursor(encode_search_cursor(c, "адреса"), "адреса") == c


def test_search_cursor_round_trip_with_optional_fields():
    c = _sc(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC), from_me=True)
    token = encode_search_cursor(c, "x")
    assert "_m_" in token and "_s" in token and "_e" in token
    assert decode_search_cursor(token, "x") == c


def test_search_cursor_negative_chat_id_letter():
    token = encode_search_cursor(_sc(chat_id=-1001234567890), "q")
    assert "_n1001234567890_" in token
    assert token.startswith("h4790_d")


def test_query_tag_ignores_case_and_spacing():
    assert query_tag("Моя  Адреса") == query_tag("моя адреса")
    assert len(query_tag("x")) == 3 and query_tag("x").isdigit()


def test_other_query_rejected():
    token = encode_search_cursor(_sc(), "адреса")
    with pytest.raises(CursorError, match="another query") as e:
        decode_search_cursor(token, "паспорт")
    assert e.value.other_query


@pytest.mark.parametrize(
    "token",
    ["garbage", "", "h1_d5_u1", "h1_u1_q123", "h1_d5_d6_u1_q123", "h1_d5_q123", "h1_d5_u1_n1_q123", "f1_d5_u1_q123", "h1_d5_u1_q12"],
)
def test_search_cursor_garbage(token):
    with pytest.raises(CursorError, match="search_messages"):
        decode_search_cursor(token, "x")


def test_get_messages_cursor_error_text_unchanged():
    assert str(CursorError()) == "Invalid cursor. Call get_messages again without cursor."


# --- export cursor -----------------------------------------------------------


STAMP = datetime(2026, 10, 28, 13, 24, 41, tzinfo=UTC)
FIRST = datetime(2025, 9, 27, 8, 58, tzinfo=UTC)
LAST = datetime(2025, 10, 5, 19, 7, 30, tzinfo=UTC)


def export_cursor(**overrides) -> ExportCursor:
    fields = dict(
        chat_id=CHAT, file_stamp=STAMP, file_n=2, boundary_id=19645, start=START, end=END, count=5500, first=FIRST,
        last=LAST,
    )
    fields.update(overrides)
    return ExportCursor(**fields)


def test_export_round_trip():
    c = export_cursor()

    token = encode_export_cursor(c)

    assert token == (
        f"x19645_f{int(STAMP.timestamp())}_n2_k5500_a{int(FIRST.timestamp())}_d{int(LAST.timestamp())}"
        f"_s{int(START.timestamp())}_e{int(END.timestamp())}_c795"
    )
    assert decode_export_cursor(token, CHAT) == c


def test_export_round_trip_whole_history_first_file():
    c = export_cursor(start=None, end=None, file_n=1)

    token = encode_export_cursor(c)

    assert "_n" not in token and "_s" not in token and "_e" not in token
    assert decode_export_cursor(token, CHAT) == c


def test_export_cursor_from_another_chat():
    with pytest.raises(CursorError, match="another chat.*call export_chat again without cursor"):
        decode_export_cursor(encode_export_cursor(export_cursor()), -100111)


@pytest.mark.parametrize(
    "token",
    ["", "x1_c795", "f1_f2_k1_a1_d1_c795", "x1_f2_k1_a1_c795", "x1_f2_k1_a1_d1_d2_c795", f"{'x1_f2_k1_a1_d1_c795'}_q1"],
)
def test_export_cursor_malformed(token):
    with pytest.raises(CursorError, match="Invalid cursor. Call export_chat again without cursor."):
        decode_export_cursor(token, CHAT)


# --- folder search cursor and read cursor ---

from tg_bridge.cursor import ReadCursor, decode_read_cursor, encode_read_cursor, is_read_cursor  # noqa: E402


def test_folder_search_cursor_round_trip():
    c = _sc("folder", 123456, folder_id=5, from_me=True)
    token = encode_search_cursor(c, "x")
    assert token.startswith("o4790_d") and "_k5_m_q" in token
    assert decode_search_cursor(token, "x") == c


def test_search_cursor_without_folder_has_none():
    token = encode_search_cursor(_sc("global"), "x")
    assert token.startswith("g") and "_k" not in token
    assert decode_search_cursor(token, "x").folder_id is None


@pytest.mark.parametrize("token", ["o1_d5_u1_q%s", "h1_d5_u1_k5_q%s", "g1_d5_u1_k5_q%s"])
def test_search_cursor_folder_scope_needs_k(token):
    with pytest.raises(CursorError, match="search_messages"):
        decode_search_cursor(token % query_tag("x"), "x")


def test_other_folder_error_text():
    e = CursorError(other_folder=True)
    assert e.args[0].startswith("This cursor belongs to another folder.")
    assert e.other_folder and not e.other_chat


_RD = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)


def _rc(chat_id=-1001234567890, **kw):
    fields = dict(
        after=SearchOffset(chat_id=chat_id, message_id=4812, date=_RD),
        start=None, end=None, folder_id=None, headlines=False, from_me=False,
    )
    fields.update(kw)
    return ReadCursor(**fields)


@pytest.mark.parametrize("chat_id", [5, -1001234567890])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"start": START},
        {"end": END},
        {"folder_id": 5},
        {"headlines": True},
        {"from_me": True},
        {"start": START, "end": END, "folder_id": 5, "headlines": True, "from_me": True},
    ],
)
def test_read_cursor_round_trip(chat_id, extra):
    c = _rc(chat_id, **extra)
    token = encode_read_cursor(c)
    assert decode_read_cursor(token) == c
    assert decode_read_cursor(token).after.date.tzinfo is UTC


def test_read_cursor_exact_token():
    c = ReadCursor(
        after=SearchOffset(-1001234567890, 4812, datetime.fromtimestamp(1792969200, UTC)),
        start=datetime.fromtimestamp(1792800000, UTC),
        end=datetime.fromtimestamp(1793404800, UTC),
        folder_id=5,
        headlines=True,
        from_me=False,
    )
    assert encode_read_cursor(c) == "r4812_d1792969200_n1001234567890_s1792800000_e1793404800_k5_h"


def test_read_cursor_from_me_token():
    assert encode_read_cursor(_rc(5, from_me=True)).endswith("_u5_m")


@pytest.mark.parametrize(
    "token",
    ["garbage", "", "r1_u1", "r1_d5", "r1_d5_d6_u1", "r1_d5_u1_n1", "r1_d5_u1_h_h", "r1_d5_u1_x3", "f1_d5_u1", "r_d5_u1"],
)
def test_read_cursor_garbage(token):
    with pytest.raises(CursorError, match="Invalid cursor"):
        decode_read_cursor(token)


def test_is_read_cursor():
    assert is_read_cursor("r12_d1_u5")
    assert not is_read_cursor("f6782037_c795")
    assert not is_read_cursor("garbage")
    assert is_read_cursor(' "r12_d1_u5" ')
