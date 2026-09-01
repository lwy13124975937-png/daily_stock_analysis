"""Strict exchange-session utilities for evaluation and report anchoring."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class SessionCalendarUnavailable(RuntimeError):
    pass


class SessionCalendar(Protocol):
    def sessions_between(self, start: date, end: date) -> Sequence[date]:
        ...

    def completed_session_at(self, moment: datetime) -> date:
        ...


class ExchangeSessionCalendar:
    """A strict XSHG calendar adapter; dependency failures never fail open."""

    def __init__(self, calendar_name: str = "XSHG") -> None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise SessionCalendarUnavailable(
                "exchange-calendars is required for A-share session evaluation"
            ) from exc
        try:
            self._calendar = xcals.get_calendar(calendar_name)
        except Exception as exc:
            raise SessionCalendarUnavailable(f"cannot load exchange calendar {calendar_name}: {exc}") from exc

    @staticmethod
    def _to_date(value: object) -> date:
        if hasattr(value, "date"):
            return value.date()  # type: ignore[no-any-return]
        return datetime.fromisoformat(str(value)[:10]).date()

    def sessions_between(self, start: date, end: date) -> Sequence[date]:
        if end < start:
            return []
        try:
            values = self._calendar.sessions_in_range(start.isoformat(), end.isoformat())
        except Exception as exc:
            raise SessionCalendarUnavailable(
                f"cannot resolve XSHG sessions {start.isoformat()}..{end.isoformat()}: {exc}"
            ) from exc
        return [self._to_date(value) for value in values]

    def completed_session_at(self, moment: datetime) -> date:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        local = moment.astimezone(SHANGHAI_TZ)
        search_start = date(local.year - 1, 1, 1)
        sessions = list(self.sessions_between(search_start, local.date()))
        if not sessions:
            raise SessionCalendarUnavailable(f"no XSHG session available before {local.isoformat()}")
        candidate = sessions[-1]
        try:
            close = self._calendar.session_close(candidate.isoformat()).to_pydatetime()
        except Exception as exc:
            raise SessionCalendarUnavailable(f"cannot resolve XSHG close for {candidate}: {exc}") from exc
        if local.astimezone(close.tzinfo) < close:
            if len(sessions) < 2:
                raise SessionCalendarUnavailable(f"no completed XSHG session before {local.isoformat()}")
            candidate = sessions[-2]
        return candidate


def sessions_after(calendar: SessionCalendar, anchor: date, through: date) -> list[date]:
    return [session for session in calendar.sessions_between(anchor, through) if session > anchor]


def nth_session_after(calendar: SessionCalendar, anchor: date, offset: int, through: date) -> date | None:
    if offset <= 0:
        raise ValueError("offset must be positive")
    sessions = sessions_after(calendar, anchor, through)
    return sessions[offset - 1] if len(sessions) >= offset else None
