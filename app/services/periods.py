"""Date periods for the dashboard: IST calendar days, inclusive.

  date_from / date_to   YYYY-MM-DD, India calendar days, both included
  default               the last 30 days including today
  one date only         that single day
  rules                 from <= to, at most 366 days, nothing after today  (else PeriodError -> 400)

A day runs from 00:00 IST, which is 18:30 UTC the day before. Timestamps are stored in UTC;
`ist_date()` gives the India calendar day of one, and `utc_bounds()` gives the period as UTC
times for database filters.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_DAYS = 30
MAX_DAYS = 366
WEEKLY_AFTER_DAYS = 90       # longer periods get one inflow point per 7 days


class PeriodError(ValueError):
    """An invalid period; the message is shown to the user (400)."""


def today_ist() -> date:
    return datetime.now(IST).date()


def ist_date(value: Any) -> date | None:
    """datetime (naive = UTC) -> India calendar date; a date stays a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST).date()
    if isinstance(value, date):
        return value
    return None


@dataclass(frozen=True)
class Period:
    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def contains(self, day: date | None) -> bool:
        return day is not None and self.date_from <= day <= self.date_to

    def previous(self) -> "Period":
        """The same number of days, right before this period."""
        end = self.date_from - timedelta(days=1)
        return Period(end - timedelta(days=self.days - 1), end)

    def utc_bounds(self) -> tuple[datetime, datetime]:
        """[start, end) in UTC (aware): 00:00 IST on date_from up to 00:00 IST the day after date_to."""
        start = datetime.combine(self.date_from, time(), tzinfo=IST).astimezone(timezone.utc)
        end = datetime.combine(self.date_to + timedelta(days=1), time(), tzinfo=IST).astimezone(timezone.utc)
        return start, end

    @property
    def granularity(self) -> str:
        return "week" if self.days > WEEKLY_AFTER_DAYS else "day"

    def buckets(self) -> list[tuple[date, date]]:
        """Inflow points: one per day, or 7-day buckets from date_from (the last may be shorter)."""
        step = 7 if self.granularity == "week" else 1
        out = []
        start = self.date_from
        while start <= self.date_to:
            out.append((start, min(start + timedelta(days=step - 1), self.date_to)))
            start += timedelta(days=step)
        return out

    def as_dict(self) -> dict:
        prev = self.previous()
        return {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "days": self.days,
            "previous": {"date_from": prev.date_from.isoformat(), "date_to": prev.date_to.isoformat()},
            "granularity": self.granularity,
        }


def default_period(today: date | None = None) -> Period:
    today = today or today_ist()
    return Period(today - timedelta(days=DEFAULT_DAYS - 1), today)


def _parse_day(text: str, name: str) -> date:
    try:
        if len(text) != 10:
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise PeriodError(f"{name} must be a date like 2026-11-16 (YYYY-MM-DD)")


def parse_period(date_from: str | None, date_to: str | None, *, today: date | None = None,
                 required: bool = True) -> Period | None:
    """The period from query parameters. required=False: no dates -> None (no filter)."""
    date_from = (date_from or "").strip() or None
    date_to = (date_to or "").strip() or None
    today = today or today_ist()
    if date_from is None and date_to is None:
        return default_period(today) if required else None
    start = _parse_day(date_from, "date_from") if date_from else None
    end = _parse_day(date_to, "date_to") if date_to else None
    start, end = start or end, end or start                    # one date = that single day
    if start > end:
        raise PeriodError("date_from must be on or before date_to")
    if end > today:
        raise PeriodError(f"Dates can't be in the future (today is {today.isoformat()} in India)")
    period = Period(start, end)
    if period.days > MAX_DAYS:
        raise PeriodError(f"The period can be at most {MAX_DAYS} days (this one has {period.days})")
    return period
