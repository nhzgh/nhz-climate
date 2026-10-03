"""Small, explicit room-air projection used by the ventilation advisory.

The projection is presentation data, not an additional recommendation rule.
It deliberately uses fixed first-release working values so every ventilation
zone is comparable: a 60 m3 room, 100 m3/h outdoor air and an 8-hour thermal
time constant.  The latter gives about 0.4 K of room-air response in one hour
for a 3.4 K outdoor difference, while the separately reported air-only curve
shows the physical limit without thermal storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import exp, isfinite
from typing import Any, Iterable

from .psychrometrics import EPSILON, AirState, air_state, saturation_vapour_pressure_pa


STANDARD_ROOM_VOLUME_M3 = 60.0
STANDARD_AIRFLOW_M3_PER_H = 100.0
THERMAL_TIME_CONSTANT_H = 8.0
TEMPERATURE_TARGET_C = 22.0
RELATIVE_HUMIDITY_TARGET_PERCENT = 50.0
TEMPERATURE_ACCEPTANCE_C = (20.0, 24.0)
RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT = (40.0, 60.0)
HORIZONS_HOURS = (1, 8)


@dataclass(frozen=True)
class OutdoorForecastPoint:
    """One hourly weather point, represented by its interval start."""

    start: datetime
    temperature_c: float
    relative_humidity_percent: float


@dataclass(frozen=True)
class ProjectionHorizon:
    """A single horizon. ``available`` is intentionally per horizon."""

    hours: int
    available: bool
    complete: bool
    temperature_c: float | None = None
    relative_humidity_percent: float | None = None
    temperature_delta_to_target_k: float | None = None
    relative_humidity_delta_to_target_percentage_points: float | None = None
    air_only_temperature_c: float | None = None
    air_only_relative_humidity_percent: float | None = None
    air_only_temperature_delta_k: float | None = None
    air_only_relative_humidity_delta_percentage_points: float | None = None
    actual_duration_minutes: float = 0.0
    forecast_intervals_required: int = 0
    forecast_intervals_covered: int = 0
    source: str | None = None
    quality_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "complete": self.complete,
            "temperature_c": self.temperature_c,
            "relative_humidity_percent": self.relative_humidity_percent,
            "temperature_delta_to_target_k": self.temperature_delta_to_target_k,
            "relative_humidity_delta_to_target_percentage_points": (
                self.relative_humidity_delta_to_target_percentage_points
            ),
            "air_only_temperature_c": self.air_only_temperature_c,
            "air_only_relative_humidity_percent": self.air_only_relative_humidity_percent,
            "air_only_temperature_delta_k": self.air_only_temperature_delta_k,
            "air_only_relative_humidity_delta_to_target_percentage_points": (
                self.air_only_relative_humidity_delta_percentage_points
            ),
            "duration_minutes": self.actual_duration_minutes,
            "forecast_intervals_required": self.forecast_intervals_required,
            "forecast_intervals_covered": self.forecast_intervals_covered,
            "source": self.source,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class VentilationProjection:
    """JSON-safe projection attached to a window-open decision sensor."""

    available: bool
    current_temperature_c: float | None
    current_relative_humidity_percent: float | None
    current_temperature_delta_to_target_k: float | None
    current_relative_humidity_delta_to_target_percentage_points: float | None
    pressure_pa: float | None
    horizons: dict[int, ProjectionHorizon]
    quality_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "model": "standard_room_v1",
            "room_volume_m3": STANDARD_ROOM_VOLUME_M3,
            "airflow_m3_per_h": STANDARD_AIRFLOW_M3_PER_H,
            "thermal_time_constant_h": THERMAL_TIME_CONSTANT_H,
            "pressure_pa": self.pressure_pa,
            "targets": {
                "temperature_c": TEMPERATURE_TARGET_C,
                "relative_humidity_percent": RELATIVE_HUMIDITY_TARGET_PERCENT,
                "temperature_acceptance_c": list(TEMPERATURE_ACCEPTANCE_C),
                "relative_humidity_acceptance_percent": list(
                    RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT
                ),
            },
            "current": {
                "temperature_c": self.current_temperature_c,
                "relative_humidity_percent": self.current_relative_humidity_percent,
                "temperature_delta_to_target_k": self.current_temperature_delta_to_target_k,
                "relative_humidity_delta_to_target_percentage_points": (
                    self.current_relative_humidity_delta_to_target_percentage_points
                ),
            },
            "horizons": {f"{hours}h": result.as_dict() for hours, result in self.horizons.items()},
            "quality_flags": list(self.quality_flags),
        }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _relative_humidity(
    temperature_c: float,
    humidity_ratio: float,
    pressure_pa: float,
) -> float | None:
    saturation_pressure = saturation_vapour_pressure_pa(temperature_c)
    if saturation_pressure is None or humidity_ratio < 0 or pressure_pa <= 0:
        return None
    vapour_pressure = humidity_ratio * pressure_pa / (EPSILON + humidity_ratio)
    relative_humidity = 100.0 * vapour_pressure / saturation_pressure
    if not isfinite(relative_humidity):
        return None
    # Tiny numerical overshoots are not a climate statement and should not
    # make the otherwise valid display attribute unusable.
    return max(0.0, min(100.0, relative_humidity))


def parse_hourly_forecast(points: Iterable[dict[str, Any]]) -> tuple[OutdoorForecastPoint, ...]:
    """Read HA's hourly forecast shape, discarding incomplete/invalid points."""
    result: dict[datetime, OutdoorForecastPoint] = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        raw_time = point.get("datetime")
        if not isinstance(raw_time, str):
            continue
        try:
            start = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            start = _utc(start)
        except ValueError:
            continue
        temperature = _finite(point.get("temperature"))
        humidity = _finite(point.get("humidity"))
        if (
            temperature is None
            or humidity is None
            or not -40.0 <= temperature <= 60.0
            or not 0.0 <= humidity <= 100.0
        ):
            continue
        result[start] = OutdoorForecastPoint(start, temperature, humidity)
    return tuple(result[key] for key in sorted(result))


def current_refresh_hourly_forecast(
    coordinator_data: object,
) -> tuple[dict[str, Any], ...]:
    """Return only the weather result from the current coordinator refresh.

    ``hourly_forecast`` intentionally retains earlier points for complete
    same-day climate curves.  It is not safe for a forward-looking ventilation
    calculation: a failed weather request could otherwise leave stale future
    points looking like a complete overnight forecast.  Callers must therefore
    use this explicit, unmerged companion payload.
    """
    if not isinstance(coordinator_data, dict):
        return ()
    points = coordinator_data.get("hourly_forecast_current_refresh")
    if not isinstance(points, list):
        return ()
    return tuple(point for point in points if isinstance(point, dict))


def _next_hour(now: datetime) -> datetime:
    """Return the next hour boundary; an exact hour owns its full first hour."""
    truncated = now.replace(minute=0, second=0, microsecond=0)
    return truncated + timedelta(hours=1)


def _step(
    temperature_c: float,
    humidity_ratio: float,
    outdoor: AirState,
    pressure_pa: float,
    duration_h: float,
    *,
    thermal_time_constant_h: float,
) -> tuple[float, float, float | None]:
    """Advance thermal and ideal humidity mixing over one constant interval."""
    thermal_factor = exp(-duration_h / thermal_time_constant_h)
    moisture_factor = exp(
        -STANDARD_AIRFLOW_M3_PER_H / STANDARD_ROOM_VOLUME_M3 * duration_h
    )
    projected_temperature = outdoor.temperature_c + (
        temperature_c - outdoor.temperature_c
    ) * thermal_factor
    projected_ratio = outdoor.humidity_ratio_kg_per_kg + (
        humidity_ratio - outdoor.humidity_ratio_kg_per_kg
    ) * moisture_factor
    return projected_temperature, projected_ratio, _relative_humidity(
        projected_temperature, projected_ratio, pressure_pa
    )


def _air_only_step(
    temperature_c: float,
    humidity_ratio: float,
    outdoor: AirState,
    pressure_pa: float,
    duration_h: float,
) -> tuple[float, float, float | None]:
    """Advance the no-building-mass air-only limit for the same intervals."""
    factor = exp(-STANDARD_AIRFLOW_M3_PER_H / STANDARD_ROOM_VOLUME_M3 * duration_h)
    projected_temperature = outdoor.temperature_c + (temperature_c - outdoor.temperature_c) * factor
    projected_ratio = outdoor.humidity_ratio_kg_per_kg + (
        humidity_ratio - outdoor.humidity_ratio_kg_per_kg
    ) * factor
    return projected_temperature, projected_ratio, _relative_humidity(
        projected_temperature, projected_ratio, pressure_pa
    )


def _horizon_unavailable(
    hours: int,
    *,
    required: int,
    covered: int,
    flags: tuple[str, ...],
) -> ProjectionHorizon:
    return ProjectionHorizon(
        hours=hours,
        available=False,
        complete=False,
        forecast_intervals_required=required,
        forecast_intervals_covered=covered,
        quality_flags=flags,
    )


def project_standard_room(
    *,
    now: datetime,
    indoor: AirState | None,
    outdoor: AirState | None,
    pressure_pa: float | None,
    hourly_forecast: Iterable[dict[str, Any]],
) -> VentilationProjection:
    """Project 1 h / 8 h air state without affecting the advisory decision.

    The current local outdoor observation is used up to the next hour
    boundary.  Forecast points then apply by their hourly interval start.  A
    one-hour calculation may fall back to the current outdoor observation if
    the forecast does not cover its remaining partial hour.  Eight hours must
    have every required future hourly point; it intentionally never repeats a
    current measurement as a synthetic overnight forecast.
    """
    timestamp = _utc(now)
    valid_pressure = _finite(pressure_pa)
    if indoor is None or outdoor is None or valid_pressure is None or valid_pressure <= 0:
        unavailable = {
            hours: _horizon_unavailable(
                hours, required=0, covered=0, flags=("projection_inputs_unavailable",)
            )
            for hours in HORIZONS_HOURS
        }
        return VentilationProjection(
            available=False,
            current_temperature_c=None,
            current_relative_humidity_percent=None,
            current_temperature_delta_to_target_k=None,
            current_relative_humidity_delta_to_target_percentage_points=None,
            pressure_pa=valid_pressure,
            horizons=unavailable,
            quality_flags=("projection_inputs_unavailable",),
        )

    forecast_by_start = {point.start: point for point in parse_hourly_forecast(hourly_forecast)}
    boundary = _next_hour(timestamp)
    first_duration_h = min(1.0, (boundary - timestamp).total_seconds() / 3600.0)
    # Exact hour and partial hour are both deliberately served by the actual
    # outdoor reading before any forecast is used.
    assert first_duration_h > 0

    results: dict[int, ProjectionHorizon] = {}
    combined_flags: set[str] = set()
    for horizon_hours in HORIZONS_HOURS:
        remaining_h = float(horizon_hours)
        temperature = indoor.temperature_c
        ratio = indoor.humidity_ratio_kg_per_kg
        air_temperature = indoor.temperature_c
        air_ratio = indoor.humidity_ratio_kg_per_kg
        duration_h = min(remaining_h, first_duration_h)
        temperature, ratio, relative_humidity = _step(
            temperature, ratio, outdoor, valid_pressure, duration_h,
            thermal_time_constant_h=THERMAL_TIME_CONSTANT_H,
        )
        air_temperature, air_ratio, air_relative_humidity = _air_only_step(
            air_temperature, air_ratio, outdoor, valid_pressure, duration_h
        )
        remaining_h -= duration_h
        forecast_required = 0
        forecast_covered = 0
        flags: list[str] = []
        source = "current_outdoor_actual"
        interval_start = boundary

        while remaining_h > 1e-9:
            interval_duration_h = min(1.0, remaining_h)
            point = forecast_by_start.get(interval_start)
            forecast_required += 1
            if point is None:
                # The one-hour card remains useful when the very next
                # forecast point is temporarily absent.  It is explicitly
                # labelled as an actual-current fallback, never as forecast.
                if horizon_hours == 1:
                    temperature, ratio, relative_humidity = _step(
                        temperature, ratio, outdoor, valid_pressure, interval_duration_h,
                        thermal_time_constant_h=THERMAL_TIME_CONSTANT_H,
                    )
                    air_temperature, air_ratio, air_relative_humidity = _air_only_step(
                        air_temperature, air_ratio, outdoor, valid_pressure, interval_duration_h
                    )
                    source = "current_outdoor_actual_fallback"
                    flags.append("forecast_missing_current_fallback")
                    remaining_h -= interval_duration_h
                    # Let the completed loop construct the normal one-hour
                    # result below.  This is a labelled fallback, not an
                    # unavailable horizon.
                    continue
                flags.append("forecast_incomplete")
                results[horizon_hours] = _horizon_unavailable(
                    horizon_hours,
                    required=forecast_required,
                    covered=forecast_covered,
                    flags=tuple(flags),
                )
                combined_flags.update(flags)
                break
            forecast_covered += 1
            forecast_air = air_state(
                point.temperature_c, point.relative_humidity_percent, valid_pressure
            )
            if forecast_air is None:
                flags.append("forecast_invalid")
                results[horizon_hours] = _horizon_unavailable(
                    horizon_hours,
                    required=forecast_required,
                    covered=forecast_covered - 1,
                    flags=tuple(flags),
                )
                combined_flags.update(flags)
                break
            temperature, ratio, relative_humidity = _step(
                temperature, ratio, forecast_air, valid_pressure, interval_duration_h,
                thermal_time_constant_h=THERMAL_TIME_CONSTANT_H,
            )
            air_temperature, air_ratio, air_relative_humidity = _air_only_step(
                air_temperature, air_ratio, forecast_air, valid_pressure, interval_duration_h
            )
            remaining_h -= interval_duration_h
            interval_start += timedelta(hours=1)
            source = "current_outdoor_actual_plus_hourly_forecast"
        else:
            if relative_humidity is None or air_relative_humidity is None:
                flags.append("projection_psychrometrics_invalid")
                results[horizon_hours] = _horizon_unavailable(
                    horizon_hours,
                    required=forecast_required,
                    covered=forecast_covered,
                    flags=tuple(flags),
                )
                combined_flags.update(flags)
                continue
            results[horizon_hours] = ProjectionHorizon(
                hours=horizon_hours,
                available=True,
                complete=True,
                temperature_c=temperature,
                relative_humidity_percent=relative_humidity,
                temperature_delta_to_target_k=temperature - TEMPERATURE_TARGET_C,
                relative_humidity_delta_to_target_percentage_points=(
                    relative_humidity - RELATIVE_HUMIDITY_TARGET_PERCENT
                ),
                air_only_temperature_c=air_temperature,
                air_only_relative_humidity_percent=air_relative_humidity,
                air_only_temperature_delta_k=air_temperature - TEMPERATURE_TARGET_C,
                air_only_relative_humidity_delta_percentage_points=(
                    air_relative_humidity - RELATIVE_HUMIDITY_TARGET_PERCENT
                ),
                actual_duration_minutes=horizon_hours * 60.0,
                forecast_intervals_required=forecast_required,
                forecast_intervals_covered=forecast_covered,
                source=source,
                quality_flags=tuple(flags),
            )
            combined_flags.update(flags)

    return VentilationProjection(
        available=True,
        current_temperature_c=indoor.temperature_c,
        current_relative_humidity_percent=indoor.relative_humidity_percent,
        current_temperature_delta_to_target_k=indoor.temperature_c - TEMPERATURE_TARGET_C,
        current_relative_humidity_delta_to_target_percentage_points=(
            indoor.relative_humidity_percent - RELATIVE_HUMIDITY_TARGET_PERCENT
        ),
        pressure_pa=valid_pressure,
        horizons=results,
        quality_flags=tuple(sorted(combined_flags)),
    )
