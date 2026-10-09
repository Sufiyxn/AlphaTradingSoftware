"""Standard-library US equity session calendar and current session state.

Phase 11 checkpoint 1 extends this module from *awareness* to *enforcement*:
``market_session`` still answers what time it is at the exchange, and the new
``session_gate`` answers the only question the execution pipeline may ask -
"may an order be submitted right now, for this asset class, with this
provider?" - as a machine-readable decision instead of a boolean.

Everything here is computed from the calendar (weekdays, observed market
holidays, daylight-saving rules, early closes). No network, no vendor call,
no fabricated state: when the exchange is closed the answer is closed.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo


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


# --------------------------------------------------------------------------
# Phase 11 checkpoint 1 - trading windows and the submission gate
# --------------------------------------------------------------------------


class AssetClass:
    """Asset classes the platform can currently route an order for.

    The shipped universe is US equities, so that is the only class with a
    trading calendar today. An unknown class is a *fail-closed* answer at the
    gate, never an assumed window.
    """

    US_EQUITY = "US_EQUITY"


SUPPORTED_ASSET_CLASSES: frozenset[str] = frozenset({AssetClass.US_EQUITY})

#: Machine-readable gate outcomes. ``SESSION_OK`` is the only value that
#: allows a submission; every other code refuses it with its own reason.
SESSION_OK = "SESSION_OK"
MARKET_CLOSED = "MARKET_CLOSED"
OUTSIDE_ALLOWED_WINDOW = "OUTSIDE_ALLOWED_WINDOW"
EXTENDED_HOURS_UNSUPPORTED = "EXTENDED_HOURS_UNSUPPORTED"
UNKNOWN_ASSET_CLASS = "UNKNOWN_ASSET_CLASS"

#: Submission policies. ``regular`` (the default) accepts orders only during
#: the regular session. ``extended`` additionally accepts them during the
#: extended window below, and only when the connected execution provider
#: reports extended-hours capability. Neither policy ever accepts an order on
#: a non-trading day.
SESSION_POLICIES: tuple[str, ...] = ("regular", "extended")
DEFAULT_SESSION_POLICY = "regular"

#: Platform submission policy for extended hours, in exchange local time.
#: This is this platform's own configured window - not a claim about any
#: venue's schedule. A provider that does not support extended hours is
#: refused regardless of this window.
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)

SESSION_LABELS = {
    "PRE_MARKET": "pre-market",
    "REGULAR": "regular session",
    "AFTER_HOURS": "after-hours",
    "CLOSED": "closed",
}


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """The answer to "may an order be submitted at this moment?"."""

    allowed: bool
    code: str
    state: str
    is_trading_day: bool
    trading_date: date
    at: datetime
    policy: str
    asset_class: str
    reason: str

    @property
    def exchange_local_time(self) -> time:
        return self.at.timetz().replace(tzinfo=None)


def normalize_session_policy(value: str | None) -> str:
    """Validate a configured submission policy; anything else fails startup."""
    policy = (value or "").strip().lower()
    if not policy:
        return DEFAULT_SESSION_POLICY
    if policy not in SESSION_POLICIES:
        raise ValueError(
            f"MARKET_SESSION_SUBMISSION must be one of {SESSION_POLICIES}, "
            f"received {value!r}")
    return policy


def asset_class_for_symbol(symbol: str) -> str:
    """Asset class for a tradable symbol.

    The platform's universe and exchange capability are US equities, so every
    symbol resolves to :class:`AssetClass.US_EQUITY`. New asset classes must
    extend this map *and* add a calendar before they can pass the gate -
    they are never assumed tradeable.
    """
    return AssetClass.US_EQUITY


def session_gate(at: datetime | None = None,
                 *,
                 policy: str = DEFAULT_SESSION_POLICY,
                 provider_extended_hours: bool = True,
                 asset_class: str = AssetClass.US_EQUITY) -> SessionDecision:
    """Decide whether an order may be submitted at ``at``.

    Enforcement order, most fundamental first:

    1. an unknown asset class has no calendar, so it is refused
       (``UNKNOWN_ASSET_CLASS``);
    2. a weekend or market holiday is refused for every policy
       (``MARKET_CLOSED``);
    3. inside the regular session the order is allowed (``SESSION_OK``);
    4. outside it the ``regular`` policy refuses
       (``OUTSIDE_ALLOWED_WINDOW``), while the ``extended`` policy checks the
       provider's declared extended-hours capability
       (``EXTENDED_HOURS_UNSUPPORTED``) and then this platform's extended
       window (``OUTSIDE_ALLOWED_WINDOW``).

    A naive ``at`` is interpreted as UTC so a caller without a clock cannot
    silently inherit the host machine's local zone.
    """
    normalized_policy = normalize_session_policy(policy)
    if asset_class not in SUPPORTED_ASSET_CLASSES:
        return SessionDecision(
            False, UNKNOWN_ASSET_CLASS, "UNKNOWN", False,
            (at or datetime.now(timezone.utc)).astimezone(NEW_YORK).date(),
            _aware(at), normalized_policy, asset_class,
            f"no market calendar is defined for asset class {asset_class!r}; "
            f"orders are refused until one is")
    moment = _aware(at)
    session = market_session(moment)
    local = moment.astimezone(NEW_YORK)
    window = f"{session.regular_open.strftime('%H:%M')}-{session.regular_close.strftime('%H:%M')} {NEW_YORK.tzname(session.regular_open)}"
    label = SESSION_LABELS.get(session.state, session.state)

    if not session.is_trading_day:
        resumes = _next_trading_day(session.trading_date)
        return SessionDecision(
            False, MARKET_CLOSED, "CLOSED", False, session.trading_date, local,
            normalized_policy, asset_class,
            f"US equity market is closed on {session.trading_date.isoformat()}; "
            f"the next regular session is {resumes.isoformat()} 09:30 "
            f"{NEW_YORK.tzname(session.regular_open)}")

    if session.state == "REGULAR":
        return SessionDecision(
            True, SESSION_OK, session.state, True, session.trading_date, local,
            normalized_policy, asset_class,
            f"the {label} is open ({window})")

    # PRE_MARKET or AFTER_HOURS on a trading day.
    if normalized_policy != "extended":
        return SessionDecision(
            False, OUTSIDE_ALLOWED_WINDOW, session.state, True,
            session.trading_date, local, normalized_policy, asset_class,
            f"orders are accepted only during the regular session ({window}); "
            f"it is {local.strftime('%H:%M')} {label} on "
            f"{session.trading_date.isoformat()}")
    if not provider_extended_hours:
        return SessionDecision(
            False, EXTENDED_HOURS_UNSUPPORTED, session.state, True,
            session.trading_date, local, normalized_policy, asset_class,
            f"the connected execution provider does not support extended-hours "
            f"orders; it is {local.strftime('%H:%M')} {label} on "
            f"{session.trading_date.isoformat()}")
    if not (EXTENDED_OPEN <= local.time().replace(tzinfo=None) < EXTENDED_CLOSE):
        return SessionDecision(
            False, OUTSIDE_ALLOWED_WINDOW, session.state, True,
            session.trading_date, local, normalized_policy, asset_class,
            f"outside the extended-hours submission window "
            f"{EXTENDED_OPEN.strftime('%H:%M')}-{EXTENDED_CLOSE.strftime('%H:%M')} "
            f"{NEW_YORK.tzname(session.regular_open)}: it is "
            f"{local.strftime('%H:%M')} {label} on "
            f"{session.trading_date.isoformat()}")
    return SessionDecision(
        True, SESSION_OK, session.state, True, session.trading_date, local,
        normalized_policy, asset_class,
        f"{label} submission is allowed by the {normalized_policy!r} policy and "
        f"the provider supports extended hours")


def _aware(at: datetime | None) -> datetime:
    moment = at if at is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(NEW_YORK)


def _next_trading_day(day: date, limit: int = 10) -> date:
    candidate = day + timedelta(days=1)
    for _ in range(limit):
        if candidate.weekday() < 5 and candidate not in _holidays(candidate.year):
            return candidate
        candidate += timedelta(days=1)
    return candidate
