"""Real NYSE trading hours (regular session 9:30-16:00 America/New_York, holidays, early closes)."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata missing
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")

# Official NYSE holiday calendar (full-day closures)
HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
# 1:00 pm ET early closes
EARLY_CLOSE = {"2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26"}

OPEN_T = dtime(9, 30)
CLOSE_T = dtime(16, 0)
EARLY_T = dtime(13, 0)


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def _session(d: date) -> Tuple[datetime, datetime]:
    close_t = EARLY_T if d.isoformat() in EARLY_CLOSE else CLOSE_T
    o = datetime.combine(d, OPEN_T)
    c = datetime.combine(d, close_t)
    try:
        o = o.replace(tzinfo=ET)
        c = c.replace(tzinfo=ET)
    except Exception:  # pytz
        o = ET.localize(o)  # type: ignore
        c = ET.localize(c)  # type: ignore
    return o, c


def is_open(ts: float) -> bool:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    if not _is_trading_day(dt.date()):
        return False
    o, c = _session(dt.date())
    return o <= dt < c


def next_open(ts: float) -> float:
    """Timestamp of the next regular-session open strictly after/at ts (or now if open)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    d = dt.date()
    for i in range(0, 15):
        day = d + timedelta(days=i)
        if not _is_trading_day(day):
            continue
        o, c = _session(day)
        if dt < o:
            return o.timestamp()
        if o <= dt < c:
            return ts
    return ts + 86400


def next_close(ts: float) -> Optional[float]:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    if not is_open(ts):
        return None
    return _session(dt.date())[1].timestamp()


def status(ts: float) -> dict:
    op = is_open(ts)
    if op:
        nc = next_close(ts)
        return {"open": True, "label": "Market open", "next_close": nc, "next_open": None}
    no = next_open(ts)
    when = datetime.fromtimestamp(no, tz=timezone.utc).astimezone(ET)
    return {"open": False, "label": "Market closed", "next_open": no,
            "next_open_text": when.strftime("%a %b %d, %I:%M %p ET").replace(" 0", " "), "next_close": None}
