"""Pure, versioned psychrometric calculations for NHZ Climate.

The saturation-pressure equations are the ASHRAE implementation of the
Hyland/Wexler correlation, with separate ice and liquid-water branches.  All
inputs and calculated values use SI units internally: Pa, degC and kg/kg dry
air.  Invalid physical sensor input returns ``None`` rather than an invented
zero or a misleading calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite, log
from typing import Any


ALGORITHM_VERSION = "psychrometrics_v1"
MIN_TEMPERATURE_C = -40.0
MAX_TEMPERATURE_C = 60.0
MIN_RELATIVE_HUMIDITY_PERCENT = 0.0
MAX_RELATIVE_HUMIDITY_PERCENT = 100.0
EPSILON = 0.62198  # Ratio of water-vapour to dry-air gas constants.
DRY_AIR_GAS_CONSTANT_J_PER_KG_K = 287.042
DRY_AIR_SPECIFIC_HEAT_KJ_PER_KG_K = 1.006


@dataclass(frozen=True)
class AirState:
    """Validated moist-air state, expressed per kg of dry air."""

    temperature_c: float
    relative_humidity_percent: float
    pressure_pa: float
    saturation_vapour_pressure_pa: float
    vapour_pressure_pa: float
    humidity_ratio_kg_per_kg: float
    enthalpy_kj_per_kg: float


def _finite_number(value: Any) -> float | None:
    """Convert a sensor scalar without accepting booleans or non-finite data."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def saturation_vapour_pressure_pa(temperature_c: Any) -> float | None:
    """Return saturation vapour pressure in Pa using ice/water H/W branches.

    The published ASHRAE equations are evaluated in Kelvin.  The ice branch is
    selected below the water triple point (0.01 degC), preserving the intended
    frost behaviour rather than extrapolating liquid-water pressure below 0 C.
    """
    temperature = _finite_number(temperature_c)
    if temperature is None or not MIN_TEMPERATURE_C <= temperature <= MAX_TEMPERATURE_C:
        return None
    kelvin = temperature + 273.15
    if temperature < 0.01:
        log_pressure = (
            -5.6745359e3 / kelvin
            + 6.3925247
            - 9.677843e-3 * kelvin
            + 6.2215701e-7 * kelvin**2
            + 2.0747825e-9 * kelvin**3
            - 9.484024e-13 * kelvin**4
            + 4.1635019 * log(kelvin)
        )
    else:
        log_pressure = (
            -5.8002206e3 / kelvin
            + 1.3914993
            - 4.8640239e-2 * kelvin
            + 4.1764768e-5 * kelvin**2
            - 1.4452093e-8 * kelvin**3
            + 6.5459673 * log(kelvin)
        )
    pressure = exp(log_pressure)
    return pressure if isfinite(pressure) and pressure > 0 else None


def input_validation_errors(
    temperature_c: Any,
    relative_humidity_percent: Any,
    pressure_pa: Any,
) -> tuple[str, ...]:
    """Return stable error codes for an air-state input without raising."""
    errors: list[str] = []
    temperature = _finite_number(temperature_c)
    humidity = _finite_number(relative_humidity_percent)
    pressure = _finite_number(pressure_pa)
    if temperature is None or not MIN_TEMPERATURE_C <= temperature <= MAX_TEMPERATURE_C:
        errors.append("invalid_temperature")
    if humidity is None or not MIN_RELATIVE_HUMIDITY_PERCENT <= humidity <= MAX_RELATIVE_HUMIDITY_PERCENT:
        errors.append("invalid_relative_humidity")
    if pressure is None or pressure <= 0:
        errors.append("invalid_pressure")
    if errors:
        return tuple(errors)

    saturation_pressure = saturation_vapour_pressure_pa(temperature)
    assert saturation_pressure is not None  # guarded by the temperature range above
    vapour_pressure = saturation_pressure * humidity / 100.0
    if pressure <= vapour_pressure:
        errors.append("pressure_not_above_vapour_pressure")
    return tuple(errors)


def humidity_ratio_kg_per_kg(
    temperature_c: Any,
    relative_humidity_percent: Any,
    pressure_pa: Any,
) -> float | None:
    """Return humidity ratio ``w`` in kg water/kg dry air, or ``None``."""
    if input_validation_errors(temperature_c, relative_humidity_percent, pressure_pa):
        return None
    saturation_pressure = saturation_vapour_pressure_pa(temperature_c)
    assert saturation_pressure is not None
    vapour_pressure = saturation_pressure * float(relative_humidity_percent) / 100.0
    humidity_ratio = EPSILON * vapour_pressure / (float(pressure_pa) - vapour_pressure)
    return humidity_ratio if isfinite(humidity_ratio) and humidity_ratio >= 0 else None


def enthalpy_kj_per_kg(temperature_c: Any, humidity_ratio: Any) -> float | None:
    """Return moist-air enthalpy in kJ/kg dry air using the agreed formula."""
    temperature = _finite_number(temperature_c)
    ratio = _finite_number(humidity_ratio)
    if (
        temperature is None
        or not MIN_TEMPERATURE_C <= temperature <= MAX_TEMPERATURE_C
        or ratio is None
        or ratio < 0
    ):
        return None
    enthalpy = DRY_AIR_SPECIFIC_HEAT_KJ_PER_KG_K * temperature + ratio * (2501.0 + 1.86 * temperature)
    return enthalpy if isfinite(enthalpy) else None


def air_state(
    temperature_c: Any,
    relative_humidity_percent: Any,
    pressure_pa: Any,
) -> AirState | None:
    """Return the complete air state, or ``None`` when inputs are unphysical."""
    if input_validation_errors(temperature_c, relative_humidity_percent, pressure_pa):
        return None
    temperature = float(temperature_c)
    humidity = float(relative_humidity_percent)
    pressure = float(pressure_pa)
    saturation_pressure = saturation_vapour_pressure_pa(temperature)
    humidity_ratio = humidity_ratio_kg_per_kg(temperature, humidity, pressure)
    assert saturation_pressure is not None and humidity_ratio is not None
    enthalpy = enthalpy_kj_per_kg(temperature, humidity_ratio)
    assert enthalpy is not None
    return AirState(
        temperature_c=temperature,
        relative_humidity_percent=humidity,
        pressure_pa=pressure,
        saturation_vapour_pressure_pa=saturation_pressure,
        vapour_pressure_pa=saturation_pressure * humidity / 100.0,
        humidity_ratio_kg_per_kg=humidity_ratio,
        enthalpy_kj_per_kg=enthalpy,
    )


def dry_air_density_kg_per_m3(state: AirState) -> float:
    """Return dry-air mass per moist-air volume for a validated air state."""
    kelvin = state.temperature_c + 273.15
    density = (state.pressure_pa - state.vapour_pressure_pa) / (
        DRY_AIR_GAS_CONSTANT_J_PER_KG_K * kelvin
    )
    if not isfinite(density) or density <= 0:
        raise ValueError("validated air state produced non-positive dry-air density")
    return density
