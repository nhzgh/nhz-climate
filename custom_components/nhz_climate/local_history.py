"""Validation and normalization for locally recorded hourly observations.

The recorder is the authority for a configured local source.  This module is
deliberately independent from Home Assistant imports so its interval rules can
be tested without a running HA instance.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo


RAIN_VARIABLE = "rain"
TEMPERATURE_VARIABLE = "temperature_2m"


def last_completed_hour(now: datetime, site_timezone: str) -> datetime:
    """Return the UTC end of the most recently fully elapsed local hour."""
    local_now = now.astimezone(ZoneInfo(site_timezone))
    return local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Recorder's Python statistics rows use Unix seconds, while some
        # serialized HA API surfaces expose milliseconds. Accept both without
        # mistaking a numeric start for a missing local hour.
        number = _number(value)
        if number is None:
            return None
        try:
            parsed = datetime.fromtimestamp(
                number / 1000 if abs(number) >= 100_000_000_000 else number,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def normalize_hourly_statistics(
    statistics: Iterable[dict[str, Any]],
    start_utc: datetime,
    end_utc: datetime,
    variable: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return complete hourly values only, with explicit validation reasons.

    ``start`` is the beginning of the recorder's hourly bucket.  A bucket is
    accepted only when it has a finite value and lies wholly in the requested
    historic range.  Thus the active and future hour can never enter an IST
    curve, even when the recorder already exposed a provisional statistic.
    """
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("UTC-aware range required")
    start_utc = start_utc.astimezone(timezone.utc)
    end_utc = end_utc.astimezone(timezone.utc)
    if end_utc <= start_utc or start_utc.minute or start_utc.second or end_utc.minute or end_utc.second:
        raise ValueError("range must consist of complete UTC hours")

    # Recorder exposes the reset-aware increment of a ``total_increasing``
    # entity as ``change``.  ``sum`` is a running cumulative statistic and is
    # not an hourly rainfall amount.
    key = "mean" if variable == TEMPERATURE_VARIABLE else "change"
    by_start: dict[datetime, float] = {}
    invalid: list[str] = []
    for item in statistics:
        bucket_start = _as_utc(item.get("start"))
        value = _number(item.get(key))
        if bucket_start is None or value is None:
            invalid.append("invalid_hourly_statistic")
            continue
        if bucket_start < start_utc or bucket_start + timedelta(hours=1) > end_utc:
            continue
        if variable == RAIN_VARIABLE and value < 0:
            invalid.append("negative_rain")
            continue
        # A duplicate hour makes the recorder coverage ambiguous.  Do not
        # silently choose one of two potentially revised values.
        if bucket_start in by_start:
            invalid.append("duplicate_hour")
            continue
        by_start[bucket_start] = value

    points: list[dict[str, Any]] = []
    expected = start_utc
    while expected < end_utc:
        value = by_start.get(expected)
        if value is None:
            invalid.append("missing_hour")
        else:
            points.append({"start": expected.isoformat(), "value": value})
        expected += timedelta(hours=1)
    return points, sorted(set(invalid))


def local_segment(
    statistics: Iterable[dict[str, Any]],
    start_utc: datetime,
    end_utc: datetime,
    variable: str,
    source_entity: str,
) -> dict[str, Any]:
    """Build a safe local segment, or mark it unusable without inventing zero.

    Rain uses recorder's reset-aware hourly ``change`` statistic.  A zero is valid
    precipitation; missing buckets are not zero and invalidate that local
    range, so the caller can retain the modelled fallback.
    """
    points, issues = normalize_hourly_statistics(
        statistics, start_utc, end_utc, variable
    )
    expected_hours = int((end_utc - start_utc).total_seconds() // 3600)
    complete = len(points) == expected_hours and not issues
    values = [point["value"] for point in points]
    return {
        "source_class": "local_observation",
        "source_entity": source_entity,
        "variable": variable,
        "range_start_utc": start_utc.astimezone(timezone.utc).isoformat(),
        "range_end_utc": end_utc.astimezone(timezone.utc).isoformat(),
        "through_utc": end_utc.astimezone(timezone.utc).isoformat(),
        "value": (
            sum(values) if variable == RAIN_VARIABLE else sum(values) / len(values)
        ) if complete else None,
        # Coordinator-only data for per-hour merging. Callers strip it before
        # publishing attributes, so even a 365-day comparison stays compact.
        "hourly": points,
        "coverage_ratio": len(points) / expected_hours if expected_hours else 0.0,
        "complete": complete,
        "quality_flags": issues,
        "reset_handling": (
            "home_assistant_lts_change" if variable == RAIN_VARIABLE else None
        ),
    }


def normalize_daily_references(
    records: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Normalize API daily references without treating absent values as zero."""
    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        start = record.get("local_start")
        end = record.get("local_end")
        metric = record.get("reference")
        if not isinstance(start, str) or not isinstance(end, str) or not isinstance(metric, dict):
            continue
        normalized[(start, end)] = {
            "mean": metric.get("mean_mm"),
            "p10": metric.get("p10_mm"),
            "p50": metric.get("p50_mm"),
            "p90": metric.get("p90_mm"),
            "sample_count": metric.get("sample_count"),
            "coverage_ratio": metric.get("coverage_ratio"),
            "method": "api_daily_reference_exact",
            "partial": bool(record.get("partial")),
        }
    return normalized


def calendar_day_temperature_anomaly(
    *,
    now: datetime,
    site_timezone: str,
    hourly_normal: Iterable[dict[str, Any]],
    local_actual: dict[str, Any] | None,
    hourly_forecast: Iterable[dict[str, Any]],
    current_observation: float | None = None,
) -> dict[str, Any]:
    """Calculate today's estimated mean-temperature anomaly in kelvin.

    Completed local hours use Recorder/LTS observations; future hours use the
    configured weather forecast.  Every hour is compared to the same local
    clock-hour's climatological mean.  A missing normal, IST or forecast hour
    makes the result unavailable instead of changing a gap into a 0 K delta.
    """
    if now.tzinfo is None:
        raise ValueError("UTC-aware now required")
    zone = ZoneInfo(site_timezone)
    now_utc = now.astimezone(timezone.utc)
    local_today = now_utc.astimezone(zone).date()
    local_start = datetime.combine(local_today, datetime.min.time(), tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    completed_end = min(last_completed_hour(now_utc, site_timezone), local_end.astimezone(timezone.utc))
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    normal_by_hour: dict[int, float] = {}
    for point in hourly_normal:
        if not isinstance(point, dict):
            continue
        hour = point.get("local_hour")
        value = _number(point.get("mean"))
        if isinstance(hour, int) and 0 <= hour <= 23 and value is not None:
            normal_by_hour[hour] = value

    actual_by_start: dict[datetime, float] = {}
    if isinstance(local_actual, dict):
        for point in local_actual.get("hourly", []):
            if not isinstance(point, dict):
                continue
            start = _as_utc(point.get("start"))
            value = _number(point.get("value"))
            if start is not None and value is not None:
                actual_by_start[start] = value

    forecast_by_start: dict[datetime, float] = {}
    for point in hourly_forecast:
        if not isinstance(point, dict):
            continue
        start = _as_utc(point.get("datetime"))
        value = _number(point.get("temperature"))
        if start is not None and value is not None:
            forecast_by_start[start] = value

    expected_hours = int((end_utc - start_utc).total_seconds() // 3600)
    actual_hours = 0
    forecast_hours = 0
    covered_hours = 0
    normal_sum = 0.0
    temperature_sum = 0.0
    missing: list[str] = []
    notes: list[str] = []
    current_proxy_hours = 0
    cursor = start_utc
    while cursor < end_utc:
        normal = normal_by_hour.get(cursor.astimezone(zone).hour)
        if normal is None:
            missing.append("missing_hourly_normal")
            cursor += timedelta(hours=1)
            continue
        if cursor < completed_end:
            temperature = actual_by_start.get(cursor)
            source = "local_lts"
        else:
            temperature = forecast_by_start.get(cursor)
            source = "forecast"
            # HA weather forecasts commonly start with the *next* full hour.
            # Bridge only the currently active hour with the explicitly
            # selected local outdoor sensor; never extend that proxy into any
            # later missing forecast hours.
            if (
                temperature is None
                and cursor == completed_end
                and current_observation is not None
            ):
                temperature = current_observation
                source = "current_observation_proxy"
                notes.append("current_hour_instantaneous_proxy")
        if temperature is None:
            missing.append(f"missing_{source}_hour")
            cursor += timedelta(hours=1)
            continue
        covered_hours += 1
        normal_sum += normal
        temperature_sum += temperature
        if source == "local_lts":
            actual_hours += 1
        elif source == "current_observation_proxy":
            current_proxy_hours += 1
        else:
            forecast_hours += 1
        cursor += timedelta(hours=1)

    complete = covered_hours == expected_hours and not missing
    return {
        "value": (temperature_sum - normal_sum) / expected_hours if complete else None,
        "estimated_day_mean": temperature_sum / expected_hours if complete else None,
        "normal_day_mean": normal_sum / expected_hours if complete else None,
        "expected_hours": expected_hours,
        "covered_hours": covered_hours,
        "actual_hours": actual_hours,
        "forecast_hours": forecast_hours,
        "current_proxy_hours": current_proxy_hours,
        "complete": complete,
        "quality_flags": sorted(set((*missing, *notes))),
        "range_start_utc": start_utc.isoformat(),
        "range_end_utc": end_utc.isoformat(),
        "actual_through_utc": completed_end.isoformat(),
        "semantics": "calendar_day_mean_ist_until_completed_hour_plus_forecast_remainder_minus_hourly_climatology_mean",
    }
