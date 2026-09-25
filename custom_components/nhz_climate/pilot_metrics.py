"""Persistent, LTS-friendly metrics for a ventilation pilot.

The pilot must be evaluable from Home Assistant long-term statistics without
requiring an additional dashboard or a high-cardinality event stream.  This
module therefore stores monotonic duration counters (hours) and a few small
event counters.  It deliberately has no Home Assistant imports so it can be
used by the integration and tested independently.

Durations are attributed to the previously observed status when a subsequent
observation arrives.  A gap larger than ``max_gap`` is treated as an
unobserved interval and contributes no pilot time.  This is important after
restarts, recorder outages, or stale source data: absence of an update must
not be reported as a measured recommendation state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping


STATUS_YES = "ja"
STATUS_AMBIVALENT = "ambivalent"
STATUS_NO = "nein"
STATUS_UNAVAILABLE = "unavailable"
VALID_STATUSES = frozenset(
    (STATUS_YES, STATUS_AMBIVALENT, STATUS_NO, STATUS_UNAVAILABLE)
)

REASON_RAIN = "rain"
REASON_GUST = "gust"


def _utc(value: datetime) -> datetime:
    """Return an aware UTC timestamp and reject ambiguous naive values."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _non_negative(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(number) or number < 0:
        return default
    return number


def _event_reason(reasons: tuple[str, ...], key: str) -> bool:
    """Accept both canonical reasons and advisory reason-code variants."""

    return any(key == reason or key in reason for reason in reasons)


@dataclass
class PilotTotals:
    """Monotonic counters suitable for HA ``total_increasing`` sensors."""

    yes_hours: float = 0.0
    ambivalent_hours: float = 0.0
    no_hours: float = 0.0
    unavailable_hours: float = 0.0
    rain_lock_hours: float = 0.0
    gust_lock_hours: float = 0.0
    status_transitions: int = 0
    rain_events: int = 0
    gust_events: int = 0

    def as_dict(self) -> dict[str, float | int]:
        """Return JSON-serializable field names and values."""

        return asdict(self)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "PilotTotals":
        """Restore counters defensively, never allowing a negative value."""

        values = values if isinstance(values, Mapping) else {}
        integer_fields = {"status_transitions", "rain_events", "gust_events"}
        data: dict[str, float | int] = {}
        for field_name in cls.__dataclass_fields__:
            value = _non_negative(values.get(field_name), 0.0)
            data[field_name] = int(value) if field_name in integer_fields else value
        return cls(**data)


class PilotMetrics:
    """Accumulate observed advisory time while explicitly ignoring gaps."""

    def __init__(self, *, max_gap: timedelta = timedelta(minutes=10)) -> None:
        if max_gap <= timedelta(0):
            raise ValueError("max_gap must be positive")
        self.max_gap = max_gap
        self.totals = PilotTotals()
        self._last_at: datetime | None = None
        self._last_status: str | None = None
        self._last_reasons: tuple[str, ...] = ()

    @property
    def last_at(self) -> datetime | None:
        return self._last_at

    @property
    def last_status(self) -> str | None:
        return self._last_status

    @property
    def last_reasons(self) -> tuple[str, ...]:
        return self._last_reasons

    @staticmethod
    def _status(status: object) -> str:
        # Unknown/invalid states are conservatively measured as technical
        # unavailability instead of raising in a live HA update callback.
        return status if isinstance(status, str) and status in VALID_STATUSES else STATUS_UNAVAILABLE

    @staticmethod
    def _reasons(reasons: object) -> tuple[str, ...]:
        if isinstance(reasons, str):
            return (reasons,)
        if reasons is None:
            return ()
        try:
            return tuple(str(reason) for reason in reasons if reason)
        except TypeError:
            return ()

    def _add_duration(self, status: str, reasons: tuple[str, ...], seconds: float) -> None:
        hours = seconds / 3600.0
        if status == STATUS_YES:
            self.totals.yes_hours += hours
        elif status == STATUS_AMBIVALENT:
            self.totals.ambivalent_hours += hours
        elif status == STATUS_NO:
            self.totals.no_hours += hours
        else:
            self.totals.unavailable_hours += hours
        if _event_reason(reasons, REASON_RAIN):
            self.totals.rain_lock_hours += hours
        if _event_reason(reasons, REASON_GUST):
            self.totals.gust_lock_hours += hours

    def update(
        self,
        at: datetime,
        status: object,
        reasons: object = (),
    ) -> PilotTotals:
        """Record one observation and return the current monotonic totals.

        Duplicate timestamps and timestamps older than the latest accepted
        observation update neither durations nor event counters.  A future
        timestamp after ``max_gap`` establishes a new baseline, so the outage
        interval is intentionally excluded.
        """

        observed_at = _utc(at)
        normalized_status = self._status(status)
        normalized_reasons = self._reasons(reasons)

        if self._last_at is not None:
            elapsed = observed_at - self._last_at
            continuous = timedelta(0) < elapsed <= self.max_gap
            if continuous and self._last_status is not None:
                self._add_duration(
                    self._last_status,
                    self._last_reasons,
                    elapsed.total_seconds(),
                )
            elif elapsed <= timedelta(0):
                # Stale/duplicate callbacks must not move the baseline back.
                return self.totals

            # A status transition or newly asserted safety lock is only an
            # observed event when both samples are in one continuous window.
            # A post-outage baseline must not manufacture a transition.
            if continuous and normalized_status != self._last_status:
                self.totals.status_transitions += 1
            if continuous and _event_reason(normalized_reasons, REASON_RAIN) and not _event_reason(
                self._last_reasons, REASON_RAIN
            ):
                self.totals.rain_events += 1
            if continuous and _event_reason(normalized_reasons, REASON_GUST) and not _event_reason(
                self._last_reasons, REASON_GUST
            ):
                self.totals.gust_events += 1

        self._last_at = observed_at
        self._last_status = normalized_status
        self._last_reasons = normalized_reasons
        return self.totals

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable state for config-entry persistence."""

        return {
            "version": 1,
            "max_gap_seconds": self.max_gap.total_seconds(),
            "totals": self.totals.as_dict(),
            "last_at": self._last_at.isoformat() if self._last_at else None,
            "last_status": self._last_status,
            "last_reasons": list(self._last_reasons),
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any] | None,
        *,
        max_gap: timedelta | None = None,
    ) -> "PilotMetrics":
        """Restore a snapshot, tolerating missing/corrupt optional fields."""

        snapshot = snapshot or {}
        configured_gap = max_gap
        if configured_gap is None:
            configured_gap = timedelta(
                seconds=_non_negative(snapshot.get("max_gap_seconds"), 600.0)
            )
        if configured_gap <= timedelta(0):
            configured_gap = timedelta(minutes=10)
        result = cls(max_gap=configured_gap)
        result.totals = PilotTotals.from_mapping(snapshot.get("totals"))
        last_at = snapshot.get("last_at")
        if isinstance(last_at, str):
            try:
                result._last_at = _utc(datetime.fromisoformat(last_at))
            except (TypeError, ValueError):
                result._last_at = None
        status = snapshot.get("last_status")
        result._last_status = result._status(status) if result._last_at else None
        result._last_reasons = result._reasons(snapshot.get("last_reasons")) if result._last_at else ()
        return result
