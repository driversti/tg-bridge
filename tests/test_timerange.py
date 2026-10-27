from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tg_bridge.errors import PeriodError
from tg_bridge.timerange import Period, describe_period, parse_period

W = ZoneInfo("Europe/Warsaw")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def parse(days=None, since=None, until=None, now=datetime(2026, 10, 27, 15, 0, tzinfo=W)) -> Period:
    return parse_period(days, since, until, W, now)


def test_days_1_is_today_midnight():
    p = parse(days=1)

    assert p.start == utc(2026, 10, 26, 23)
    assert p.end is None


def test_days_2_is_yesterday_midnight():
    assert parse(days=2).start == utc(2026, 10, 25, 23)


def test_days_across_dst_start():
    now = datetime(2026, 3, 29, 12, 0, tzinfo=W)

    assert parse(days=1, now=now).start == utc(2026, 3, 28, 23)
    assert parse(days=2, now=now).start == utc(2026, 3, 27, 23)


def test_days_across_dst_end():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=W)

    assert parse(days=1, now=now).start == utc(2026, 10, 24, 22)
    assert parse(days=3, now=now).start == utc(2026, 10, 22, 22)


def test_until_date_is_inclusive():
    p = parse(until="2026-10-25")

    assert p.end == utc(2026, 10, 25, 23)
    assert p.start is None


def test_since_until_dates():
    p = parse(since="2026-10-20", until="2026-10-21")

    assert (p.start, p.end) == (utc(2026, 10, 19, 22), utc(2026, 10, 21, 22))


def test_datetime_without_offset_is_local():
    assert parse(since="2026-10-20 14:30").start == utc(2026, 10, 20, 12, 30)


def test_datetime_with_offset_respected():
    assert parse(since="2026-10-20T14:30+00:00").start == utc(2026, 10, 20, 14, 30)


def test_z_suffix():
    assert parse(since="2026-10-20T14:30:15Z").start == utc(2026, 10, 20, 14, 30, 15)


def test_keywords_today_yesterday():
    p = parse(since="yesterday", until="Yesterday")

    assert (p.start, p.end) == (utc(2026, 10, 25, 23), utc(2026, 10, 26, 23))


def test_nothing_given_is_latest():
    assert parse() == Period(None, None)


def test_days_with_since_rejected():
    with pytest.raises(PeriodError):
        parse(days=2, since="today")


def test_days_zero_rejected():
    with pytest.raises(PeriodError):
        parse(days=0)


def test_days_too_large_rejected():
    with pytest.raises(PeriodError):
        parse(days=3651)


def test_garbage_since_rejected():
    with pytest.raises(PeriodError, match="since"):
        parse(since="last tuesday-ish")


def test_start_not_before_end_rejected():
    with pytest.raises(PeriodError):
        parse(since="2026-10-22", until="2026-10-20")


def test_future_since_is_allowed():
    assert parse(since="2030-01-01").start == utc(2029, 12, 31, 23)


def test_describe_period():
    assert describe_period(Period(utc(2026, 10, 25, 23), None), W) == "2026-10-26 00:00 → now"
    assert (
        describe_period(Period(utc(2026, 10, 19, 22), utc(2026, 10, 21, 22)), W)
        == "2026-10-20 00:00 → 2026-10-22 00:00"
    )
    assert describe_period(Period(None, None), W) == "latest messages"
    assert describe_period(Period(None, utc(2026, 10, 21, 22)), W) == "latest before 2026-10-22 00:00"
