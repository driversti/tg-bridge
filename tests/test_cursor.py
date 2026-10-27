from datetime import UTC, datetime

import pytest

from tg_bridge.cursor import Cursor, decode_cursor, encode_cursor
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
