"""Native soil-series contracts and the provisional source selector.

This module deliberately does not create root-zone values.  A soil value is
only useful when its provider variable and native point/layer depth remain
visible.  ``best_available`` is a convenience view over compatible native
series, never an interpolation or a replacement for a missing layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Literal


Availability = Literal["available", "unavailable"]
DepthSemantics = Literal["point", "layer"]

ICON_2I_MODEL_KEY = "italia_meteo_arpae_icon_2i"
ICON_EU_MODEL_KEY = "icon_eu"
BEST_AVAILABLE_ALGORITHM_VERSION = "native-soil-best-available-v1"


def _require_utc(value: datetime, name: str) -> None:
    """Reject naive and non-UTC timestamps at the data-contract boundary."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC datetime")


def _utc_iso(value: datetime) -> str:
    _require_utc(value, "datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class NativeSoilDepth:
    """A provider's unmodified depth coordinate.

    Point depths must not be used as layers and a layer's bounds must not be
    silently collapsed into a representative point.
    """

    semantics: DepthSemantics
    point_depth_cm: float | None = None
    layer_top_cm: float | None = None
    layer_bottom_cm: float | None = None

    def __post_init__(self) -> None:
        if self.semantics == "point":
            if self.point_depth_cm is None or not isfinite(self.point_depth_cm) or self.point_depth_cm < 0:
                raise ValueError("point depth must be a finite non-negative value")
            if self.layer_top_cm is not None or self.layer_bottom_cm is not None:
                raise ValueError("point depth cannot also define layer bounds")
            return
        if self.semantics == "layer":
            if (
                self.layer_top_cm is None
                or self.layer_bottom_cm is None
                or not isfinite(self.layer_top_cm)
                or not isfinite(self.layer_bottom_cm)
                or self.layer_top_cm < 0
                or self.layer_top_cm >= self.layer_bottom_cm
            ):
                raise ValueError("layer bounds must be finite, non-negative, and increasing")
            if self.point_depth_cm is not None:
                raise ValueError("layer depth cannot also define a point")
            return
        raise ValueError(f"unsupported depth semantics: {self.semantics}")

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "depth_semantics": self.semantics,
            "point_depth_cm": self.point_depth_cm,
            "layer_top_cm": self.layer_top_cm,
            "layer_bottom_cm": self.layer_bottom_cm,
        }


@dataclass(frozen=True)
class NativeSoilSeries:
    """Identity and immutable metadata for one unharmonized provider series."""

    model_key: str
    source_variable: str
    quantity: str
    unit: str
    depth: NativeSoilDepth
    interval_semantics: str

    def __post_init__(self) -> None:
        for field_name in ("model_key", "source_variable", "quantity", "unit", "interval_semantics"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")

    def is_compatible_with(self, other: "NativeSoilSeries") -> bool:
        """Return true only for interchangeable native quantity/depth semantics."""
        return (
            self.quantity == other.quantity
            and self.unit == other.unit
            and self.depth == other.depth
            and self.interval_semantics == other.interval_semantics
        )

    def as_dict(self) -> dict[str, str | float | None]:
        return {
            "model_key": self.model_key,
            "source_variable": self.source_variable,
            "quantity": self.quantity,
            "unit": self.unit,
            "interval_semantics": self.interval_semantics,
            **self.depth.as_dict(),
        }


@dataclass(frozen=True)
class NativeSoilObservation:
    """One native value and its point-level provenance.

    ``value=None`` represents an explicitly unavailable provider value.  It
    can never be converted to 0 by this contract.
    """

    series: NativeSoilSeries
    valid_from_utc: datetime
    valid_to_utc: datetime
    model_run_at_utc: datetime
    retrieved_at_utc: datetime
    value: float | None
    availability: Availability
    coverage_ratio: float
    quality_flags: int = 0
    unavailable_reason: str | None = None
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "valid_from_utc",
            "valid_to_utc",
            "model_run_at_utc",
            "retrieved_at_utc",
        ):
            _require_utc(getattr(self, field_name), field_name)
        if self.valid_from_utc >= self.valid_to_utc:
            raise ValueError("valid UTC interval must have a positive duration")
        if self.model_run_at_utc > self.retrieved_at_utc:
            raise ValueError("model_run_at_utc must not be after retrieved_at_utc")
        if not 0.0 <= self.coverage_ratio <= 1.0:
            raise ValueError("coverage_ratio must be between 0 and 1")
        if self.quality_flags < 0:
            raise ValueError("quality_flags must not be negative")
        if self.availability == "available":
            if self.value is None or not isfinite(self.value):
                raise ValueError("available observations require a finite value")
            if self.unavailable_reason is not None:
                raise ValueError("available observations cannot have an unavailable reason")
        elif self.availability == "unavailable":
            if self.value is not None:
                raise ValueError("unavailable observations must use value=None, never a substitute")
            if not self.unavailable_reason or not self.unavailable_reason.strip():
                raise ValueError("unavailable observations require an explicit reason")
        else:
            raise ValueError(f"unsupported availability: {self.availability}")

    def as_dict(self) -> dict[str, object]:
        return {
            **self.series.as_dict(),
            "valid_from_utc": _utc_iso(self.valid_from_utc),
            "valid_to_utc": _utc_iso(self.valid_to_utc),
            "model_run_at_utc": _utc_iso(self.model_run_at_utc),
            "retrieved_at_utc": _utc_iso(self.retrieved_at_utc),
            "value": self.value,
            "availability": self.availability,
            "coverage_ratio": self.coverage_ratio,
            "quality_flags": self.quality_flags,
            "unavailable_reason": self.unavailable_reason,
            "provider_request_id": self.provider_request_id,
        }


@dataclass(frozen=True)
class SoilSelectionPolicy:
    """Acceptance thresholds supplied by the deployment, not guessed here."""

    maximum_age: timedelta
    minimum_coverage_ratio: float
    accepted_quality_flags: frozenset[int] = field(default_factory=lambda: frozenset({0}))

    def __post_init__(self) -> None:
        if self.maximum_age < timedelta(0):
            raise ValueError("maximum_age must not be negative")
        if not 0.0 <= self.minimum_coverage_ratio <= 1.0:
            raise ValueError("minimum_coverage_ratio must be between 0 and 1")
        if not self.accepted_quality_flags or any(flag < 0 for flag in self.accepted_quality_flags):
            raise ValueError("accepted_quality_flags must contain non-negative flags")


@dataclass(frozen=True)
class BestAvailableSoilPoint:
    """Result for one requested UTC interval, including durable provenance."""

    target: NativeSoilSeries
    valid_from_utc: datetime
    valid_to_utc: datetime
    value: float | None
    availability: Availability
    source_model_key: str | None
    source_variable: str | None
    source_depth: NativeSoilDepth | None
    source_model_run_at_utc: datetime | None
    source_retrieved_at_utc: datetime | None
    source_coverage_ratio: float | None
    source_quality_flags: int | None
    mapping_method: str
    fallback_reason: str | None
    algorithm_version: str = BEST_AVAILABLE_ALGORITHM_VERSION
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.valid_from_utc, "valid_from_utc")
        _require_utc(self.valid_to_utc, "valid_to_utc")
        if self.valid_from_utc >= self.valid_to_utc:
            raise ValueError("valid UTC interval must have a positive duration")
        if self.availability == "available":
            if self.value is None or not isfinite(self.value):
                raise ValueError("available best_available points require a finite value")
            required = (
                self.source_model_key,
                self.source_variable,
                self.source_depth,
                self.source_model_run_at_utc,
                self.source_retrieved_at_utc,
                self.source_coverage_ratio,
                self.source_quality_flags,
            )
            if any(item is None for item in required) or self.unavailable_reason is not None:
                raise ValueError("available best_available points require complete source provenance")
            _require_utc(self.source_model_run_at_utc, "source_model_run_at_utc")
            _require_utc(self.source_retrieved_at_utc, "source_retrieved_at_utc")
            if not 0.0 <= self.source_coverage_ratio <= 1.0 or self.source_quality_flags < 0:
                raise ValueError("available best_available points require valid source coverage and quality")
        elif self.availability == "unavailable":
            source_values = (
                self.source_model_key,
                self.source_variable,
                self.source_depth,
                self.source_model_run_at_utc,
                self.source_retrieved_at_utc,
                self.source_coverage_ratio,
                self.source_quality_flags,
            )
            if self.value is not None or any(item is not None for item in source_values):
                raise ValueError("unavailable best_available points cannot contain a substitute source or value")
            if not self.unavailable_reason:
                raise ValueError("unavailable best_available points require an explicit reason")
        else:
            raise ValueError(f"unsupported availability: {self.availability}")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "target": self.target.as_dict(),
            "valid_from_utc": _utc_iso(self.valid_from_utc),
            "valid_to_utc": _utc_iso(self.valid_to_utc),
            "value": self.value,
            "availability": self.availability,
            "source_model_key": self.source_model_key,
            "source_variable": self.source_variable,
            "source_model_run_at_utc": (
                _utc_iso(self.source_model_run_at_utc) if self.source_model_run_at_utc else None
            ),
            "source_retrieved_at_utc": (
                _utc_iso(self.source_retrieved_at_utc) if self.source_retrieved_at_utc else None
            ),
            "source_coverage_ratio": self.source_coverage_ratio,
            "source_quality_flags": self.source_quality_flags,
            "mapping_method": self.mapping_method,
            "fallback_reason": self.fallback_reason,
            "algorithm_version": self.algorithm_version,
            "unavailable_reason": self.unavailable_reason,
        }
        result.update(self.source_depth.as_dict() if self.source_depth else {
            "depth_semantics": None,
            "point_depth_cm": None,
            "layer_top_cm": None,
            "layer_bottom_cm": None,
        })
        return result


def _candidate_reason(
    observation: NativeSoilObservation | None,
    target: NativeSoilSeries,
    valid_from_utc: datetime,
    valid_to_utc: datetime,
    as_of_utc: datetime,
    policy: SoilSelectionPolicy,
) -> str | None:
    if observation is None:
        return "missing"
    if observation.valid_from_utc != valid_from_utc or observation.valid_to_utc != valid_to_utc:
        return "different_validity_interval"
    if not observation.series.is_compatible_with(target):
        return "incompatible_quantity_or_depth_semantics"
    if observation.availability != "available":
        return f"unavailable:{observation.unavailable_reason}"
    if observation.retrieved_at_utc > as_of_utc:
        return "retrieved_in_future"
    if as_of_utc - observation.retrieved_at_utc > policy.maximum_age:
        return "stale"
    if observation.coverage_ratio < policy.minimum_coverage_ratio:
        return "insufficient_coverage"
    if observation.quality_flags not in policy.accepted_quality_flags:
        return "quality_rejected"
    return None


def _evaluate_model_candidates(
    observations: list[NativeSoilObservation],
    model_key: str,
    target: NativeSoilSeries,
    valid_from_utc: datetime,
    valid_to_utc: datetime,
    as_of_utc: datetime,
    policy: SoilSelectionPolicy,
) -> tuple[NativeSoilObservation | None, str | None]:
    candidates = [item for item in observations if item.series.model_key == model_key]
    if not candidates:
        return None, "missing"
    compatible = [item for item in candidates if item.series.is_compatible_with(target)]
    if not compatible:
        return None, "incompatible_quantity_or_depth_semantics"
    same_interval = [
        item for item in compatible
        if item.valid_from_utc == valid_from_utc and item.valid_to_utc == valid_to_utc
    ]
    if not same_interval:
        return None, "different_validity_interval"
    evaluated = [
        (item, _candidate_reason(item, target, valid_from_utc, valid_to_utc, as_of_utc, policy))
        for item in same_interval
    ]
    eligible = [item for item, reason in evaluated if reason is None]
    if eligible:
        return max(eligible, key=lambda item: (item.retrieved_at_utc, item.model_run_at_utc)), None
    rejected, reason = max(
        evaluated,
        key=lambda pair: (pair[0].retrieved_at_utc, pair[0].model_run_at_utc),
    )
    return rejected, reason


def select_best_available(
    *,
    target: NativeSoilSeries,
    valid_from_utc: datetime,
    valid_to_utc: datetime,
    observations: list[NativeSoilObservation],
    as_of_utc: datetime,
    policy: SoilSelectionPolicy,
) -> BestAvailableSoilPoint:
    """Select ICON-2I, otherwise ICON-EU, without mixing native layers.

    The function has no implicit freshness or coverage defaults.  A caller
    must provide the policy used for the persisted result, making the current
    provisional threshold auditable and replaceable after the S04 comparison.
    """
    _require_utc(valid_from_utc, "valid_from_utc")
    _require_utc(valid_to_utc, "valid_to_utc")
    _require_utc(as_of_utc, "as_of_utc")
    if valid_from_utc >= valid_to_utc:
        raise ValueError("valid UTC interval must have a positive duration")

    evaluations: dict[str, tuple[NativeSoilObservation | None, str | None]] = {}
    for model_key in (ICON_2I_MODEL_KEY, ICON_EU_MODEL_KEY):
        evaluations[model_key] = _evaluate_model_candidates(
            observations,
            model_key,
            target,
            valid_from_utc,
            valid_to_utc,
            as_of_utc,
            policy,
        )

    primary, primary_reason = evaluations[ICON_2I_MODEL_KEY]
    fallback, fallback_reason = evaluations[ICON_EU_MODEL_KEY]
    selected: NativeSoilObservation | None = None
    selected_fallback_reason: str | None = None
    if primary is not None and primary_reason is None:
        selected = primary
    elif fallback is not None and fallback_reason is None:
        selected = fallback
        selected_fallback_reason = f"icon_2i:{primary_reason or 'not_selected'}"

    if selected is None:
        reasons = (
            f"icon_2i:{primary_reason or 'not_selected'};"
            f"icon_eu:{fallback_reason or 'not_selected'}"
        )
        return BestAvailableSoilPoint(
            target=target,
            valid_from_utc=valid_from_utc,
            valid_to_utc=valid_to_utc,
            value=None,
            availability="unavailable",
            source_model_key=None,
            source_variable=None,
            source_depth=None,
            source_model_run_at_utc=None,
            source_retrieved_at_utc=None,
            source_coverage_ratio=None,
            source_quality_flags=None,
            mapping_method="no_mapping",
            fallback_reason=None,
            unavailable_reason=f"no_eligible_native_source:{reasons}",
        )

    return BestAvailableSoilPoint(
        target=target,
        valid_from_utc=valid_from_utc,
        valid_to_utc=valid_to_utc,
        value=selected.value,
        availability="available",
        source_model_key=selected.series.model_key,
        source_variable=selected.series.source_variable,
        source_depth=selected.series.depth,
        source_model_run_at_utc=selected.model_run_at_utc,
        source_retrieved_at_utc=selected.retrieved_at_utc,
        source_coverage_ratio=selected.coverage_ratio,
        source_quality_flags=selected.quality_flags,
        mapping_method="native_passthrough",
        fallback_reason=selected_fallback_reason,
    )
