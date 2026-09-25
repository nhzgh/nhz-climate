"""Home Assistant state adapter for the local ventilation pilot.

This module is deliberately independent of Home Assistant.  The integration
turns a HA ``State`` into :class:`EntityStateInput`; all unit conversion,
freshness checks and provenance construction happen here and can therefore be
tested without a running HA instance.

It is intentionally conservative: a model value can supply a psychrometric
comparison, but it can never prove that the local rain/gust safety sources are
safe.  In particular, the commonly unchanged GW1100A ``0 mm/h`` rain state is
accepted only while the shared outdoor temperature *and* humidity sources are
fresh and mutually time-aligned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Literal

from .ventilation import (
    LOCAL_OBSERVATION,
    MODEL_FALLBACK,
    FreshnessPolicy,
    Measurement,
    VentilationEvaluation,
    VentilationInputs,
    evaluate_ventilation_potential,
)
from .ventilation_advisory import AdvisoryDecision, VentilationAdvisory, WeatherSafetyInputs


Quantity = Literal[
    "temperature",
    "relative_humidity",
    "pressure",
    "rain_rate",
    "rain_counter",
    "wind_gust",
]

CANONICAL_UNITS: dict[Quantity, str] = {
    "temperature": "°C",
    "relative_humidity": "%",
    "pressure": "Pa",
    "rain_rate": "mm/h",
    "rain_counter": "mm",
    "wind_gust": "km/h",
}


@dataclass(frozen=True)
class EntityStateInput:
    """The small, HA-state-shaped contract accepted by this module.

    ``last_updated`` should be HA's timestamp at which the source was last
    supplied.  It must be timezone-aware; naive values are deliberately not
    interpreted in the host timezone.  ``source_class`` is the existing S01
    provenance value and controls the 30/120-minute freshness policy.
    """

    entity_id: str
    state: Any
    unit_of_measurement: str | None
    last_updated: datetime | None
    source_class: str = LOCAL_OBSERVATION


@dataclass(frozen=True)
class SourceProvenance:
    """Serializable, per-input traceability kept with every snapshot."""

    entity_id: str
    source_class: str
    observed_at: datetime | None
    original_unit: str | None
    canonical_unit: str
    conversion: str | None
    value: float | None
    error_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "source_class": self.source_class,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "original_unit": self.original_unit,
            "canonical_unit": self.canonical_unit,
            "conversion": self.conversion,
            "value": self.value,
            "error_codes": list(self.error_codes),
        }


@dataclass(frozen=True)
class NormalizedMeasurement:
    """A canonical source value plus a ``Measurement`` for S01."""

    name: str
    quantity: Quantity
    measurement: Measurement
    provenance: SourceProvenance

    @property
    def value(self) -> float | None:
        value = self.measurement.value
        return value if isinstance(value, float) else None


@dataclass(frozen=True)
class SourceFreshness:
    """Timestamp/source-class freshness independent of unit validation."""

    fresh: bool
    age_seconds: float | None
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StationHealth:
    """Whether a shared GW1100A source can safely prove a zero rain rate."""

    fresh: bool
    temperature_age_seconds: float | None
    humidity_age_seconds: float | None
    temperature_humidity_skew_seconds: float | None
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "fresh": self.fresh,
            "temperature_age_seconds": self.temperature_age_seconds,
            "humidity_age_seconds": self.humidity_age_seconds,
            "temperature_humidity_skew_seconds": self.temperature_humidity_skew_seconds,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class OutdoorRuntimeInputs:
    """The shared sources used by each local-zone evaluation."""

    temperature: EntityStateInput
    relative_humidity: EntityStateInput
    pressure: EntityStateInput
    rain_rate: EntityStateInput
    wind_gust: EntityStateInput
    rain_counter: EntityStateInput | None = None


@dataclass(frozen=True)
class ZoneRuntimeInputs:
    """One zone's indoor source pair and its common outdoor station."""

    zone_id: str
    zone_name: str
    indoor_temperature: EntityStateInput
    indoor_relative_humidity: EntityStateInput
    outdoor: OutdoorRuntimeInputs


@dataclass(frozen=True)
class VentilationRuntimeSnapshot:
    """One explainable zone evaluation suitable for HA state attributes."""

    zone_id: str
    zone_name: str
    evaluated_at: datetime
    potential: VentilationEvaluation
    decision: AdvisoryDecision
    station_health: StationHealth
    provenance: dict[str, SourceProvenance] = field(default_factory=dict)
    error_codes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.potential.available and self.decision.status != "unavailable"

    def as_attributes(self) -> dict[str, object]:
        """Return plain JSON-compatible diagnostic attributes for HA entities."""
        return {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "evaluated_at": self.evaluated_at.isoformat(),
            "available": self.available,
            "status": self.decision.status,
            "reason_codes": list(self.decision.reason_codes),
            "error_codes": list(self.error_codes),
            "potential_quality_flags": list(self.potential.quality_flags),
            "source_quality": self.potential.source_quality,
            "input_age_seconds": dict(self.potential.input_age_seconds),
            "temperature_humidity_skew_seconds": dict(self.potential.temperature_humidity_skew_seconds),
            "station_health": self.station_health.as_dict(),
            "source_provenance": {name: item.as_dict() for name, item in self.provenance.items()},
        }


def _utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _unit_key(unit: str | None) -> str | None:
    if not isinstance(unit, str):
        return None
    stripped = unit.strip()
    if not stripped:
        return None
    return stripped.lower().replace(" ", "")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _converter(quantity: Quantity, unit: str | None) -> tuple[float, float, str] | None:
    """Return multiplier, offset and a human-readable conversion, if valid."""
    key = _unit_key(unit)
    if quantity == "temperature":
        if key in {"°c", "c", "celsius", "°celsius"}:
            return 1.0, 0.0, "identity"
        if key in {"°f", "f", "fahrenheit", "°fahrenheit"}:
            return 5.0 / 9.0, -160.0 / 9.0, "°F_to_°C"
    elif quantity == "relative_humidity" and key in {"%", "percent", "percentage"}:
        return 1.0, 0.0, "identity"
    elif quantity == "pressure":
        if key == "pa":
            return 1.0, 0.0, "identity"
        if key in {"hpa", "mbar"}:
            return 100.0, 0.0, "hPa_to_Pa"
        if key == "kpa":
            return 1000.0, 0.0, "kPa_to_Pa"
        if key == "bar":
            return 100000.0, 0.0, "bar_to_Pa"
    elif quantity == "rain_rate" and key in {"mm/h", "mm/hr", "mmh-1", "mmperh"}:
        return 1.0, 0.0, "identity"
    elif quantity == "rain_counter" and key in {"mm", "millimeter", "millimetre"}:
        return 1.0, 0.0, "identity"
    elif quantity == "wind_gust":
        if key in {"km/h", "kmh", "kph", "kilometersperhour"}:
            return 1.0, 0.0, "identity"
        if key in {"m/s", "mps", "meterspersecond"}:
            return 3.6, 0.0, "m/s_to_km/h"
    return None


def _valid_range(quantity: Quantity, value: float) -> bool:
    if quantity == "temperature":
        return -40.0 <= value <= 60.0
    if quantity == "relative_humidity":
        return 0.0 <= value <= 100.0
    if quantity == "pressure":
        return value > 0
    return value >= 0


def normalize_entity_state(
    name: str,
    quantity: Quantity,
    raw: EntityStateInput,
) -> NormalizedMeasurement:
    """Convert one HA-like input into the agreed canonical unit.

    Unit mismatch is never guessed from magnitude.  This is especially
    important for pressure: ``1013`` is intentionally rejected without a
    declared ``hPa``/``mbar`` unit instead of being silently treated as Pa.
    """
    errors: list[str] = []
    number = _number(raw.state)
    conversion = _converter(quantity, raw.unit_of_measurement)
    canonical_value: float | None = None
    conversion_name: str | None = None
    if number is None:
        errors.append(f"{name}_invalid_value")
    if conversion is None:
        errors.append(f"{name}_invalid_unit")
    if number is not None and conversion is not None:
        multiplier, offset, conversion_name = conversion
        candidate = number * multiplier + offset
        if not _valid_range(quantity, candidate):
            errors.append(f"{name}_invalid_range")
        else:
            canonical_value = candidate
    observed_at = _utc(raw.last_updated)
    if raw.last_updated is not None and observed_at is None:
        errors.append(f"{name}_invalid_timestamp")
    provenance = SourceProvenance(
        entity_id=raw.entity_id,
        source_class=raw.source_class,
        observed_at=observed_at,
        original_unit=raw.unit_of_measurement,
        canonical_unit=CANONICAL_UNITS[quantity],
        conversion=conversion_name,
        value=canonical_value,
        error_codes=tuple(errors),
    )
    return NormalizedMeasurement(
        name=name,
        quantity=quantity,
        measurement=Measurement(
            value=canonical_value,
            observed_at=observed_at,
            source_class=raw.source_class,
            source_entity=raw.entity_id,
        ),
        provenance=provenance,
    )


def source_freshness(
    raw: EntityStateInput,
    *,
    now: datetime,
    policy: FreshnessPolicy = FreshnessPolicy(),
) -> SourceFreshness:
    """Evaluate source freshness with the common S01 local/model limits."""
    current = _utc(now)
    if current is None:
        raise ValueError("now must be timezone-aware")
    observed_at = _utc(raw.last_updated)
    errors: list[str] = []
    if raw.source_class == LOCAL_OBSERVATION:
        max_age = policy.local_max_age
    elif raw.source_class == MODEL_FALLBACK:
        max_age = policy.model_max_age
    else:
        max_age = None
        errors.append("unsupported_source_class")
    if observed_at is None:
        errors.append("missing_timestamp")
        return SourceFreshness(False, None, tuple(errors))
    age = (current - observed_at).total_seconds()
    if age < 0:
        errors.append("future_timestamp")
    elif max_age is not None and age > max_age.total_seconds():
        errors.append("stale")
    return SourceFreshness(not errors, age, tuple(errors))


def shared_station_health(
    temperature: NormalizedMeasurement,
    relative_humidity: NormalizedMeasurement,
    *,
    now: datetime,
    policy: FreshnessPolicy = FreshnessPolicy(),
) -> StationHealth:
    """Validate the shared local T/RH pair used to corroborate rain zero."""
    temp_freshness = source_freshness(
        EntityStateInput(
            temperature.provenance.entity_id,
            temperature.measurement.value,
            temperature.provenance.original_unit,
            temperature.measurement.observed_at,
            temperature.provenance.source_class,
        ),
        now=now,
        policy=policy,
    )
    humidity_freshness = source_freshness(
        EntityStateInput(
            relative_humidity.provenance.entity_id,
            relative_humidity.measurement.value,
            relative_humidity.provenance.original_unit,
            relative_humidity.measurement.observed_at,
            relative_humidity.provenance.source_class,
        ),
        now=now,
        policy=policy,
    )
    reasons: list[str] = []
    if temperature.provenance.source_class != LOCAL_OBSERVATION:
        reasons.append("station_temperature_not_local")
    if relative_humidity.provenance.source_class != LOCAL_OBSERVATION:
        reasons.append("station_humidity_not_local")
    if temperature.provenance.error_codes:
        reasons.extend(f"station_{code}" for code in temperature.provenance.error_codes)
    if relative_humidity.provenance.error_codes:
        reasons.extend(f"station_{code}" for code in relative_humidity.provenance.error_codes)
    reasons.extend(f"station_temperature_{code}" for code in temp_freshness.error_codes)
    reasons.extend(f"station_humidity_{code}" for code in humidity_freshness.error_codes)
    temp_at = temperature.measurement.observed_at
    humidity_at = relative_humidity.measurement.observed_at
    skew: float | None = None
    if temp_at is not None and humidity_at is not None:
        skew = abs((temp_at - humidity_at).total_seconds())
        if skew > policy.max_temperature_humidity_skew.total_seconds():
            reasons.append("station_temperature_humidity_skew")
    # A model source can be fresh enough for a potential, but not for proving
    # that a physical local rain sensor has reported a stable dry state.
    fresh = (
        temperature.provenance.source_class == LOCAL_OBSERVATION
        and relative_humidity.provenance.source_class == LOCAL_OBSERVATION
        and temp_freshness.fresh
        and humidity_freshness.fresh
        and not temperature.provenance.error_codes
        and not relative_humidity.provenance.error_codes
        and (skew is None or skew <= policy.max_temperature_humidity_skew.total_seconds())
    )
    return StationHealth(
        fresh=fresh,
        temperature_age_seconds=temp_freshness.age_seconds,
        humidity_age_seconds=humidity_freshness.age_seconds,
        temperature_humidity_skew_seconds=skew,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _safe_weather_value(
    normalized: NormalizedMeasurement,
    raw: EntityStateInput,
    *,
    now: datetime,
    policy: FreshnessPolicy,
    require_local: bool = True,
    allow_stale_zero_with_station: bool = False,
) -> tuple[float | None, tuple[str, ...]]:
    """Return a safety input or explicit reason codes; never synthesize data."""
    reasons = list(normalized.provenance.error_codes)
    if require_local and raw.source_class != LOCAL_OBSERVATION:
        reasons.append(f"{normalized.name}_not_local")
    freshness = source_freshness(raw, now=now, policy=policy)
    # GW1100A often leaves a true zero rate unchanged.  Fresh outdoor T/RH
    # corroboration is checked by WeatherSafetyInputs.shared_station_fresh;
    # preserve this zero even when its individual state timestamp is old.
    zero_exception = (
        allow_stale_zero_with_station
        and normalized.value == 0.0
        and freshness.error_codes == ("stale",)
        and raw.source_class == LOCAL_OBSERVATION
    )
    if not freshness.fresh and not zero_exception:
        reasons.extend(f"{normalized.name}_{code}" for code in freshness.error_codes)
    if reasons:
        return None, tuple(dict.fromkeys(reasons))
    return normalized.value, ()


class VentilationZoneRuntime:
    """Stateful adapter for a single zone's S01/S07 local evaluation.

    The embedded advisory owns its weather and 15-minute stabilization state.
    One instance must therefore be kept for the lifecycle of exactly one HA
    config subentry/zone; it must not be shared across rooms.
    """

    def __init__(
        self,
        zone_id: str,
        zone_name: str,
        *,
        freshness_policy: FreshnessPolicy = FreshnessPolicy(),
        advisory: VentilationAdvisory | None = None,
    ) -> None:
        if not zone_id:
            raise ValueError("zone_id must not be empty")
        self.zone_id = zone_id
        self.zone_name = zone_name or zone_id
        self._freshness_policy = freshness_policy
        self._advisory = advisory or VentilationAdvisory()

    def evaluate(self, inputs: ZoneRuntimeInputs, *, now: datetime) -> VentilationRuntimeSnapshot:
        """Normalize sources, calculate S01 potential and apply S07 safely."""
        evaluated_at = _utc(now)
        if evaluated_at is None:
            raise ValueError("now must be timezone-aware")
        if inputs.zone_id != self.zone_id:
            raise ValueError("runtime zone_id does not match inputs.zone_id")

        normalized = {
            "indoor_temperature": normalize_entity_state(
                "indoor_temperature", "temperature", inputs.indoor_temperature
            ),
            "indoor_relative_humidity": normalize_entity_state(
                "indoor_relative_humidity", "relative_humidity", inputs.indoor_relative_humidity
            ),
            "outdoor_temperature": normalize_entity_state(
                "outdoor_temperature", "temperature", inputs.outdoor.temperature
            ),
            "outdoor_relative_humidity": normalize_entity_state(
                "outdoor_relative_humidity", "relative_humidity", inputs.outdoor.relative_humidity
            ),
            "pressure": normalize_entity_state("pressure", "pressure", inputs.outdoor.pressure),
            "rain_rate": normalize_entity_state("rain_rate", "rain_rate", inputs.outdoor.rain_rate),
            "wind_gust": normalize_entity_state("wind_gust", "wind_gust", inputs.outdoor.wind_gust),
        }
        if inputs.outdoor.rain_counter is not None:
            normalized["rain_counter"] = normalize_entity_state(
                "rain_counter", "rain_counter", inputs.outdoor.rain_counter
            )

        station = shared_station_health(
            normalized["outdoor_temperature"],
            normalized["outdoor_relative_humidity"],
            now=evaluated_at,
            policy=self._freshness_policy,
        )
        potential = evaluate_ventilation_potential(
            VentilationInputs(
                indoor_temperature=normalized["indoor_temperature"].measurement,
                indoor_relative_humidity=normalized["indoor_relative_humidity"].measurement,
                outdoor_temperature=normalized["outdoor_temperature"].measurement,
                outdoor_relative_humidity=normalized["outdoor_relative_humidity"].measurement,
                pressure=normalized["pressure"].measurement,
            ),
            now=evaluated_at,
            policy=self._freshness_policy,
        )

        rain_rate, rain_errors = _safe_weather_value(
            normalized["rain_rate"],
            inputs.outdoor.rain_rate,
            now=evaluated_at,
            policy=self._freshness_policy,
            allow_stale_zero_with_station=True,
        )
        gust, gust_errors = _safe_weather_value(
            normalized["wind_gust"],
            inputs.outdoor.wind_gust,
            now=evaluated_at,
            policy=self._freshness_policy,
        )
        counter: float | None = None
        counter_errors: tuple[str, ...] = ()
        if inputs.outdoor.rain_counter is not None:
            counter, counter_errors = _safe_weather_value(
                normalized["rain_counter"],
                inputs.outdoor.rain_counter,
                now=evaluated_at,
                policy=self._freshness_policy,
            )
        weather = WeatherSafetyInputs(
            shared_station_fresh=station.fresh,
            rain_rate_mm_per_h=rain_rate,
            rain_counter_mm=counter,
            gust_kmh=gust,
        )
        decision = self._advisory.evaluate(potential, weather, now=evaluated_at)

        errors: list[str] = []
        for item in normalized.values():
            errors.extend(item.provenance.error_codes)
        errors.extend(station.reason_codes)
        errors.extend(rain_errors)
        errors.extend(gust_errors)
        errors.extend(counter_errors)
        errors.extend(potential.quality_flags)
        errors.extend(decision.reason_codes)
        if decision.weather is not None:
            errors.extend(decision.weather.reason_codes)
        return VentilationRuntimeSnapshot(
            zone_id=self.zone_id,
            zone_name=self.zone_name,
            evaluated_at=evaluated_at,
            potential=potential,
            decision=decision,
            station_health=station,
            provenance={name: item.provenance for name, item in normalized.items()},
            error_codes=tuple(dict.fromkeys(errors)),
        )
