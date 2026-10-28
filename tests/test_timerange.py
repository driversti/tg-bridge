from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tg_bridge.errors import PeriodError
from tg_bridge.timerange import Period, describe_period, iso_week, now_details, now_line, parse_period

W = ZoneInfo("Europe/Warsaw")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def parse(days=None, since=None, until=None, now=datetime(2026, 10, 27, 15, 0, tzinfo=W), period=None) -> Period:
    return parse_period(days, since, until, W, now, period)


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


WED = datetime(2026, 10, 28, 14, 3, tzinfo=W)


def test_period_this_week_starts_monday():
    assert parse(period="this_week", now=WED) == Period(utc(2026, 10, 25, 23), None)


def test_period_last_week_across_dst_end():
    assert parse(period="last_week", now=WED) == Period(utc(2026, 10, 18, 22), utc(2026, 10, 25, 23))


def test_monday_is_this_week():
    now = datetime(2026, 10, 26, 0, 30, tzinfo=W)

    assert parse(period="this_week", now=now).start == utc(2026, 10, 25, 23)


def test_sunday_night_is_last_week_on_monday():
    now = datetime(2026, 11, 2, 10, 0, tzinfo=W)

    assert parse(period="last_week", now=now).end == utc(2026, 11, 1, 23)


def test_this_month_last_month():
    assert parse(period="this_month", now=WED).start == utc(2026, 9, 30, 22)
    assert parse(period="last_month", now=WED) == Period(utc(2026, 8, 31, 22), utc(2026, 9, 30, 22))


def test_last_month_in_january():
    now = datetime(2027, 1, 15, 12, 0, tzinfo=W)

    assert parse(period="last_month", now=now) == Period(utc(2026, 11, 30, 23), utc(2026, 12, 31, 23))


def test_last_month_leap_february():
    now = datetime(2028, 3, 1, 12, 0, tzinfo=W)

    assert parse(period="last_month", now=now) == Period(utc(2028, 1, 31, 23), utc(2028, 2, 29, 23))


def test_this_year_last_year():
    assert parse(period="this_year", now=WED) == Period(utc(2025, 12, 31, 23), None)
    assert parse(period="last_year", now=WED) == Period(utc(2024, 12, 31, 23), utc(2025, 12, 31, 23))


def test_last_week_across_dst_start():
    now = datetime(2026, 4, 1, 12, 0, tzinfo=W)

    assert parse(period="last_week", now=now) == Period(utc(2026, 3, 22, 23), utc(2026, 3, 29, 22))


def test_period_yesterday_is_closed():
    assert parse(period="yesterday", now=WED) == Period(utc(2026, 10, 26, 23), utc(2026, 10, 27, 23))


def test_period_today_is_open():
    assert parse(period="today", now=WED) == Period(utc(2026, 10, 27, 23), None)


def test_keywords_in_since_until():
    assert parse(since="last_month", until="last_month", now=WED) == parse(period="last_month", now=WED)
    assert parse(until="this_week", now=WED).end == utc(2026, 11, 1, 23)


@pytest.mark.parametrize("spelling", ["This Week", "this-week", " this_week "])
def test_keyword_spelling_variants(spelling):
    assert parse(period=spelling, now=WED) == parse(period="this_week", now=WED)


@pytest.mark.parametrize("kwargs", [{"days": 1}, {"since": "2026-10-01"}, {"until": "today"}])
def test_period_with_days_or_since_rejected(kwargs):
    with pytest.raises(PeriodError, match="Use one of"):
        parse(period="today", **kwargs)


def test_unknown_period_lists_keywords():
    with pytest.raises(PeriodError, match="last_year"):
        parse(period="next_week")


def test_bad_since_lists_keywords():
    with pytest.raises(PeriodError, match="this_week.*last_year"):
        parse(since="garbage")


def test_now_line_format():
    assert now_line(utc(2026, 10, 28, 13, 3), W) == "Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)"


def test_now_line_uses_local_date_after_utc_midnight():
    assert now_line(utc(2026, 10, 28, 23, 30), W) == "Now: 2026-10-29 Thu 00:30 (Europe/Warsaw)"


def test_now_line_length_constant():
    jan = now_line(utc(2027, 1, 4, 9, 5), W)
    octo = now_line(utc(2026, 10, 28, 13, 3), W)

    assert len(jan) == len(octo)


def test_now_line_ignores_locale():
    names = [now_line(utc(2026, 10, 26, 12, 0) + timedelta(days=i), W).split()[2] for i in range(7)]

    assert names == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def test_now_details_exact():
    assert now_details(utc(2026, 10, 28, 13, 3), W) == (
        "Today: Wednesday 2026-10-28 · ISO week 2026-W44 · UTC+01:00\n"
        "This week: 2026-10-26 Mon → 2026-11-01 Sun · last week: 2026-10-19 → 2026-10-25\n"
        "This month: 2026-10 (October) · last month: 2026-09 (September)\n"
        "This year: 2026 · last year: 2025 · next year: 2027\n"
        "Period keywords: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_year"
    )


def test_now_details_summer_offset():
    assert "UTC+02:00" in now_details(utc(2026, 7, 15, 10, 0), W).splitlines()[0]


def test_iso_week_year_boundary():
    assert iso_week(date(2027, 1, 1)) == "2026-W53"
