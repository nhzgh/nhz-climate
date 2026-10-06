"""Small, explicit room-air trajectory used by the ventilation advisory.

It deliberately uses fixed first-release working values so every ventilation
zone is comparable: a 60 m3 room, 100 m3/h outdoor air and an 8-hour thermal
time constant. The trajectory is a duration-aware refinement of the advisory
before its stability gate, while the separately reported air-only curve still
shows the physical limit without thermal storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import exp, hypot, isfinite
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
TRAJECTORY_STEP_MINUTES = 15
TRAJECTORY_MAX_MINUTES = 8 * 60
TARGET_TEMPERATURE_SCALE_K = 2.0
TARGET_RELATIVE_HUMIDITY_SCALE_PERCENTAGE_POINTS = 10.0
MEANINGFUL_TARGET_IMPROVEMENT_PERCENT = 5.0
# Only floating point noise may be ignored. Each 15-minute endpoint must
# otherwise keep moving toward the target; a policy-sized tolerance would let
# a genuinely worsening forecast segment pass as an overnight recommendation.
PATH_REVERSAL_EPSILON = 1e-6


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
class ProjectionTrajectoryPoint:
    """One exact 15-minute endpoint of the standard-room projection."""

    minutes: int
    available: bool
    temperature_c: float | None = None
    relative_humidity_percent: float | None = None
    humidity_ratio_kg_per_kg: float | None = None
    target_distance: float | None = None
    source: str | None = None
    quality_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "duration_minutes": self.minutes,
            "temperature_c": self.temperature_c,
            "relative_humidity_percent": self.relative_humidity_percent,
            "humidity_ratio_kg_per_kg": self.humidity_ratio_kg_per_kg,
            "target_distance": self.target_distance,
            "source": self.source,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class DurationRecommendation:
    """Versioned, duration-aware climate recommendation.

    The public action deliberately remains separate from the historical HA
    state vocabulary.  Callers map ``short_airing``, ``ventilate`` and
    ``overnight`` to the compatible ``ja`` state, while ``avoid`` maps to
    ``nein`` and ``optional`` remains ``optional``.
    """

    available: bool
    action: str
    recommended_duration_minutes: int | None
    optimal_duration_minutes: int | None
    limiting_factor: str | None
    target_distance_before: float | None
    target_distance_after: float | None
    target_improvement_percent: float | None
    complete_through_minutes: int
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": "duration_target_v1",
            "available": self.available,
            "recommended_action": self.action,
            "recommended_duration_minutes": self.recommended_duration_minutes,
            "optimal_duration_minutes": self.optimal_duration_minutes,
            "limiting_factor": self.limiting_factor,
            "target_distance_before": self.target_distance_before,
            "target_distance_after": self.target_distance_after,
            "target_improvement_percent": self.target_improvement_percent,
            "complete_through_minutes": self.complete_through_minutes,
            "reason_codes": list(self.reason_codes),
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
    trajectory: dict[int, ProjectionTrajectoryPoint]
    recommendation: DurationRecommendation
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
            # All endpoints are exposed for diagnostics.  The compact card
            # only needs the named summary horizons, keeping its UI stable.
            "trajectory": {
                f"{minutes}m": point.as_dict()
                for minutes, point in self.trajectory.items()
            },
            "trajectory_summary": {
                f"{minutes}m": self.trajectory[minutes].as_dict()
                for minutes in (15, 30, 60, 480)
                if minutes in self.trajectory
            },
            "duration_recommendation": self.recommendation.as_dict(),
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


def target_distance(temperature_c: float, relative_humidity_percent: float) -> float:
    """Return the documented, unitless distance to 22 C / 50 % rF.

    The scales intentionally equal the half-widths of the accepted comfort
    corridor.  This makes temperature and humidity comparable without
    pretending that one kelvin equals one relative-humidity point.
    """
    return hypot(
        (temperature_c - TEMPERATURE_TARGET_C) / TARGET_TEMPERATURE_SCALE_K,
        (relative_humidity_percent - RELATIVE_HUMIDITY_TARGET_PERCENT)
        / TARGET_RELATIVE_HUMIDITY_SCALE_PERCENTAGE_POINTS,
    )


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


def _within_acceptance(temperature_c: float, relative_humidity_percent: float) -> bool:
    return (
        TEMPERATURE_ACCEPTANCE_C[0] <= temperature_c <= TEMPERATURE_ACCEPTANCE_C[1]
        and RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        <= relative_humidity_percent
        <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
    )


def _violates_new_corridor(
    before_temperature_c: float,
    before_relative_humidity_percent: float,
    after_temperature_c: float,
    after_relative_humidity_percent: float,
) -> str | None:
    """Name a newly crossed guardrail, if any."""
    # A room beginning outside a corridor may move through it toward target,
    # but must never overshoot through the opposite bound and later re-enter.
    # This makes the guard a path constraint rather than an endpoint filter.
    if (
        before_temperature_c > TEMPERATURE_ACCEPTANCE_C[1]
        and after_temperature_c < TEMPERATURE_ACCEPTANCE_C[0]
    ) or (
        before_temperature_c < TEMPERATURE_ACCEPTANCE_C[0]
        and after_temperature_c > TEMPERATURE_ACCEPTANCE_C[1]
    ):
        return "temperature_opposite_corridor"
    if (
        before_relative_humidity_percent > RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
        and after_relative_humidity_percent < RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
    ) or (
        before_relative_humidity_percent < RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        and after_relative_humidity_percent > RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
    ):
        return "humidity_opposite_corridor"
    if (
        TEMPERATURE_ACCEPTANCE_C[0] <= before_temperature_c <= TEMPERATURE_ACCEPTANCE_C[1]
        and not TEMPERATURE_ACCEPTANCE_C[0] <= after_temperature_c <= TEMPERATURE_ACCEPTANCE_C[1]
    ):
        return "temperature_corridor"
    if (
        RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        <= before_relative_humidity_percent
        <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
        and not RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        <= after_relative_humidity_percent
        <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
    ):
        return "humidity_corridor"
    return None


def _worsens_existing_violation(
    before_temperature_c: float,
    before_relative_humidity_percent: float,
    after_temperature_c: float,
    after_relative_humidity_percent: float,
) -> str | None:
    """Do not trade a currently failed dimension for another improvement."""
    epsilon = 1e-6
    if before_temperature_c < TEMPERATURE_ACCEPTANCE_C[0] and after_temperature_c < before_temperature_c - epsilon:
        return "temperature_worsens"
    if before_temperature_c > TEMPERATURE_ACCEPTANCE_C[1] and after_temperature_c > before_temperature_c + epsilon:
        return "temperature_worsens"
    if (
        before_relative_humidity_percent < RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        and after_relative_humidity_percent < before_relative_humidity_percent - epsilon
    ):
        return "humidity_worsens"
    if (
        before_relative_humidity_percent > RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
        and after_relative_humidity_percent > before_relative_humidity_percent + epsilon
    ):
        return "humidity_worsens"
    return None


def _trajectory(
    *,
    now: datetime,
    indoor: AirState,
    outdoor: AirState,
    pressure_pa: float,
    forecast_by_start: dict[datetime, OutdoorForecastPoint],
) -> dict[int, ProjectionTrajectoryPoint]:
    """Simulate exact 15-minute endpoints, never repeating missing forecast.

    The live outdoor observation owns the remaining current hour.  Thereafter
    every segment is served by its matching fresh hourly forecast interval.
    This intentionally differs from the display-only 1-hour fallback: an
    absent forecast may still support a very short current-hour suggestion,
    but it may never manufacture a long recommendation.
    """
    boundary = _next_hour(now)
    points: dict[int, ProjectionTrajectoryPoint] = {}
    for minutes in range(TRAJECTORY_STEP_MINUTES, TRAJECTORY_MAX_MINUTES + 1, TRAJECTORY_STEP_MINUTES):
        remaining_h = minutes / 60.0
        cursor = now
        temperature = indoor.temperature_c
        ratio = indoor.humidity_ratio_kg_per_kg
        source_parts: set[str] = set()
        flags: list[str] = []
        while remaining_h > 1e-9:
            if cursor < boundary:
                segment_end = min(boundary, cursor + timedelta(hours=remaining_h))
                current_outdoor = outdoor
                source_parts.add("current_outdoor_actual")
            else:
                interval_start = cursor.replace(minute=0, second=0, microsecond=0)
                point = forecast_by_start.get(interval_start)
                if point is None:
                    flags.append("forecast_incomplete")
                    break
                current_outdoor = air_state(
                    point.temperature_c, point.relative_humidity_percent, pressure_pa
                )
                if current_outdoor is None:
                    flags.append("forecast_invalid")
                    break
                source_parts.add("hourly_forecast")
                segment_end = min(
                    interval_start + timedelta(hours=1),
                    cursor + timedelta(hours=remaining_h),
                )
            duration_h = (segment_end - cursor).total_seconds() / 3600.0
            temperature, ratio, relative_humidity = _step(
                temperature,
                ratio,
                current_outdoor,
                pressure_pa,
                duration_h,
                thermal_time_constant_h=THERMAL_TIME_CONSTANT_H,
            )
            cursor = segment_end
            remaining_h -= duration_h
        if flags or relative_humidity is None:
            points[minutes] = ProjectionTrajectoryPoint(
                minutes=minutes,
                available=False,
                quality_flags=tuple(flags or ("projection_psychrometrics_invalid",)),
            )
            continue
        source = (
            "current_outdoor_actual_plus_hourly_forecast"
            if len(source_parts) == 2
            else next(iter(source_parts))
        )
        points[minutes] = ProjectionTrajectoryPoint(
            minutes=minutes,
            available=True,
            temperature_c=temperature,
            relative_humidity_percent=relative_humidity,
            humidity_ratio_kg_per_kg=ratio,
            target_distance=target_distance(temperature, relative_humidity),
            source=source,
        )
    return points


def _duration_recommendation(
    *,
    indoor: AirState,
    outdoor: AirState,
    trajectory: dict[int, ProjectionTrajectoryPoint],
) -> DurationRecommendation:
    """Choose a duration whose simulated state materially approaches target.

    A candidate is valid only when it improves the normalised target distance
    by at least five percent and does not newly leave a comfort guardrail or
    worsen an already violated dimension.  The absolute-moisture gate keeps
    a relative-humidity artefact caused by cooling from being described as
    dehumidification.
    """
    before_temperature = indoor.temperature_c
    before_humidity = indoor.relative_humidity_percent
    before = target_distance(before_temperature, before_humidity)
    complete_through = max((minutes for minutes, point in trajectory.items() if point.available), default=0)
    if before <= 1e-9:
        return DurationRecommendation(
            True, "optional", None, None, "already_at_target", before, before, 0.0,
            complete_through, ("already_at_target", "climate_neutral_ventilation_optional"),
        )

    # Build one *continuous* safe path.  A later recovery never erases an
    # earlier 15-minute guardrail crossing, and a material turn away from the
    # best reached target distance ends the useful opening duration.
    path: list[ProjectionTrajectoryPoint] = []
    eight_hour_complete = trajectory.get(
        TRAJECTORY_MAX_MINUTES, ProjectionTrajectoryPoint(0, False)
    ).available
    best_distance = before
    limiting: str | None = None
    previous_humidity_ratio = indoor.humidity_ratio_kg_per_kg
    previous_temperature = before_temperature
    previous_relative_humidity = before_humidity
    temperature_recovered = (
        TEMPERATURE_ACCEPTANCE_C[0] <= before_temperature <= TEMPERATURE_ACCEPTANCE_C[1]
    )
    humidity_recovered = (
        RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
        <= before_humidity
        <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
    )
    for minutes in sorted(trajectory):
        point = trajectory[minutes]
        if not point.available:
            limiting = "forecast_incomplete"
            break
        # Without an eight-hour forecast, advice must stay shorter than one
        # hour even when the final current-observation segment happens to end
        # exactly on an hour boundary.
        if not eight_hour_complete and minutes >= 60:
            limiting = "forecast_incomplete"
            break
        assert point.temperature_c is not None and point.relative_humidity_percent is not None
        # Once a formerly failed dimension reaches the corridor, leaving it
        # again is a hard path boundary even when the value is still better
        # than the original reading.  This prevents 80 -> 55 -> 65 % rF from
        # being treated as one continuous dehumidification recommendation.
        if temperature_recovered and not (
            TEMPERATURE_ACCEPTANCE_C[0] <= point.temperature_c <= TEMPERATURE_ACCEPTANCE_C[1]
        ):
            limiting = "temperature_corridor"
            break
        if humidity_recovered and not (
            RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
            <= point.relative_humidity_percent
            <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
        ):
            limiting = "humidity_corridor"
            break
        # The direction of water transport is assessed at every projected
        # segment, rather than from only the current outdoor observation. A
        # later hourly forecast can therefore stop a long recommendation when
        # it starts adding water to an already too-humid room (or removing it
        # from an already too-dry room).
        ratio = point.humidity_ratio_kg_per_kg
        if ratio is None:
            limiting = "absolute_humidity_unavailable"
            break
        if (
            before_humidity > RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
            and ratio > previous_humidity_ratio + 1e-9
        ) or (
            before_humidity < RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
            and ratio < previous_humidity_ratio - 1e-9
        ):
            limiting = "absolute_humidity_worsens"
            break
        guard = _violates_new_corridor(
            before_temperature,
            before_humidity,
            point.temperature_c,
            point.relative_humidity_percent,
        ) or _worsens_existing_violation(
            previous_temperature,
            previous_relative_humidity,
            point.temperature_c,
            point.relative_humidity_percent,
        )
        if guard:
            limiting = guard
            break
        assert point.target_distance is not None
        if point.target_distance > best_distance + PATH_REVERSAL_EPSILON:
            limiting = "target_distance_reverses"
            break
        path.append(point)
        best_distance = min(best_distance, point.target_distance)
        previous_humidity_ratio = ratio
        previous_temperature = point.temperature_c
        previous_relative_humidity = point.relative_humidity_percent
        temperature_recovered = temperature_recovered or (
            TEMPERATURE_ACCEPTANCE_C[0] <= point.temperature_c <= TEMPERATURE_ACCEPTANCE_C[1]
        )
        humidity_recovered = humidity_recovered or (
            RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[0]
            <= point.relative_humidity_percent
            <= RELATIVE_HUMIDITY_ACCEPTANCE_PERCENT[1]
        )

    valid = [
        point
        for point in path
        if point.target_distance is not None
        and point.target_distance
        <= before * (1.0 - MEANINGFUL_TARGET_IMPROVEMENT_PERCENT / 100.0)
    ]

    if not valid:
        in_corridor = _within_acceptance(before_temperature, before_humidity)
        limiting = limiting or (
            "forecast_incomplete" if complete_through == 0 else "target_distance"
        )
        # `optional` means climate-neutral and non-harmful, not merely that
        # the current reading happens to be inside the broad comfort corridor.
        # A path that immediately moves away from target or exits a recovered
        # corridor must be a clear `avoid` with no suggested opening duration.
        harmful_path = limiting == "target_distance_reverses" or limiting.endswith(
            "_corridor"
        ) or limiting.startswith("absolute_humidity")
        action = "optional" if in_corridor and not harmful_path else "avoid"
        reason = (
            "climate_neutral_ventilation_optional"
            if action == "optional"
            else (
                "absolute_humidity_harm"
                if limiting.startswith("absolute_humidity")
                else "no_meaningful_target_improvement"
            )
        )
        return DurationRecommendation(
            complete_through > 0,
            action,
            None,
            None,
            limiting,
            before,
            before,
            0.0,
            complete_through,
            tuple(dict.fromkeys((reason, limiting))),
        )

    # Reaching the acceptance corridor is preferable to a mathematically
    # slightly closer point outside it.  Otherwise choose the smallest
    # normalised target distance, deterministically preferring the earlier
    # 15-minute point.
    corridor_points = [
        point for point in valid
        if _within_acceptance(point.temperature_c, point.relative_humidity_percent)  # type: ignore[arg-type]
    ]
    optimal = corridor_points[0] if corridor_points else min(
        valid, key=lambda point: (point.target_distance or float("inf"), point.minutes)
    )
    assert optimal.target_distance is not None
    improvement = 100.0 * (before - optimal.target_distance) / before

    # Overnight is deliberately strict: the contiguous safe path must reach
    # the 8-hour endpoint without a meaningful reversal after an earlier
    # optimum. Otherwise a duration beyond one hour is ordinary ventilation,
    # never a misleading "Stoßlüften" label.
    path_reaches_eight_hours = (
        eight_hour_complete
        and bool(path)
        and path[-1].minutes == TRAJECTORY_MAX_MINUTES
        and limiting is None
    )
    if path_reaches_eight_hours:
        action = "overnight"
        duration = TRAJECTORY_MAX_MINUTES
        limiting = None
        reasons = ("target_distance_improves_through_8h", "overnight_ventilation_benefit")
    elif optimal.minutes >= 60:
        action = "ventilate"
        duration = optimal.minutes
        limiting = limiting or "target_distance_optimum"
        reasons = ("target_distance_improves_for_duration", "ventilation_benefit")
    else:
        action = "short_airing"
        duration = optimal.minutes
        limiting = limiting or (
            "forecast_incomplete" if not eight_hour_complete else "target_distance_optimum"
        )
        reasons = ("target_distance_improves_short_term", "short_airing_benefit")
    return DurationRecommendation(
        True,
        action,
        duration,
        optimal.minutes,
        limiting,
        before,
        optimal.target_distance,
        improvement,
        complete_through,
        tuple(
            dict.fromkeys(
                (*reasons, *(("forecast_incomplete_short_only",) if not eight_hour_complete else ()))
            )
        ),
    )


def project_standard_room(
    *,
    now: datetime,
    indoor: AirState | None,
    outdoor: AirState | None,
    pressure_pa: float | None,
    hourly_forecast: Iterable[dict[str, Any]],
) -> VentilationProjection:
    """Project room state and a duration-aware advisory refinement.

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
            trajectory={},
            recommendation=DurationRecommendation(
                available=False,
                action="unavailable",
                recommended_duration_minutes=None,
                optimal_duration_minutes=None,
                limiting_factor="projection_inputs_unavailable",
                target_distance_before=None,
                target_distance_after=None,
                target_improvement_percent=None,
                complete_through_minutes=0,
                reason_codes=("projection_inputs_unavailable",),
            ),
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

    trajectory = _trajectory(
        now=timestamp,
        indoor=indoor,
        outdoor=outdoor,
        pressure_pa=valid_pressure,
        forecast_by_start=forecast_by_start,
    )
    recommendation = _duration_recommendation(
        indoor=indoor,
        outdoor=outdoor,
        trajectory=trajectory,
    )
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
        trajectory=trajectory,
        recommendation=recommendation,
        quality_flags=tuple(sorted((*combined_flags, *recommendation.reason_codes))),
    )
