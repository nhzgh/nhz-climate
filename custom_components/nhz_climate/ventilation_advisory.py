"""Conservative, advisory-only policy for ventilation potentials.

The module deliberately has no Home Assistant imports and has no actuator
interface.  It turns the versioned S01 psychrometric potential into an
explainable suggestion only after source-quality, weather-safety and temporal
stability gates have passed.  The numeric limits in this first version are
documented *Arbeitswerte* for the SL pilot, not comfort or safety guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Iterable

from .ventilation import VentilationEvaluation
from .ventilation_projection import DurationRecommendation


STATUS_YES = "ja"
STATUS_AMBIVALENT = "ambivalent"
STATUS_NO = "nein"
STATUS_OPTIONAL = "optional"
STATUS_UNAVAILABLE = "unavailable"
VALID_STATUSES = frozenset((STATUS_YES, STATUS_AMBIVALENT, STATUS_NO, STATUS_OPTIONAL))

EFFECT_BENEFIT = "benefit"
EFFECT_HARM = "harm"
EFFECT_NEUTRAL = "neutral"
EFFECT_AMBIVALENT = "ambivalent"
EFFECT_UNAVAILABLE = "unavailable"

HUMIDITY_IDEAL_PERCENT = 50.0
HUMIDITY_MIN_PERCENT = 40.0
HUMIDITY_MAX_PERCENT = 60.0
TEMPERATURE_IDEAL_C = 22.0
TEMPERATURE_MIN_C = 20.0
TEMPERATURE_MAX_C = 24.0
NEUTRAL_DELTA_W_G_PER_KG = 0.8
NEUTRAL_DELTA_T_K = 1.0
NEUTRAL_DELTA_H_KJ_PER_KG = 2.0

RAIN_DRYDOWN = timedelta(minutes=15)
GUST_CLEAR_DURATION = timedelta(minutes=15)
GUST_LOCK_KMH = 40.0
GUST_CLEAR_KMH = 35.0
MINIMUM_STABLE_DURATION = timedelta(minutes=15)


@dataclass(frozen=True)
class PartAssessment:
    """One explainable humidity or thermal assessment."""

    effect: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class WeatherSafetyInputs:
    """Local live safety sources; no forecast value can substitute these.

    ``shared_station_fresh`` represents the separately validated, common
    GW1100A health signal.  It specifically validates an unchanged ``0``
    rain-rate state.  Rain inputs are optional: when neither a usable rate nor
    a positive counter delta exists, the advisory continues without that
    interlock.  Every measured positive rate or counter delta still locks;
    ``rain_counter_mm`` is optional and only positive differences are rain.
    """

    shared_station_fresh: bool
    rain_rate_mm_per_h: float | None = None
    rain_counter_mm: float | None = None
    gust_kmh: float | None = None


@dataclass(frozen=True)
class WeatherSafetyResult:
    """Stateful safety result for one evaluation timestamp."""

    available: bool
    locked: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvisoryDecision:
    """The public advisory result.  It never contains a control command.

    When a confirmed recommendation exists, ``status`` and its assessments
    describe that recommendation.  While ``pending`` is true, the separately
    named ``candidate_*`` fields describe the unconfirmed replacement.  This
    keeps a usable, confirmed public recommendation visible without
    presenting a candidate's explanation as if it had already passed the
    stability gate.  Before the first confirmation, and for immediate safety
    or source-quality outcomes, ``status`` describes that direct result.
    """

    status: str
    reason_codes: tuple[str, ...]
    humidity: PartAssessment
    thermal: PartAssessment
    air_quality: str = "not_evaluated"
    hvac: str = "not_evaluated"
    pending: bool = False
    candidate_status: str | None = None
    candidate_reason_codes: tuple[str, ...] = ()
    candidate_started_at: datetime | None = None
    candidate_humidity: PartAssessment | None = None
    candidate_thermal: PartAssessment | None = None
    potential_available: bool = False
    weather: WeatherSafetyResult | None = None
    duration_recommendation: DurationRecommendation | None = None
    candidate_duration_recommendation: DurationRecommendation | None = None


@dataclass(frozen=True)
class ReplaySample:
    """One timestamped input for deterministic advisory replay.

    The optional ``expected_positive`` label is a human-reviewed reference,
    not something inferred from the advisory itself.  It permits transparent
    counts of false alarms and missed opportunities during the SL pilot.
    """

    at: datetime
    potential: VentilationEvaluation
    weather: WeatherSafetyInputs
    expected_positive: bool | None = None


@dataclass(frozen=True)
class ReplayMetrics:
    samples: int
    unavailable: int
    recommendations_yes: int
    recommendations_ambivalent: int
    recommendations_no: int
    recommendations_optional: int
    transitions: int
    labelled_samples: int
    true_positives: int
    false_alarms: int
    missed_opportunities: int


@dataclass(frozen=True)
class ReplayResult:
    decisions: tuple[AdvisoryDecision, ...]
    metrics: ReplayMetrics


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return at.astimezone(timezone.utc)


def evaluate_humidity(potential: VentilationEvaluation) -> PartAssessment:
    """Assess absolute-moisture benefit without chasing the 50 % ideal point.

    The current indoor relative humidity decides whether a moisture change is
    relevant.  Inside the 40--60 % acceptance corridor, no advisory is made
    just because outdoor air points toward the 50 % ideal.
    """
    if not potential.available or potential.potential is None:
        return PartAssessment(EFFECT_UNAVAILABLE, ("potential_unavailable",))
    indoor_rh = potential.potential.indoor.relative_humidity_percent
    delta_w = potential.potential.delta_humidity_ratio_g_per_kg
    if not (isfinite(indoor_rh) and isfinite(delta_w)):
        return PartAssessment(EFFECT_UNAVAILABLE, ("humidity_input_invalid",))
    if HUMIDITY_MIN_PERCENT <= indoor_rh <= HUMIDITY_MAX_PERCENT:
        return PartAssessment(EFFECT_NEUTRAL, ("humidity_within_acceptance_corridor",))
    if abs(delta_w) < NEUTRAL_DELTA_W_G_PER_KG:
        return PartAssessment(EFFECT_NEUTRAL, ("humidity_potential_neutral",))
    if indoor_rh > HUMIDITY_MAX_PERCENT:
        return (
            PartAssessment(EFFECT_BENEFIT, ("dehumidification_benefit",))
            if delta_w < 0
            else PartAssessment(EFFECT_HARM, ("humidification_harm",))
        )
    return (
        PartAssessment(EFFECT_BENEFIT, ("humidification_benefit",))
        if delta_w > 0
        else PartAssessment(EFFECT_HARM, ("dehumidification_harm",))
    )


def evaluate_thermal(potential: VentilationEvaluation) -> PartAssessment:
    """Assess sensible and total energy separately, without HVAC assumptions."""
    if not potential.available or potential.potential is None:
        return PartAssessment(EFFECT_UNAVAILABLE, ("potential_unavailable",))
    indoor_temperature = potential.potential.indoor.temperature_c
    delta_t = potential.potential.delta_sensible_temperature_k
    delta_h = potential.potential.delta_enthalpy_kj_per_kg
    if not all(isfinite(value) for value in (indoor_temperature, delta_t, delta_h)):
        return PartAssessment(EFFECT_UNAVAILABLE, ("thermal_input_invalid",))
    if TEMPERATURE_MIN_C <= indoor_temperature <= TEMPERATURE_MAX_C:
        return PartAssessment(EFFECT_NEUTRAL, ("temperature_within_acceptance_corridor",))

    sensible_direction = 0
    enthalpy_direction = 0
    if abs(delta_t) >= NEUTRAL_DELTA_T_K:
        sensible_direction = 1 if delta_t > 0 else -1
    if abs(delta_h) >= NEUTRAL_DELTA_H_KJ_PER_KG:
        enthalpy_direction = 1 if delta_h > 0 else -1
    if sensible_direction == 0 and enthalpy_direction == 0:
        return PartAssessment(EFFECT_NEUTRAL, ("thermal_potential_neutral",))

    desired_direction = -1 if indoor_temperature > TEMPERATURE_MAX_C else 1
    directions = {direction for direction in (sensible_direction, enthalpy_direction) if direction}
    if directions == {desired_direction}:
        return PartAssessment(
            EFFECT_BENEFIT,
            ("cooling_benefit",) if desired_direction < 0 else ("warming_benefit",),
        )
    if directions == {-desired_direction}:
        return PartAssessment(
            EFFECT_HARM,
            ("cooling_harm",) if desired_direction > 0 else ("warming_harm",),
        )
    return PartAssessment(EFFECT_AMBIVALENT, ("sensible_enthalpy_conflict",))


# Explicitly keep this as data rather than an implicit ordering of goals: S07
# must expose a humidity/thermal conflict rather than silently choosing one.
CONFLICT_MATRIX: dict[tuple[str, str], str] = {
    (EFFECT_BENEFIT, EFFECT_BENEFIT): STATUS_YES,
    (EFFECT_BENEFIT, EFFECT_NEUTRAL): STATUS_YES,
    (EFFECT_NEUTRAL, EFFECT_BENEFIT): STATUS_YES,
    (EFFECT_NEUTRAL, EFFECT_NEUTRAL): STATUS_OPTIONAL,
    (EFFECT_HARM, EFFECT_HARM): STATUS_NO,
    (EFFECT_HARM, EFFECT_NEUTRAL): STATUS_NO,
    (EFFECT_NEUTRAL, EFFECT_HARM): STATUS_NO,
    (EFFECT_BENEFIT, EFFECT_HARM): STATUS_AMBIVALENT,
    (EFFECT_HARM, EFFECT_BENEFIT): STATUS_AMBIVALENT,
    (EFFECT_AMBIVALENT, EFFECT_BENEFIT): STATUS_AMBIVALENT,
    (EFFECT_BENEFIT, EFFECT_AMBIVALENT): STATUS_AMBIVALENT,
    (EFFECT_AMBIVALENT, EFFECT_HARM): STATUS_AMBIVALENT,
    (EFFECT_HARM, EFFECT_AMBIVALENT): STATUS_AMBIVALENT,
    (EFFECT_AMBIVALENT, EFFECT_NEUTRAL): STATUS_AMBIVALENT,
    (EFFECT_NEUTRAL, EFFECT_AMBIVALENT): STATUS_AMBIVALENT,
    (EFFECT_AMBIVALENT, EFFECT_AMBIVALENT): STATUS_AMBIVALENT,
}


def combine_assessments(humidity: PartAssessment, thermal: PartAssessment) -> tuple[str, tuple[str, ...]]:
    """Apply the published S07 conflict matrix to two valid assessments."""
    if EFFECT_UNAVAILABLE in (humidity.effect, thermal.effect):
        return STATUS_UNAVAILABLE, ("potential_unavailable",)
    status = CONFLICT_MATRIX[(humidity.effect, thermal.effect)]
    reasons = (*humidity.reason_codes, *thermal.reason_codes)
    if status == STATUS_AMBIVALENT:
        reasons = (*reasons, "humidity_thermal_conflict")
    elif status == STATUS_OPTIONAL:
        reasons = (*reasons, "climate_neutral_ventilation_optional")
    elif status == STATUS_NO:
        reasons = (*reasons, "no_net_ventilation_benefit")
    else:
        reasons = (*reasons, "net_ventilation_benefit")
    return status, tuple(dict.fromkeys(reasons))


def refine_with_duration(
    base_status: str,
    base_reasons: tuple[str, ...],
    recommendation: DurationRecommendation | None,
) -> tuple[str, tuple[str, ...]]:
    """Map duration-aware target progress to legacy HA-compatible states.

    ``ja`` now means a time-bounded action exists: `short_airing`,
    `ventilate` and `overnight` all map to it for existing automations.  The
    action and exact duration are attributes.  `avoid` maps to `nein` and
    `optional` keeps its established meaning.  A missing duration model can
    never preserve a previously raw positive recommendation.
    """
    if recommendation is None:
        return base_status, base_reasons
    action = recommendation.action
    reasons = tuple(dict.fromkeys((*base_reasons, *recommendation.reason_codes)))
    if action == "unavailable":
        if base_status == STATUS_YES:
            return STATUS_AMBIVALENT, tuple(dict.fromkeys((*reasons, "duration_model_unavailable")))
        return base_status, reasons
    if action == "avoid":
        return STATUS_NO, tuple(dict.fromkeys((*reasons, "duration_target_avoid")))
    if action == "optional":
        return STATUS_OPTIONAL, tuple(dict.fromkeys((*reasons, "duration_target_optional")))
    if action in {"short_airing", "ventilate", "overnight"}:
        # The standard-room trajectory includes physical absolute-humidity
        # gatekeeping.  It is therefore the more specific, duration-aware
        # answer than the old instantaneous conflict matrix.
        return STATUS_YES, tuple(dict.fromkeys((*reasons, "duration_target_benefit")))
    return STATUS_AMBIVALENT, tuple(dict.fromkeys((*reasons, "duration_action_invalid")))


def _weather_limited_duration(
    recommendation: DurationRecommendation | None,
    weather: WeatherSafetyResult,
) -> DurationRecommendation | None:
    """Keep a displayed duration action consistent with safety precedence."""
    if recommendation is None:
        return None
    if weather.locked:
        return replace(
            recommendation,
            action="avoid",
            recommended_duration_minutes=None,
            limiting_factor="weather_safety",
            reason_codes=tuple(
                dict.fromkeys((*recommendation.reason_codes, *weather.reason_codes, "safety_lock"))
            ),
        )
    if not weather.available:
        return replace(
            recommendation,
            action="unavailable",
            recommended_duration_minutes=None,
            limiting_factor="weather_source_unavailable",
            reason_codes=tuple(
                dict.fromkeys((*recommendation.reason_codes, *weather.reason_codes))
            ),
        )
    return recommendation


class _WeatherGate:
    """Preserve rain/gust interlock history; it is internal policy state."""

    def __init__(self) -> None:
        self._last_rain_at: datetime | None = None
        self._previous_counter_mm: float | None = None
        self._gust_locked = False
        self._gust_clear_started_at: datetime | None = None

    def evaluate(self, weather: WeatherSafetyInputs, *, now: datetime) -> WeatherSafetyResult:
        now = _utc(now)
        rain_result = self._rain(weather, now)
        gust_result = self._gust(weather, now)
        if rain_result.locked or gust_result.locked:
            return WeatherSafetyResult(
                available=rain_result.available and gust_result.available,
                locked=True,
                reason_codes=tuple(dict.fromkeys((*rain_result.reason_codes, *gust_result.reason_codes))),
            )
        if not rain_result.available or not gust_result.available:
            return WeatherSafetyResult(
                available=False,
                locked=False,
                reason_codes=tuple(dict.fromkeys((*rain_result.reason_codes, *gust_result.reason_codes))),
            )
        return WeatherSafetyResult(available=True, locked=False)

    def _rain(self, weather: WeatherSafetyInputs, now: datetime) -> WeatherSafetyResult:
        rate = _finite(weather.rain_rate_mm_per_h)
        counter = _finite(weather.rain_counter_mm)
        if weather.rain_rate_mm_per_h is not None and rate is None:
            return WeatherSafetyResult(False, False, ("rain_rate_invalid",))
        if weather.rain_counter_mm is not None and (counter is None or counter < 0):
            return WeatherSafetyResult(False, False, ("rain_counter_invalid",))
        counter_event = False
        counter_reset = False
        if counter is not None:
            if self._previous_counter_mm is not None:
                if counter > self._previous_counter_mm:
                    counter_event = True
                elif counter < self._previous_counter_mm:
                    counter_reset = True
            self._previous_counter_mm = counter

        # Positive live rain or a positive counter delta is a safety lock even
        # if the common station health changes immediately afterwards.
        if (rate is not None and rate > 0) or counter_event:
            self._last_rain_at = now
            reasons = ["rain"]
            if counter_event:
                reasons.append("rain_counter_increase")
            return WeatherSafetyResult(True, True, tuple(reasons))

        # A negative cumulative delta is a reset, never a rain event.  A known
        # recent rain event keeps its drydown even if the optional rate source
        # becomes unavailable in the meantime.
        if counter_reset and rate is None:
            if self._last_rain_at is not None:
                elapsed = now - self._last_rain_at
                if elapsed < RAIN_DRYDOWN:
                    return WeatherSafetyResult(
                        True, True, ("rain_counter_reset", "rain_drydown")
                    )
                self._last_rain_at = None
            return WeatherSafetyResult(True, False)
        if rate is None:
            if self._last_rain_at is not None:
                elapsed = now - self._last_rain_at
                if elapsed < RAIN_DRYDOWN:
                    return WeatherSafetyResult(True, True, ("rain_drydown",))
                self._last_rain_at = None
            return WeatherSafetyResult(True, False)
        if rate < 0:
            return WeatherSafetyResult(False, False, ("rain_rate_negative",))
        if rate == 0 and not weather.shared_station_fresh:
            return WeatherSafetyResult(False, False, ("rain_station_stale",))
        assert rate == 0  # positive handled above; a valid zero proves dry.
        if self._last_rain_at is not None:
            elapsed = now - self._last_rain_at
            if elapsed < RAIN_DRYDOWN:
                return WeatherSafetyResult(True, True, ("rain_drydown",))
            self._last_rain_at = None
        return WeatherSafetyResult(True, False, ())

    def _gust(self, weather: WeatherSafetyInputs, now: datetime) -> WeatherSafetyResult:
        gust = _finite(weather.gust_kmh)
        if gust is None or gust < 0:
            # Retain a known safety lock; absence can never release it.
            if self._gust_locked:
                return WeatherSafetyResult(False, True, ("gust_source_unavailable", "strong_gusts"))
            return WeatherSafetyResult(False, False, ("gust_source_unavailable",))
        if gust >= GUST_LOCK_KMH:
            self._gust_locked = True
            self._gust_clear_started_at = None
            return WeatherSafetyResult(True, True, ("strong_gusts",))
        if not self._gust_locked:
            return WeatherSafetyResult(True, False, ())
        if gust < GUST_CLEAR_KMH:
            if self._gust_clear_started_at is None:
                self._gust_clear_started_at = now
            if now - self._gust_clear_started_at >= GUST_CLEAR_DURATION:
                self._gust_locked = False
                self._gust_clear_started_at = None
                return WeatherSafetyResult(True, False, ())
            return WeatherSafetyResult(True, True, ("gust_clear_pending",))
        # 35--40 km/h is not a new lock threshold, but it breaks the required
        # continuous below-35 period to clear an existing lock.
        self._gust_clear_started_at = None
        return WeatherSafetyResult(True, True, ("strong_gusts",))


@dataclass(frozen=True)
class _ConfirmedAssessment:
    """Private snapshot of the recommendation that passed the stability gate."""

    status: str
    reason_codes: tuple[str, ...]
    humidity: PartAssessment
    thermal: PartAssessment
    duration_recommendation: DurationRecommendation | None = None


class VentilationAdvisory:
    """Stateful S07 recommendation engine, intentionally advisory-only."""

    def __init__(self) -> None:
        self._weather = _WeatherGate()
        self._candidate_status: str | None = None
        self._candidate_reason_codes: tuple[str, ...] = ()
        self._candidate_started_at: datetime | None = None
        self._candidate_humidity: PartAssessment | None = None
        self._candidate_thermal: PartAssessment | None = None
        self._candidate_duration_recommendation: DurationRecommendation | None = None
        self._candidate_key: tuple[object, ...] | None = None
        self._confirmed: _ConfirmedAssessment | None = None

    def evaluate(
        self,
        potential: VentilationEvaluation,
        weather: WeatherSafetyInputs,
        *,
        now: datetime,
        duration_recommendation: DurationRecommendation | None = None,
    ) -> AdvisoryDecision:
        """Return an advisory; safety locks are immediate, all else is gated."""
        now = _utc(now)
        humidity = evaluate_humidity(potential)
        thermal = evaluate_thermal(potential)
        weather_result = self._weather.evaluate(weather, now=now)
        duration_recommendation = _weather_limited_duration(
            duration_recommendation, weather_result
        )
        raw_status, raw_reasons = combine_assessments(humidity, thermal)
        raw_status, raw_reasons = refine_with_duration(
            raw_status, raw_reasons, duration_recommendation
        )

        if weather_result.locked:
            # Safety locks take effect immediately and therefore become the
            # last effective recommendation.  Keep that confirmed ``no``
            # visible after the lock clears while the normal assessment earns
            # a fresh stability interval.
            safety = _ConfirmedAssessment(
                STATUS_NO,
                tuple(dict.fromkeys((*weather_result.reason_codes, "safety_lock"))),
                humidity,
                thermal,
                duration_recommendation,
            )
            self._confirmed = safety
            self._clear_candidate()
            return self._confirmed_decision(
                safety,
                potential_available=potential.available,
                weather=weather_result,
            )
        if raw_status == STATUS_UNAVAILABLE or not weather_result.available:
            self._reset_stabilization()
            reason = (
                raw_reasons
                if raw_status == STATUS_UNAVAILABLE
                else weather_result.reason_codes
            )
            return AdvisoryDecision(
                STATUS_UNAVAILABLE,
                tuple(dict.fromkeys(reason)),
                humidity,
                thermal,
                potential_available=potential.available,
                weather=weather_result,
                duration_recommendation=duration_recommendation,
            )

        return self._stabilize(
            raw_status,
            raw_reasons,
            humidity,
            thermal,
            potential,
            weather_result,
            now,
            duration_recommendation,
        )

    def _reset_stabilization(self) -> None:
        self._candidate_status = None
        self._candidate_reason_codes = ()
        self._candidate_started_at = None
        self._candidate_humidity = None
        self._candidate_thermal = None
        self._candidate_duration_recommendation = None
        self._candidate_key = None
        self._confirmed = None

    def _clear_candidate(self) -> None:
        self._candidate_status = None
        self._candidate_reason_codes = ()
        self._candidate_started_at = None
        self._candidate_humidity = None
        self._candidate_thermal = None
        self._candidate_duration_recommendation = None
        self._candidate_key = None

    @staticmethod
    def _recommendation_key(
        status: str, recommendation: DurationRecommendation | None
    ) -> tuple[object, ...]:
        if recommendation is None:
            return (status, None)
        return (
            status,
            recommendation.action,
            # Duration is rounded to this integration's 15-minute trajectory
            # grid. A changed duration is a changed recommendation and must
            # earn the same stability interval as a changed status/action.
            recommendation.recommended_duration_minutes,
        )

    def _confirmed_decision(
        self,
        confirmed: _ConfirmedAssessment,
        *,
        potential_available: bool,
        weather: WeatherSafetyResult,
        pending: bool = False,
    ) -> AdvisoryDecision:
        """Build a public result from the confirmed snapshot only.

        Candidate information is deliberately carried in its own fields.  In
        particular, do not replace the confirmed reason codes or assessments
        with their current candidate equivalents while the gate is pending.
        """
        return AdvisoryDecision(
            confirmed.status,
            confirmed.reason_codes,
            confirmed.humidity,
            confirmed.thermal,
            pending=pending,
            candidate_status=self._candidate_status if pending else None,
            candidate_reason_codes=self._candidate_reason_codes if pending else (),
            candidate_started_at=self._candidate_started_at if pending else None,
            candidate_humidity=self._candidate_humidity if pending else None,
            candidate_thermal=self._candidate_thermal if pending else None,
            potential_available=potential_available,
            weather=weather,
            duration_recommendation=confirmed.duration_recommendation,
            candidate_duration_recommendation=(
                self._candidate_duration_recommendation if pending else None
            ),
        )

    def _stabilize(
        self,
        raw_status: str,
        raw_reasons: tuple[str, ...],
        humidity: PartAssessment,
        thermal: PartAssessment,
        potential: VentilationEvaluation,
        weather: WeatherSafetyResult,
        now: datetime,
        duration_recommendation: DurationRecommendation | None,
    ) -> AdvisoryDecision:
        current_key = self._recommendation_key(raw_status, duration_recommendation)
        confirmed_key = (
            self._recommendation_key(
                self._confirmed.status, self._confirmed.duration_recommendation
            )
            if self._confirmed is not None
            else None
        )
        if self._confirmed is not None and confirmed_key == current_key:
            # A status which is already confirmed needs no new gate.  Refresh
            # its explanation and component assessments from the live inputs,
            # then cancel any incomplete replacement candidate.
            self._confirmed = _ConfirmedAssessment(
                raw_status, raw_reasons, humidity, thermal, duration_recommendation
            )
            self._clear_candidate()
            return self._confirmed_decision(
                self._confirmed,
                potential_available=potential.available,
                weather=weather,
            )
        if self._candidate_key != current_key:
            self._candidate_status = raw_status
            self._candidate_started_at = now
            self._candidate_key = current_key
        # Status stability is what the gate protects.  The candidate's
        # explanation remains live, but it is kept separate from the
        # confirmed explanation exposed as ``reason_codes``.
        self._candidate_reason_codes = raw_reasons
        self._candidate_humidity = humidity
        self._candidate_thermal = thermal
        self._candidate_duration_recommendation = duration_recommendation
        assert self._candidate_started_at is not None
        if now - self._candidate_started_at >= MINIMUM_STABLE_DURATION:
            self._confirmed = _ConfirmedAssessment(
                raw_status, raw_reasons, humidity, thermal, duration_recommendation
            )
            self._clear_candidate()
            return self._confirmed_decision(
                self._confirmed,
                potential_available=potential.available,
                weather=weather,
            )
        if self._confirmed is not None:
            return self._confirmed_decision(
                self._confirmed,
                potential_available=potential.available,
                weather=weather,
                pending=True,
            )
        return AdvisoryDecision(
            STATUS_UNAVAILABLE,
            ("awaiting_stable_assessment",),
            humidity,
            thermal,
            pending=True,
            candidate_status=self._candidate_status,
            candidate_reason_codes=self._candidate_reason_codes,
            candidate_started_at=self._candidate_started_at,
            candidate_humidity=self._candidate_humidity,
            candidate_thermal=self._candidate_thermal,
            potential_available=potential.available,
            weather=weather,
            duration_recommendation=None,
            candidate_duration_recommendation=self._candidate_duration_recommendation,
        )


def replay(samples: Iterable[ReplaySample]) -> ReplayResult:
    """Run a chronological deterministic replay and derive pilot metrics."""
    ordered = tuple(sorted(samples, key=lambda sample: _utc(sample.at)))
    engine = VentilationAdvisory()
    decisions: list[AdvisoryDecision] = []
    transitions = 0
    previous_status: str | None = None
    labels = true_positives = false_alarms = missed = 0
    for sample in ordered:
        decision = engine.evaluate(sample.potential, sample.weather, now=sample.at)
        decisions.append(decision)
        if previous_status is not None and decision.status != previous_status:
            transitions += 1
        previous_status = decision.status
        if sample.expected_positive is not None:
            labels += 1
            positive = decision.status == STATUS_YES
            if positive and sample.expected_positive:
                true_positives += 1
            elif positive:
                false_alarms += 1
            elif sample.expected_positive:
                missed += 1
    metrics = ReplayMetrics(
        samples=len(decisions),
        unavailable=sum(item.status == STATUS_UNAVAILABLE for item in decisions),
        recommendations_yes=sum(item.status == STATUS_YES for item in decisions),
        recommendations_ambivalent=sum(item.status == STATUS_AMBIVALENT for item in decisions),
        recommendations_no=sum(item.status == STATUS_NO for item in decisions),
        recommendations_optional=sum(item.status == STATUS_OPTIONAL for item in decisions),
        transitions=transitions,
        labelled_samples=labels,
        true_positives=true_positives,
        false_alarms=false_alarms,
        missed_opportunities=missed,
    )
    return ReplayResult(tuple(decisions), metrics)
