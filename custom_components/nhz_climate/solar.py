"""Experimental local, unshaded GTI transposition for S02.

This module is intentionally independent of Home Assistant and provider I/O.
It transposes *supplied* hourly mean radiation and a supplied representative
solar geometry; it never manufactures a solar position, estimates a missing
radiation component, or replaces the provider GTI reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Literal

from .surfaces import Surface


OPEN_METEO_ALBEDO = 0.20
SKY_DIFFUSE_MODEL = "isotropic"
MODEL_VERSION = "local_unshaded_isotropic_v1"
INTERVAL_SEMANTICS = "mean_over_preceding_interval"
Quality = Literal["ok", "night", "unavailable"]


class SolarValidationError(ValueError):
    """The caller supplied an invalid timestamp, geometry, or irradiance."""


def _finite_number(value: float | int | None, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise SolarValidationError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SolarValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(result):
        raise SolarValidationError(f"{field} must be a finite number")
    return result


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SolarValidationError(f"{field} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class SolarPosition:
    """Solar geometry in the same south-relative azimuth convention as a Surface."""

    zenith_deg: float | None
    azimuth_deg: float | None

    def __post_init__(self) -> None:
        if self.zenith_deg is not None:
            zenith = _finite_number(self.zenith_deg, "solar_zenith_deg")
            if not 0 <= zenith <= 180:
                raise SolarValidationError(
                    "solar_zenith_deg must be between 0 and 180 degrees"
                )
        if self.azimuth_deg is not None:
            azimuth = _finite_number(self.azimuth_deg, "solar_azimuth_deg")
            if not -180 <= azimuth <= 180:
                raise SolarValidationError(
                    "solar_azimuth_deg must use south-relative convention and be between -180 and 180 degrees"
                )

    def missing_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.zenith_deg is None:
            fields.append("solar_zenith_deg")
        if self.azimuth_deg is None:
            fields.append("solar_azimuth_deg")
        return tuple(fields)

    def validated(self) -> tuple[float, float]:
        zenith = _finite_number(self.zenith_deg, "solar_zenith_deg")
        azimuth = _finite_number(self.azimuth_deg, "solar_azimuth_deg")
        return zenith, azimuth


@dataclass(frozen=True, slots=True)
class HorizontalIrradiance:
    """Hourly-mean horizontal radiation components in W/m²."""

    ghi_w_m2: float | None
    dni_w_m2: float | None
    dhi_w_m2: float | None

    def __post_init__(self) -> None:
        for field, value in (
            ("ghi_w_m2", self.ghi_w_m2),
            ("dni_w_m2", self.dni_w_m2),
            ("dhi_w_m2", self.dhi_w_m2),
        ):
            if value is not None and _finite_number(value, field) < 0:
                raise SolarValidationError(f"{field} must not be negative")

    def missing_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.ghi_w_m2 is None:
            fields.append("ghi_w_m2")
        if self.dni_w_m2 is None:
            fields.append("dni_w_m2")
        if self.dhi_w_m2 is None:
            fields.append("dhi_w_m2")
        return tuple(fields)

    def validated(self) -> tuple[float, float, float]:
        ghi = _finite_number(self.ghi_w_m2, "ghi_w_m2")
        dni = _finite_number(self.dni_w_m2, "dni_w_m2")
        dhi = _finite_number(self.dhi_w_m2, "dhi_w_m2")
        return ghi, dni, dhi


@dataclass(frozen=True, slots=True)
class TranspositionInput:
    """Inputs for one radiation interval ending at ``interval_end``.

    Open-Meteo hourly radiation values are means over the preceding hour.  If
    ``interval_start`` is omitted, this contract uses exactly the preceding
    elapsed hour, in UTC-safe arithmetic.  ``geometry_at`` is required when
    data are otherwise complete: it identifies the supplied representative
    geometry and must fall within that interval.  This avoids silently using
    the end timestamp's geometry for an hourly mean.
    """

    interval_end: datetime
    solar_position: SolarPosition
    irradiance: HorizontalIrradiance
    geometry_at: datetime | None = None
    interval_start: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.solar_position, SolarPosition):
            raise SolarValidationError("solar_position must be a SolarPosition")
        if not isinstance(self.irradiance, HorizontalIrradiance):
            raise SolarValidationError("irradiance must be a HorizontalIrradiance")
        end = _aware(self.interval_end, "interval_end")
        start = self.interval_start
        if start is None:
            # Local wall-clock subtraction is wrong across DST changes.  The
            # provider contract is an elapsed preceding hour, so subtract in
            # UTC and convert back only for a legible persisted timestamp.
            start = (end.astimezone(timezone.utc) - timedelta(hours=1)).astimezone(
                end.tzinfo
            )
            object.__setattr__(self, "interval_start", start)
        start = _aware(start, "interval_start")
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        if start_utc >= end_utc:
            raise SolarValidationError("interval_start must be before interval_end")
        if self.geometry_at is not None:
            geometry_at = _aware(self.geometry_at, "geometry_at")
            geometry_utc = geometry_at.astimezone(timezone.utc)
            if not start_utc <= geometry_utc <= end_utc:
                raise SolarValidationError(
                    "geometry_at must fall within the irradiance interval"
                )

    @property
    def missing_fields(self) -> tuple[str, ...]:
        fields = list(self.solar_position.missing_fields())
        fields.extend(self.irradiance.missing_fields())
        if self.geometry_at is None:
            fields.append("geometry_at")
        return tuple(fields)


@dataclass(frozen=True, slots=True)
class LocalGtiResult:
    """A transparent experimental result, never a provider-GTI replacement."""

    surface_id: str
    value_w_m2: float | None
    direct_w_m2: float | None
    sky_diffuse_w_m2: float | None
    ground_reflected_w_m2: float | None
    quality: Quality
    missing_inputs: tuple[str, ...]
    interval_start: datetime
    interval_end: datetime
    geometry_at: datetime | None
    model: str = MODEL_VERSION
    sky_model: str = SKY_DIFFUSE_MODEL
    albedo: float = OPEN_METEO_ALBEDO
    interval_semantics: str = INTERVAL_SEMANTICS
    experimental: bool = True
    shaded: bool = False

    @property
    def available(self) -> bool:
        return self.value_w_m2 is not None


def _cos_incidence(surface: Surface, zenith_deg: float, solar_azimuth_deg: float) -> float:
    """Return cosine of incidence for south-relative azimuths."""
    zenith = math.radians(zenith_deg)
    tilt = math.radians(surface.tilt_deg)
    azimuth_difference = math.radians(solar_azimuth_deg - surface.azimuth_deg)
    return (
        math.cos(zenith) * math.cos(tilt)
        + math.sin(zenith) * math.sin(tilt) * math.cos(azimuth_difference)
    )


def transpose_unshaded_gti(
    surface: Surface,
    sample: TranspositionInput,
    *,
    albedo: float = OPEN_METEO_ALBEDO,
) -> LocalGtiResult:
    """Transpose supplied mean GHI/DNI/DHI to a plane using an isotropic sky.

    ``sample`` must carry each of GHI, DNI, DHI, an explicitly timestamped
    representative solar position, and the interval ending timestamp.  If any
    are missing, the result is *unavailable*, not zero.  With complete inputs
    and the representative sun at/below the horizon, the result is a valid
    zero tagged ``night``.  Therefore a dashboard can distinguish darkness
    from a broken or unavailable upstream source.
    """
    if not isinstance(surface, Surface):
        raise SolarValidationError("surface must be a validated Surface")
    if not isinstance(sample, TranspositionInput):
        raise SolarValidationError("sample must be a TranspositionInput")
    albedo_value = _finite_number(albedo, "albedo")
    if not 0 <= albedo_value <= 1:
        raise SolarValidationError("albedo must be between 0 and 1")

    missing = sample.missing_fields
    if missing:
        return LocalGtiResult(
            surface_id=surface.surface_id,
            value_w_m2=None,
            direct_w_m2=None,
            sky_diffuse_w_m2=None,
            ground_reflected_w_m2=None,
            quality="unavailable",
            missing_inputs=missing,
            interval_start=sample.interval_start,
            interval_end=sample.interval_end,
            geometry_at=sample.geometry_at,
            albedo=albedo_value,
        )

    zenith, solar_azimuth = sample.solar_position.validated()
    ghi, dni, dhi = sample.irradiance.validated()
    if zenith >= 90:
        return LocalGtiResult(
            surface_id=surface.surface_id,
            value_w_m2=0.0,
            direct_w_m2=0.0,
            sky_diffuse_w_m2=0.0,
            ground_reflected_w_m2=0.0,
            quality="night",
            missing_inputs=(),
            interval_start=sample.interval_start,
            interval_end=sample.interval_end,
            geometry_at=sample.geometry_at,
            albedo=albedo_value,
        )

    tilt = math.radians(surface.tilt_deg)
    direct = dni * max(0.0, _cos_incidence(surface, zenith, solar_azimuth))
    sky_diffuse = dhi * (1.0 + math.cos(tilt)) / 2.0
    ground_reflected = ghi * albedo_value * (1.0 - math.cos(tilt)) / 2.0
    return LocalGtiResult(
        surface_id=surface.surface_id,
        value_w_m2=direct + sky_diffuse + ground_reflected,
        direct_w_m2=direct,
        sky_diffuse_w_m2=sky_diffuse,
        ground_reflected_w_m2=ground_reflected,
        quality="ok",
        missing_inputs=(),
        interval_start=sample.interval_start,
        interval_end=sample.interval_end,
        geometry_at=sample.geometry_at,
        albedo=albedo_value,
    )
