"""Standard-library US equity session calendar and current session state."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo


class EasternTime(tzinfo):
    """America/New_York offset rules for modern US equity-market dates."""

    def utcoffset(self, dt):
        return timedelta(hours=-5) + self.dst(dt)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        local = dt.replace(tzinfo=None)
        start = datetime.combine(_nth_weekday(local.year, 3, 6, 2), time(2))
        end = datetime.combine(_nth_weekday(local.year, 11, 6, 1), time(2))
        return timedelta(hours=1) if start <= local < end else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt):
        utc = dt.replace(tzinfo=None)
        start_local = datetime.combine(_nth_weekday(utc.year, 3, 6, 2), time(2))
        end_local = datetime.combine(_nth_weekday(utc.year, 11, 6, 1), time(2))
        daylight = start_local + timedelta(hours=5) <= utc < end_local + timedelta(hours=4)
        offset = timedelta(hours=-4 if daylight else -5)
        return (utc + offset).replace(tzinfo=self)


NEW_YORK = EasternTime()


@dataclass(frozen=True, slots=True)
class USMarketSession:
    trading_date: date
    state: str
    is_trading_day: bool
    regular_open: datetime
    regular_close: datetime


def market_session(at: datetime | None = None) -> USMarketSession:
    now = (at or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    day = now.date()
    open_at = datetime.combine(day, time(9, 30), NEW_YORK)
    close_at = datetime.combine(day, time(13 if _early_close(day) else 16), NEW_YORK)
    trading_day = day.weekday() < 5 and day not in _holidays(day.year)
    if not trading_day:
        state = "CLOSED"
    elif now < open_at:
        state = "PRE_MARKET"
    elif now < close_at:
        state = "REGULAR"
    else:
        state = "AFTER_HOURS"
    return USMarketSession(day, state, trading_day, open_at, close_at)


def _early_close(day: date) -> bool:
    return (day.month == 11 and day.weekday() == 4 and 23 <= day.day <= 29) or (
        day.month == 7 and day.day in (2, 3) and day.weekday() < 5
    ) or (day.month == 12 and day.day == 24 and day.weekday() < 5)


def _holidays(year: int) -> set[date]:
    easter = _easter(year)
    return {
        _observed(date(year, 1, 1)), _observed(date(year + 1, 1, 1)),
        _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2), _last_weekday(year, 5, 0), _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)), _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + (month == 12), month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b = year % 19, year // 100
    c, d, e = year % 100, b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)
