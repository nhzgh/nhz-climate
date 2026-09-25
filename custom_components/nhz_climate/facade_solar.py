"""Pure S03 calculations for incident facade and roof radiation.

This module intentionally stops at *incident solar radiation without local
shading correction*.  It converts the S02 GTI result (W/m²) into a power on a
configured plane (W) and integrates interval means using elapsed UTC time.
It does not estimate shading, absorption, solar gains, heating or cooling
loads.

``SurfaceAreaBreakdown`` keeps a facade's gross plane, glazing and any other
opening disjoint.  The opaque area is derived exactly once as
``gross - window - other_openings``.  Consequently total incident power is a
plane value, while window, opaque and other-opening powers partition it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Literal, Sequence

from .solar import LocalGtiResult
from .surfaces import Surface, SurfaceType


MODEL_VERSION = "incident_solar_only_v1"
UNSHADED_DESCRIPTION = "without_local_shading_correction"
Quality = Literal["ok", "night", "partial", "unavailable"]


class FacadeSolarValidationError(ValueError):
    """A surface area, GTI result, or integration interval is invalid."""


def _finite_nonnegative(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise FacadeSolarValidationError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FacadeSolarValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise FacadeSolarValidationError(f"{field} must be a finite number")
    if number < 0:
        raise FacadeSolarValidationError(f"{field} must not be negative")
    return number


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FacadeSolarValidationError(f"{field} must be timezone-aware")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _coverage(covered_seconds: float, expected_seconds: float) -> float:
    if expected_seconds <= 0:
        return 0.0
    return min(1.0, max(0.0, covered_seconds / expected_seconds))


@dataclass(frozen=True, slots=True)
class SurfaceAreaBreakdown:
    """A disjoint surface partition in square metres.

    ``other_opening_area_m2`` represents openings such as an opaque door.  It
    is intentionally not part of ``window_area_m2``.  Therefore it is removed
    once, and only once, from the derived opaque area.

    A zero gross area is permitted so a removed or not-yet-measured configured
    footprint yields a traceable 0 W rather than pretending to be a data gap.
    """

    gross_area_m2: float
    window_area_m2: float = 0.0
    other_opening_area_m2: float = 0.0

    def __post_init__(self) -> None:
        for field in ("gross_area_m2", "window_area_m2", "other_opening_area_m2"):
            object.__setattr__(self, field, _finite_nonnegative(getattr(self, field), field))
        if self.window_area_m2 + self.other_opening_area_m2 > self.gross_area_m2:
            raise FacadeSolarValidationError(
                "window_area_m2 plus other_opening_area_m2 must not exceed gross_area_m2"
            )

    @property
    def opaque_area_m2(self) -> float:
        """Gross area minus the two disjoint kinds of opening."""
        return self.gross_area_m2 - self.window_area_m2 - self.other_opening_area_m2

    @classmethod
    def from_surface(
        cls, surface: Surface, *, other_opening_area_m2: float | None = None
    ) -> "SurfaceAreaBreakdown":
        """Build an S03 partition from the validated S02 surface geometry."""
        if not isinstance(surface, Surface):
            raise FacadeSolarValidationError("surface must be a validated Surface")
        return cls(
            gross_area_m2=surface.gross_area_m2,
            window_area_m2=surface.window_area_m2,
            other_opening_area_m2=(
                surface.other_opening_area_m2
                if other_opening_area_m2 is None
                else other_opening_area_m2
            ),
        )


@dataclass(frozen=True, slots=True)
class IncidentSolarPower:
    """Incident, unshaded plane power for one S02 GTI interval.

    Values ending in ``_w`` are powers, never irradiance densities.  ``total``
    includes all of the gross plane.  The four area components form the exact
    identity ``total = window + opaque + other_opening`` when available.
    """

    surface_id: str
    surface_type: SurfaceType
    areas: SurfaceAreaBreakdown
    gti_w_m2: float | None
    total_w: float | None
    window_w: float | None
    opaque_w: float | None
    other_opening_w: float | None
    quality: Quality
    coverage_fraction: float
    missing_inputs: tuple[str, ...]
    interval_start: datetime
    interval_end: datetime
    source_model: str
    model: str = MODEL_VERSION
    unshaded: bool = True
    shading_description: str = UNSHADED_DESCRIPTION

    @property
    def available(self) -> bool:
        return self.total_w is not None


@dataclass(frozen=True, slots=True)
class IncidentSolarEnergy:
    """Integrated incident energy for one surface over a requested period.

    A partial result is a sum of only measured/modelled intervals.  Its
    ``coverage_fraction`` reports their elapsed-UTC-time coverage; a missing
    interval is never replaced by a zero.  A result is ``unavailable`` only
    when no usable interval exists.
    """

    scope_id: str
    # Present for a per-surface series; ``None`` deliberately means that this
    # is a group/building aggregate rather than falsely identifying one plane.
    surface_id: str | None
    surface_ids: tuple[str, ...]
    total_kwh: float | None
    window_kwh: float | None
    opaque_kwh: float | None
    other_opening_kwh: float | None
    quality: Quality
    coverage_fraction: float
    covered_seconds: float
    expected_seconds: float
    interval_start: datetime
    interval_end: datetime
    missing_surface_ids: tuple[str, ...]
    model: str = MODEL_VERSION
    unshaded: bool = True
    shading_description: str = UNSHADED_DESCRIPTION

    @property
    def available(self) -> bool:
        return self.total_kwh is not None


@dataclass(frozen=True, slots=True)
class IncidentSolarPowerAggregate:
    """A simultaneous sum of scalar powers, never of W/m² values."""

    scope_id: str
    surface_ids: tuple[str, ...]
    total_w: float | None
    window_w: float | None
    opaque_w: float | None
    other_opening_w: float | None
    quality: Quality
    coverage_fraction: float
    interval_start: datetime
    interval_end: datetime
    missing_surface_ids: tuple[str, ...]
    model: str = MODEL_VERSION
    unshaded: bool = True
    shading_description: str = UNSHADED_DESCRIPTION

    @property
    def available(self) -> bool:
        return self.total_w is not None


def _source_quality(source: LocalGtiResult) -> Quality:
    if source.quality == "night":
        return "night"
    if source.quality == "ok":
        return "ok"
    if source.quality == "unavailable":
        return "unavailable"
    raise FacadeSolarValidationError(f"unsupported GTI quality: {source.quality!r}")


def incident_solar_power_for_areas(
    *,
    surface_id: str,
    surface_type: SurfaceType,
    areas: SurfaceAreaBreakdown,
    gti: LocalGtiResult,
) -> IncidentSolarPower:
    """Convert a single S02 GTI result from W/m² to scalar incident W.

    This low-level entry point accepts a zero area deliberately.  Use
    :func:`incident_solar_power` with a persisted :class:`Surface` in normal
    integration code.
    """
    if not isinstance(surface_id, str) or not surface_id:
        raise FacadeSolarValidationError("surface_id must not be empty")
    try:
        normalised_type = SurfaceType(surface_type)
    except (TypeError, ValueError) as exc:
        raise FacadeSolarValidationError("surface_type must be a SurfaceType") from exc
    if not isinstance(areas, SurfaceAreaBreakdown):
        raise FacadeSolarValidationError("areas must be a SurfaceAreaBreakdown")
    if not isinstance(gti, LocalGtiResult):
        raise FacadeSolarValidationError("gti must be a LocalGtiResult")
    if gti.surface_id != surface_id:
        raise FacadeSolarValidationError(
            "gti surface_id must match the target surface_id"
        )
    if normalised_type is not SurfaceType.FACADE and (
        areas.window_area_m2 or areas.other_opening_area_m2
    ):
        raise FacadeSolarValidationError(
            "window and other-opening areas are only applicable to facades"
        )

    start = _aware(gti.interval_start, "gti.interval_start")
    end = _aware(gti.interval_end, "gti.interval_end")
    if _utc(start) >= _utc(end):
        raise FacadeSolarValidationError("gti interval_start must be before interval_end")
    quality = _source_quality(gti)

    if gti.value_w_m2 is None:
        return IncidentSolarPower(
            surface_id=surface_id,
            surface_type=normalised_type,
            areas=areas,
            gti_w_m2=None,
            total_w=None,
            window_w=None,
            opaque_w=None,
            other_opening_w=None,
            quality="unavailable",
            coverage_fraction=0.0,
            missing_inputs=tuple(gti.missing_inputs),
            interval_start=start,
            interval_end=end,
            source_model=gti.model,
        )

    gti_w_m2 = _finite_nonnegative(gti.value_w_m2, "gti.value_w_m2")
    # A non-zero source value paired with an unavailable quality would be an
    # upstream contract violation, not a value callers may silently use.
    if quality == "unavailable":
        raise FacadeSolarValidationError("unavailable GTI result must not contain a value")
    return IncidentSolarPower(
        surface_id=surface_id,
        surface_type=normalised_type,
        areas=areas,
        gti_w_m2=gti_w_m2,
        total_w=gti_w_m2 * areas.gross_area_m2,
        window_w=gti_w_m2 * areas.window_area_m2,
        opaque_w=gti_w_m2 * areas.opaque_area_m2,
        other_opening_w=gti_w_m2 * areas.other_opening_area_m2,
        quality=quality,
        coverage_fraction=1.0,
        missing_inputs=(),
        interval_start=start,
        interval_end=end,
        source_model=gti.model,
    )


def incident_solar_power(
    surface: Surface,
    gti: LocalGtiResult,
    *,
    other_opening_area_m2: float | None = None,
) -> IncidentSolarPower:
    """Calculate incident power from a normal persisted S02 surface."""
    areas = SurfaceAreaBreakdown.from_surface(
        surface, other_opening_area_m2=other_opening_area_m2
    )
    return incident_solar_power_for_areas(
        surface_id=surface.surface_id,
        surface_type=surface.surface_type,
        areas=areas,
        gti=gti,
    )


def _validate_period(
    samples: Sequence[IncidentSolarPower],
    period_start: datetime | None,
    period_end: datetime | None,
) -> tuple[datetime, datetime, tuple[IncidentSolarPower, ...]]:
    if not samples:
        raise FacadeSolarValidationError("at least one power sample is required")
    if (period_start is None) != (period_end is None):
        raise FacadeSolarValidationError("period_start and period_end must be supplied together")

    if any(not isinstance(sample, IncidentSolarPower) for sample in samples):
        raise FacadeSolarValidationError("samples must contain IncidentSolarPower values")
    ordered = tuple(sorted(samples, key=lambda item: _utc(item.interval_start)))
    for sample in ordered:
        if not isinstance(sample, IncidentSolarPower):
            raise FacadeSolarValidationError("samples must contain IncidentSolarPower values")
        _aware(sample.interval_start, "sample.interval_start")
        _aware(sample.interval_end, "sample.interval_end")
        if _utc(sample.interval_start) >= _utc(sample.interval_end):
            raise FacadeSolarValidationError("sample interval_start must be before interval_end")

    if period_start is None:
        start = ordered[0].interval_start
        end = max(ordered, key=lambda item: _utc(item.interval_end)).interval_end
    else:
        start = _aware(period_start, "period_start")
        end = _aware(period_end, "period_end")
        if _utc(start) >= _utc(end):
            raise FacadeSolarValidationError("period_start must be before period_end")
        if any(
            _utc(sample.interval_start) < _utc(start)
            or _utc(sample.interval_end) > _utc(end)
            for sample in ordered
        ):
            raise FacadeSolarValidationError("sample intervals must fall within the requested period")

    previous_end: datetime | None = None
    for sample in ordered:
        sample_start = _utc(sample.interval_start)
        if previous_end is not None and sample_start < previous_end:
            raise FacadeSolarValidationError("sample intervals must not overlap")
        previous_end = _utc(sample.interval_end)
    return start, end, ordered


def _integrate_field(samples: Sequence[IncidentSolarPower], field: str) -> float:
    total = 0.0
    for sample in samples:
        value = getattr(sample, field)
        if value is None:
            continue
        seconds = (_utc(sample.interval_end) - _utc(sample.interval_start)).total_seconds()
        total += value * seconds / 3_600_000.0
    return total


def integrate_incident_energy(
    samples: Sequence[IncidentSolarPower],
    *,
    scope_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> IncidentSolarEnergy:
    """Integrate mean powers to kWh over their actual elapsed UTC duration.

    A partial-hour interval contributes proportionally.  DST is correct because
    all durations are measured in UTC; a Berlin local calendar day can thus
    contain 23 or 25 elapsed hours.  Gaps and unavailable samples yield a
    ``partial`` result when usable samples remain, otherwise ``unavailable``.
    """
    start, end, ordered = _validate_period(samples, period_start, period_end)
    sample_ids = {sample.surface_id for sample in ordered}
    if len(sample_ids) != 1:
        raise FacadeSolarValidationError("energy samples must belong to one surface")
    identity = scope_id or ordered[0].surface_id
    if not isinstance(identity, str) or not identity:
        raise FacadeSolarValidationError("scope_id must not be empty")

    expected_seconds = (_utc(end) - _utc(start)).total_seconds()
    usable = tuple(sample for sample in ordered if sample.available)
    covered_seconds = sum(
        (_utc(sample.interval_end) - _utc(sample.interval_start)).total_seconds()
        for sample in usable
    )
    # A gap between two valid samples is represented by coverage below one;
    # no zero-power interval is invented to bridge it.
    coverage_fraction = _coverage(covered_seconds, expected_seconds)
    missing_ids = tuple(sorted({sample.surface_id for sample in ordered if not sample.available}))

    if not usable:
        return IncidentSolarEnergy(
            scope_id=identity,
            surface_id=ordered[0].surface_id,
            surface_ids=(ordered[0].surface_id,),
            total_kwh=None,
            window_kwh=None,
            opaque_kwh=None,
            other_opening_kwh=None,
            quality="unavailable",
            coverage_fraction=0.0,
            covered_seconds=0.0,
            expected_seconds=expected_seconds,
            interval_start=start,
            interval_end=end,
            missing_surface_ids=missing_ids or (ordered[0].surface_id,),
        )

    complete = coverage_fraction == 1.0 and not missing_ids
    if complete and all(sample.quality == "night" for sample in usable):
        quality: Quality = "night"
    elif complete:
        quality = "ok"
    else:
        quality = "partial"
    return IncidentSolarEnergy(
        scope_id=identity,
        surface_id=ordered[0].surface_id,
        surface_ids=(ordered[0].surface_id,),
        total_kwh=_integrate_field(usable, "total_w"),
        window_kwh=_integrate_field(usable, "window_w"),
        opaque_kwh=_integrate_field(usable, "opaque_w"),
        other_opening_kwh=_integrate_field(usable, "other_opening_w"),
        quality=quality,
        coverage_fraction=coverage_fraction,
        covered_seconds=covered_seconds,
        expected_seconds=expected_seconds,
        interval_start=start,
        interval_end=end,
        missing_surface_ids=missing_ids,
    )


def aggregate_incident_power(
    scope_id: str, samples: Sequence[IncidentSolarPower]
) -> IncidentSolarPowerAggregate:
    """Sum concurrent surface powers for a facade group, roof, or building.

    The aggregate deliberately has no irradiance-density field: aggregation is
    only meaningful for W here.  When one plane is unavailable, known planes
    are retained as a clearly marked partial sum with surface-count coverage.
    """
    if not isinstance(scope_id, str) or not scope_id:
        raise FacadeSolarValidationError("scope_id must not be empty")
    if not samples:
        raise FacadeSolarValidationError("at least one power sample is required")
    if any(not isinstance(sample, IncidentSolarPower) for sample in samples):
        raise FacadeSolarValidationError("samples must contain IncidentSolarPower values")
    surface_ids = [sample.surface_id for sample in samples]
    if len(surface_ids) != len(set(surface_ids)):
        raise FacadeSolarValidationError(
            "power aggregate must not contain duplicate surface_id values"
        )
    reference_start = _utc(samples[0].interval_start)
    reference_end = _utc(samples[0].interval_end)
    if any(
        _utc(sample.interval_start) != reference_start or _utc(sample.interval_end) != reference_end
        for sample in samples
    ):
        raise FacadeSolarValidationError("power samples must describe the same interval")

    usable = tuple(sample for sample in samples if sample.available)
    coverage_fraction = len(usable) / len(samples)
    missing_ids = tuple(sorted(sample.surface_id for sample in samples if not sample.available))
    if not usable:
        quality: Quality = "unavailable"
        fields: tuple[float | None, float | None, float | None, float | None] = (
            None,
            None,
            None,
            None,
        )
    else:
        fields = tuple(
            sum(getattr(sample, field) for sample in usable)
            for field in ("total_w", "window_w", "opaque_w", "other_opening_w")
        )
        if coverage_fraction < 1:
            quality = "partial"
        elif all(sample.quality == "night" for sample in usable):
            quality = "night"
        else:
            quality = "ok"
    return IncidentSolarPowerAggregate(
        scope_id=scope_id,
        surface_ids=tuple(sorted({sample.surface_id for sample in samples})),
        total_w=fields[0],
        window_w=fields[1],
        opaque_w=fields[2],
        other_opening_w=fields[3],
        quality=quality,
        coverage_fraction=coverage_fraction,
        interval_start=samples[0].interval_start,
        interval_end=samples[0].interval_end,
        missing_surface_ids=missing_ids,
    )


def aggregate_incident_energy(
    scope_id: str, samples: Sequence[IncidentSolarEnergy]
) -> IncidentSolarEnergy:
    """Sum same-period scalar energies for a facade group, roof, or building.

    ``coverage_fraction`` is the surface-time coverage: covered seconds over
    expected seconds across all input surfaces.  It retains the evidence needed
    to distinguish a partial scalar sum from a complete building total.
    """
    if not isinstance(scope_id, str) or not scope_id:
        raise FacadeSolarValidationError("scope_id must not be empty")
    if not samples:
        raise FacadeSolarValidationError("at least one energy sample is required")
    if any(not isinstance(sample, IncidentSolarEnergy) for sample in samples):
        raise FacadeSolarValidationError("samples must contain IncidentSolarEnergy values")
    all_surface_ids = [
        identifier for sample in samples for identifier in sample.surface_ids
    ]
    if len(all_surface_ids) != len(set(all_surface_ids)):
        raise FacadeSolarValidationError(
            "energy aggregate must not contain duplicate surface_id values"
        )
    reference_start = _utc(samples[0].interval_start)
    reference_end = _utc(samples[0].interval_end)
    if any(
        _utc(sample.interval_start) != reference_start or _utc(sample.interval_end) != reference_end
        for sample in samples
    ):
        raise FacadeSolarValidationError("energy samples must describe the same period")

    usable = tuple(sample for sample in samples if sample.available)
    expected_seconds = sum(sample.expected_seconds for sample in samples)
    covered_seconds = sum(sample.covered_seconds for sample in samples)
    coverage_fraction = _coverage(covered_seconds, expected_seconds)
    missing_ids = tuple(sorted({identifier for sample in samples for identifier in sample.missing_surface_ids}))
    if not usable:
        quality: Quality = "unavailable"
        fields: tuple[float | None, float | None, float | None, float | None] = (
            None,
            None,
            None,
            None,
        )
    else:
        fields = tuple(
            sum(getattr(sample, field) for sample in usable)
            for field in ("total_kwh", "window_kwh", "opaque_kwh", "other_opening_kwh")
        )
        if coverage_fraction < 1 or any(sample.quality == "partial" for sample in usable):
            quality = "partial"
        elif all(sample.quality == "night" for sample in usable):
            quality = "night"
        else:
            quality = "ok"
    return IncidentSolarEnergy(
        scope_id=scope_id,
        surface_id=None,
        surface_ids=tuple(sorted({identifier for sample in samples for identifier in sample.surface_ids})),
        total_kwh=fields[0],
        window_kwh=fields[1],
        opaque_kwh=fields[2],
        other_opening_kwh=fields[3],
        quality=quality,
        coverage_fraction=coverage_fraction,
        covered_seconds=covered_seconds,
        expected_seconds=expected_seconds,
        interval_start=samples[0].interval_start,
        interval_end=samples[0].interval_end,
        missing_surface_ids=missing_ids,
    )
