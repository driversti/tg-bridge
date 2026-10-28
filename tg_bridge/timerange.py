"""Period arguments of the read tools → a UTC range. The model never does date arithmetic."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tg_bridge.errors import PeriodError

MAX_DAYS = 3650
KEYWORDS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "last_year")
# Fixed English names: strftime("%a"/"%B") would follow the process locale.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass(frozen=True)
class Period:
    start: datetime | None  # aware UTC, inclusive; None = from the latest backwards
    end: datetime | None  # aware UTC, exclusive; None = now (open)


def parse_period(
    days: int | None,
    since: str | None,
    until: str | None,
    tz: ZoneInfo,
    now: datetime,
    period: str | None = None,
) -> Period:
    since = (since or "").strip() or None
    until = (until or "").strip() or None
    period = (period or "").strip() or None
    if period is not None:
        if days is not None or since is not None or until is not None:
            raise PeriodError("Use one of `period`, `days` or `since`/`until`.")
        keyword = _keyword(period)
        if keyword is None:
            raise PeriodError(f"Unknown `period`. Use one of: {', '.join(KEYWORDS)}.")
        today = now.astimezone(tz).date()
        first, end = _keyword_days(keyword, today)
        # A period that has not ended yet reads "… → now", like `days`.
        return Period(_midnight(first, tz), None if end > today else _midnight(end, tz))
    if days is not None:
        if since is not None or until is not None:
            raise PeriodError("Use either `days` or `since`/`until`, not both.")
        if not 1 <= days <= MAX_DAYS:
            raise PeriodError(f"`days` must be between 1 and {MAX_DAYS}.")
        # Date arithmetic on the local date: timedelta on an aware datetime would ignore DST shifts.
        first_day = now.astimezone(tz).date() - timedelta(days=days - 1)
        return Period(_midnight(first_day, tz), None)

    start = _parse_moment(since, "since", tz, now, end_of_day=False) if since else None
    end = _parse_moment(until, "until", tz, now, end_of_day=True) if until else None
    if start is not None and end is not None and start >= end:
        raise PeriodError("`since` must be earlier than `until`.")
    return Period(start, end)


def describe_period(period: Period, tz: ZoneInfo) -> str:
    start = _fmt(period.start, tz) if period.start else None
    end = _fmt(period.end, tz) if period.end else None
    if start is None:
        return "latest messages" if end is None else f"latest before {end}"
    return f"{start} → {end or 'now'}"


def _fmt(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def _parse_moment(raw: str, name: str, tz: ZoneInfo, now: datetime, *, end_of_day: bool) -> datetime:
    today = now.astimezone(tz).date()
    keyword = _keyword(raw)
    if keyword is not None:
        first, end = _keyword_days(keyword, today)
        # `since` takes the period's start, `until` its end, which is already exclusive.
        return _midnight(end if end_of_day else first, tz)
    day: date | None = None
    if len(raw) == 10:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            raise _bad(name) from None
    if day is not None:
        # An `until` day is inclusive, so the exclusive bound is the next local midnight.
        return _midnight(day + timedelta(days=1) if end_of_day else day, tz)
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise _bad(name) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(UTC)


def _bad(name: str) -> PeriodError:
    return PeriodError(
        f'Cannot read `{name}`. Use YYYY-MM-DD, "YYYY-MM-DD HH:MM" or a keyword: {", ".join(KEYWORDS)}.'
    )


def _keyword(raw: str) -> str | None:
    # Small models write "this week" or "Last-Month".
    normalized = raw.strip().casefold().replace(" ", "_").replace("-", "_")
    return normalized if normalized in KEYWORDS else None


def _keyword_days(keyword: str, today: date) -> tuple[date, date]:
    """Local dates [first, end) of a keyword period; weeks start on Monday."""
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    prev_month = (month - timedelta(days=1)).replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    year = today.replace(month=1, day=1)
    return {
        "today": (today, today + timedelta(days=1)),
        "yesterday": (today - timedelta(days=1), today),
        "this_week": (monday, monday + timedelta(days=7)),
        "last_week": (monday - timedelta(days=7), monday),
        "this_month": (month, next_month),
        "last_month": (prev_month, month),
        "this_year": (year, year.replace(year=year.year + 1)),
        "last_year": (year.replace(year=year.year - 1), year),
    }[keyword]


def iso_week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def now_line(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz)
    return f"Now: {local:%Y-%m-%d} {WEEKDAYS[local.weekday()][:3]} {local:%H:%M} ({tz.key})"


def now_details(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz)
    today = local.date()
    offset = local.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    utc_offset = f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    week_start, week_end = _keyword_days("this_week", today)
    last_start, _ = _keyword_days("last_week", today)
    this_month, _ = _keyword_days("this_month", today)
    last_month, _ = _keyword_days("last_month", today)
    last_end = week_start - timedelta(days=1)
    return "\n".join(
        (
            f"Today: {WEEKDAYS[today.weekday()]} {today.isoformat()} · ISO week {iso_week(today)} · {utc_offset}",
            f"This week: {week_start} Mon → {week_end - timedelta(days=1)} Sun"
            f" · last week: {last_start} → {last_end}",
            f"This month: {this_month:%Y-%m} ({MONTHS[this_month.month - 1]})"
            f" · last month: {last_month:%Y-%m} ({MONTHS[last_month.month - 1]})",
            f"This year: {today.year} · last year: {today.year - 1} · next year: {today.year + 1}",
            f"Period keywords: {', '.join(KEYWORDS)}",
        )
    )
