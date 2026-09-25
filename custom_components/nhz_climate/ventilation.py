"""Pure input-quality and ventilation-potential calculations.

This module deliberately makes no Home Assistant imports.  The integration
layer supplies entity states as :class:`Measurement` values and maps an
unavailable result to HA's ``unavailable`` state.  It does not decide whether
to open a window; S07 owns that advisory policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from .psychrometrics import (
    ALGORITHM_VERSION,
    DRY_AIR_SPECIFIC_HEAT_KJ_PER_KG_K,
    AirState,
    air_state,
    dry_air_density_kg_per_m3,
    input_validation_errors,
)


LOCAL_OBSERVATION = "local_observation"
MODEL_FALLBACK = "model_fallback"
SUPPORTED_SOURCE_CLASSES = frozenset((LOCAL_OBSERVATION, MODEL_FALLBACK))
REFERENCE_AIRFLOW_M3_PER_H = 1.0


@dataclass(frozen=True)
class Measurement:
    """A scalar source value and the time at which that value was observed."""

    value: Any
    observed_at: datetime | None
    source_class: str
    source_entity: str | None = None


@dataclass(frozen=True)
class VentilationInputs:
    """One indoor zone against one common outdoor and pressure source."""

    indoor_temperature: Measurement
    indoor_relative_humidity: Measurement
    outdoor_temperature: Measurement
    outdoor_relative_humidity: Measurement
    pressure: Measurement


@dataclass(frozen=True)
class FreshnessPolicy:
    """Explicit, site-overridable source-quality limits for the SL pilot."""

    local_max_age: timedelta = timedelta(minutes=30)
    model_max_age: timedelta = timedelta(minutes=120)
    max_temperature_humidity_skew: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        for name, value in (
            ("local_max_age", self.local_max_age),
            ("model_max_age", self.model_max_age),
            ("max_temperature_humidity_skew", self.max_temperature_humidity_skew),
        ):
            if value < timedelta(0):
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True)
class VentilationPotential:
    """Numeric outdoor-minus-indoor potentials and hypothetical 1 m3/h rates."""

    indoor: AirState
    outdoor: AirState
    delta_humidity_ratio_kg_per_kg: float
    delta_humidity_ratio_g_per_kg: float
    delta_sensible_temperature_k: float
    delta_enthalpy_kj_per_kg: float
    reference_airflow_m3_per_h: float
    reference_dry_air_mass_flow_kg_per_h: float
    reference_moisture_rate_g_per_h: float
    reference_sensible_power_w: float
    reference_enthalpy_power_w: float


@dataclass(frozen=True)
class VentilationEvaluation:
    """A calculation result or a complete, machine-readable unavailable reason."""

    available: bool
    potential: VentilationPotential | None
    quality_flags: tuple[str, ...] = ()
    input_age_seconds: dict[str, float] = field(default_factory=dict)
    temperature_humidity_skew_seconds: dict[str, float] = field(default_factory=dict)
    source_quality: str | None = None
    algorithm_version: str = ALGORITHM_VERSION


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _utc_timestamp(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _measurement_age_seconds(measurement: Measurement, now: datetime) -> float | None:
    observed_at = _utc_timestamp(measurement.observed_at)
    if observed_at is None:
        return None
    return (now - observed_at).total_seconds()


def _max_age_for(measurement: Measurement, policy: FreshnessPolicy) -> timedelta | None:
    if measurement.source_class == LOCAL_OBSERVATION:
        return policy.local_max_age
    if measurement.source_class == MODEL_FALLBACK:
        return policy.model_max_age
    return None


def _measurement_quality(
    name: str,
    measurement: Measurement,
    now: datetime,
    policy: FreshnessPolicy,
) -> tuple[float | None, tuple[str, ...]]:
    """Validate generic scalar/source/timestamp requirements for one input."""
    flags: list[str] = []
    value = _finite_number(measurement.value)
    if value is None:
        flags.append(f"{name}_invalid_value")
    max_age = _max_age_for(measurement, policy)
    if max_age is None:
        flags.append(f"{name}_unsupported_source_class")
    age_seconds = _measurement_age_seconds(measurement, now)
    if age_seconds is None:
        flags.append(f"{name}_missing_timestamp")
    elif age_seconds < 0:
        flags.append(f"{name}_future_timestamp")
    elif max_age is not None and age_seconds > max_age.total_seconds():
        flags.append(f"{name}_stale")
    return age_seconds, tuple(flags)


def _pair_skew(
    pair_name: str,
    temperature: Measurement,
    humidity: Measurement,
    policy: FreshnessPolicy,
) -> tuple[float | None, tuple[str, ...]]:
    temperature_at = _utc_timestamp(temperature.observed_at)
    humidity_at = _utc_timestamp(humidity.observed_at)
    if temperature_at is None or humidity_at is None:
        return None, ()  # The individual missing-timestamp flags are clearer.
    skew_seconds = abs((temperature_at - humidity_at).total_seconds())
    flags: list[str] = []
    if skew_seconds > policy.max_temperature_humidity_skew.total_seconds():
        flags.append(f"{pair_name}_temperature_humidity_skew")
    # A model T/RH pair has hourly semantics: both fields must be for the same
    # provider timestep, not merely within a permissive local-sensor interval.
    if (
        temperature.source_class == MODEL_FALLBACK
        and humidity.source_class == MODEL_FALLBACK
        and skew_seconds != 0
    ):
        flags.append(f"{pair_name}_model_timestamp_mismatch")
    return skew_seconds, tuple(flags)


def _source_quality(inputs: VentilationInputs) -> str:
    classes = {
        measurement.source_class
        for measurement in (
            inputs.indoor_temperature,
            inputs.indoor_relative_humidity,
            inputs.outdoor_temperature,
            inputs.outdoor_relative_humidity,
            inputs.pressure,
        )
    }
    if classes == {LOCAL_OBSERVATION}:
        return LOCAL_OBSERVATION
    if classes == {MODEL_FALLBACK}:
        return MODEL_FALLBACK
    return "mixed_observation_model"


def evaluate_ventilation_potential(
    inputs: VentilationInputs,
    *,
    now: datetime,
    policy: FreshnessPolicy = FreshnessPolicy(),
) -> VentilationEvaluation:
    """Evaluate potential with explicit quality gates, never silently filling data.

    Delta quantities all follow the agreed ``outdoor - indoor`` convention:
    positive humidity/energy values mean that supplied outdoor air adds that
    quantity to the zone; negative values mean removal.  Rates are hypothetical
    values for exactly 1 m3/h outdoor volumetric flow, never inferred window
    airflow.
    """
    now_utc = _utc_timestamp(now)
    if now_utc is None:
        raise ValueError("now must be timezone-aware")

    measurements = {
        "indoor_temperature": inputs.indoor_temperature,
        "indoor_relative_humidity": inputs.indoor_relative_humidity,
        "outdoor_temperature": inputs.outdoor_temperature,
        "outdoor_relative_humidity": inputs.outdoor_relative_humidity,
        "pressure": inputs.pressure,
    }
    flags: list[str] = []
    ages: dict[str, float] = {}
    for name, measurement in measurements.items():
        age_seconds, measurement_flags = _measurement_quality(name, measurement, now_utc, policy)
        if age_seconds is not None:
            ages[name] = age_seconds
        flags.extend(measurement_flags)

    skews: dict[str, float] = {}
    for pair_name, temperature, humidity in (
        ("indoor", inputs.indoor_temperature, inputs.indoor_relative_humidity),
        ("outdoor", inputs.outdoor_temperature, inputs.outdoor_relative_humidity),
    ):
        skew_seconds, skew_flags = _pair_skew(pair_name, temperature, humidity, policy)
        if skew_seconds is not None:
            skews[pair_name] = skew_seconds
        flags.extend(skew_flags)

    # Do not attempt psychrometrics after failed source-quality gates. This
    # protects a caller from accidentally treating stale states as live air.
    if flags:
        return VentilationEvaluation(
            available=False,
            potential=None,
            quality_flags=tuple(sorted(set(flags))),
            input_age_seconds=ages,
            temperature_humidity_skew_seconds=skews,
            source_quality=_source_quality(inputs),
        )

    indoor_errors = input_validation_errors(
        inputs.indoor_temperature.value,
        inputs.indoor_relative_humidity.value,
        inputs.pressure.value,
    )
    outdoor_errors = input_validation_errors(
        inputs.outdoor_temperature.value,
        inputs.outdoor_relative_humidity.value,
        inputs.pressure.value,
    )
    if indoor_errors or outdoor_errors:
        return VentilationEvaluation(
            available=False,
            potential=None,
            quality_flags=tuple(sorted({*(f"indoor_{error}" for error in indoor_errors), *(f"outdoor_{error}" for error in outdoor_errors)})),
            input_age_seconds=ages,
            temperature_humidity_skew_seconds=skews,
            source_quality=_source_quality(inputs),
        )

    indoor = air_state(
        inputs.indoor_temperature.value,
        inputs.indoor_relative_humidity.value,
        inputs.pressure.value,
    )
    outdoor = air_state(
        inputs.outdoor_temperature.value,
        inputs.outdoor_relative_humidity.value,
        inputs.pressure.value,
    )
    assert indoor is not None and outdoor is not None
    delta_w = outdoor.humidity_ratio_kg_per_kg - indoor.humidity_ratio_kg_per_kg
    delta_temperature = outdoor.temperature_c - indoor.temperature_c
    delta_enthalpy = outdoor.enthalpy_kj_per_kg - indoor.enthalpy_kj_per_kg
    dry_air_mass_flow = dry_air_density_kg_per_m3(outdoor) * REFERENCE_AIRFLOW_M3_PER_H
    potential = VentilationPotential(
        indoor=indoor,
        outdoor=outdoor,
        delta_humidity_ratio_kg_per_kg=delta_w,
        delta_humidity_ratio_g_per_kg=delta_w * 1000.0,
        delta_sensible_temperature_k=delta_temperature,
        delta_enthalpy_kj_per_kg=delta_enthalpy,
        reference_airflow_m3_per_h=REFERENCE_AIRFLOW_M3_PER_H,
        reference_dry_air_mass_flow_kg_per_h=dry_air_mass_flow,
        reference_moisture_rate_g_per_h=delta_w * dry_air_mass_flow * 1000.0,
        reference_sensible_power_w=(
            DRY_AIR_SPECIFIC_HEAT_KJ_PER_KG_K * delta_temperature * dry_air_mass_flow / 3.6
        ),
        reference_enthalpy_power_w=delta_enthalpy * dry_air_mass_flow / 3.6,
    )
    return VentilationEvaluation(
        available=True,
        potential=potential,
        input_age_seconds=ages,
        temperature_humidity_skew_seconds=skews,
        source_quality=_source_quality(inputs),
    )
