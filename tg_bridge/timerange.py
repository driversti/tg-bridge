"""Period arguments of the read tools → a UTC range. The model never does date arithmetic."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tg_bridge.errors import PeriodError

MAX_DAYS = 3650


@dataclass(frozen=True)
class Period:
    start: datetime | None  # aware UTC, inclusive; None = from the latest backwards
    end: datetime | None  # aware UTC, exclusive; None = now (open)


def parse_period(days: int | None, since: str | None, until: str | None, tz: ZoneInfo, now: datetime) -> Period:
    since = (since or "").strip() or None
    until = (until or "").strip() or None
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
    keyword = raw.casefold()
    day: date | None = None
    if keyword == "today":
        day = today
    elif keyword == "yesterday":
        day = today - timedelta(days=1)
    elif len(raw) == 10:
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
        f'Cannot read `{name}`. Use YYYY-MM-DD, "YYYY-MM-DD HH:MM", "today" or "yesterday".'
    )
