"""Live Home Assistant adapter for vendor-GTI surface measurements.

The first production stage deliberately passes through a chosen, oriented
vendor GTI sensor.  The local S02 transposition remains a separately marked
parity experiment; this adapter does not silently replace the vendor value.
It turns the scalar W/m² input into physically partitioned incident W and
integrates only short, observed elapsed intervals into LTS kWh counters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_GTI_ENTITY, CONF_NAME, CONF_SITE, DOMAIN
from .facade_solar import IncidentSolarPower, incident_solar_power
from .solar import LocalGtiResult
from .surfaces import Surface, SurfaceValidationError


MODEL_KEY = "vendor_gti_incident_solar_v1"
HEARTBEAT = timedelta(minutes=5)
# Do not bridge a shutdown, a disabled source, or an HA time jump with an
# invented flat irradiance period.  The heartbeat normally keeps intervals at
# five minutes, so this only discards an uncertain gap.
MAX_INTEGRATION_GAP = timedelta(minutes=15)
STORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class SurfaceSnapshot:
    """One current surface result and cumulative, locally observed energy."""

    power: IncidentSolarPower | None
    source_entity: str
    source_updated_at: datetime | None
    source_value_w_m2: float | None
    energy_kwh: dict[str, float]
    integration_coverage_seconds: float
    missing_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.power is not None and self.power.total_w is not None


def _finite_state(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed >= 0 else None


class SurfaceContext:
    """Stateful scalar energy integration for one stable surface subentry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        self.hass = hass
        self.entry = entry
        self.subentry = subentry
        self.surface = Surface.from_mapping(subentry.data)
        self.source_entity = str(subentry.data.get(CONF_GTI_ENTITY, "")).strip()
        self.snapshot: SurfaceSnapshot | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._started = False
        self._lock = asyncio.Lock()
        self._energy = {key: 0.0 for key in ("total", "window", "opaque", "other_opening")}
        self._coverage_seconds = 0.0
        self._last_value_w_m2: float | None = None
        self._last_valid_at: datetime | None = None
        self._store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.surface_energy.{entry.entry_id}.{subentry.subentry_id}",
            private=True,
        )

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        if self._started:
            return
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            energy = stored.get("energy_kwh")
            if isinstance(energy, dict):
                for key in self._energy:
                    self._energy[key] = max(0.0, float(energy.get(key, 0.0)))
            self._coverage_seconds = max(0.0, float(stored.get("coverage_seconds", 0.0)))
            self._last_value_w_m2 = _finite_state(stored.get("last_value_w_m2"))
            raw_last_at = stored.get("last_valid_at")
            if isinstance(raw_last_at, str):
                try:
                    parsed = datetime.fromisoformat(raw_last_at.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        self._last_valid_at = parsed.astimezone(timezone.utc)
                except ValueError:
                    pass
        if self.source_entity:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, [self.source_entity], self._state_changed)
            )
        self._unsubscribers.append(
            async_track_time_interval(self.hass, self._heartbeat, HEARTBEAT)
        )
        self._started = True
        await self.async_evaluate(dt_util.utcnow())

    @callback
    def async_stop(self) -> None:
        while self._unsubscribers:
            self._unsubscribers.pop()()
        self._started = False

    @callback
    def _state_changed(self, _event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate(dt_util.utcnow()))

    @callback
    def _heartbeat(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_evaluate(now))

    def _source_value(self) -> tuple[float | None, datetime | None, str | None]:
        if not self.source_entity:
            return None, None, "gti_source_not_configured"
        state = self.hass.states.get(self.source_entity)
        if state is None:
            return None, None, "gti_source_not_found"
        value = _finite_state(state.state)
        if value is None:
            return None, state.last_updated, "gti_source_unavailable"
        unit = state.attributes.get("unit_of_measurement")
        if unit not in {"W/m²", "W/m2"}:
            return None, state.last_updated, "gti_source_unit_invalid"
        if state.attributes.get("state_class") != "measurement":
            return None, state.last_updated, "gti_source_state_class_invalid"
        return value, state.last_updated, None

    def _power(self, value_w_m2: float, now: datetime) -> IncidentSolarPower:
        # The selected vendor value is already GTI for this orientation.  Do
        # not overwrite it with local transposition before parity acceptance.
        gti = LocalGtiResult(
            surface_id=self.surface.surface_id,
            value_w_m2=value_w_m2,
            direct_w_m2=None,
            sky_diffuse_w_m2=None,
            ground_reflected_w_m2=None,
            quality="ok",
            missing_inputs=(),
            interval_start=now - HEARTBEAT,
            interval_end=now,
            geometry_at=None,
            model=MODEL_KEY,
            experimental=False,
        )
        return incident_solar_power(self.surface, gti)

    def _integrate_previous(self, now: datetime) -> None:
        if self._last_valid_at is None or self._last_value_w_m2 is None:
            return
        elapsed = now - self._last_valid_at
        if elapsed <= timedelta(0) or elapsed > MAX_INTEGRATION_GAP:
            return
        previous = self._power(self._last_value_w_m2, now)
        hours = elapsed.total_seconds() / 3600.0
        for key, field in (
            ("total", "total_w"),
            ("window", "window_w"),
            ("opaque", "opaque_w"),
            ("other_opening", "other_opening_w"),
        ):
            value = getattr(previous, field)
            if value is not None:
                self._energy[key] += value * hours / 1000.0
        self._coverage_seconds += elapsed.total_seconds()

    def _persist(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "energy_kwh": self._energy,
                "coverage_seconds": self._coverage_seconds,
                "last_value_w_m2": self._last_value_w_m2,
                "last_valid_at": self._last_valid_at.isoformat() if self._last_valid_at else None,
            },
            delay=30,
        )

    async def async_evaluate(self, now: datetime) -> None:
        async with self._lock:
            now = now.astimezone(timezone.utc)
            value, source_updated_at, error = self._source_value()
            self._integrate_previous(now)
            if value is None:
                self._last_value_w_m2 = None
                self._last_valid_at = None
                self.snapshot = SurfaceSnapshot(
                    power=None,
                    source_entity=self.source_entity,
                    source_updated_at=source_updated_at,
                    source_value_w_m2=None,
                    energy_kwh=dict(self._energy),
                    integration_coverage_seconds=self._coverage_seconds,
                    missing_reason=error,
                )
            else:
                power = self._power(value, now)
                self._last_value_w_m2 = value
                self._last_valid_at = now
                self.snapshot = SurfaceSnapshot(
                    power=power,
                    source_entity=self.source_entity,
                    source_updated_at=source_updated_at,
                    source_value_w_m2=value,
                    energy_kwh=dict(self._energy),
                    integration_coverage_seconds=self._coverage_seconds,
                )
            self._persist()
            for listener in tuple(self._listeners):
                listener()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface.surface_id,
            "surface_type": self.surface.surface_type.value,
            "source_entity": self.source_entity or None,
            "energy_kwh": dict(self._energy),
            "integration_coverage_seconds": self._coverage_seconds,
            "available": self.snapshot.available if self.snapshot else False,
            "missing_reason": self.snapshot.missing_reason if self.snapshot else None,
        }


class SurfaceCollectionContext:
    """One listener fan-out for facade, roof, and envelope aggregate entities."""

    def __init__(self, contexts: list[SurfaceContext]) -> None:
        self.contexts = contexts
        self._listeners: list[Callable[[], None]] = []
        for context in contexts:
            context.add_listener(self._notify)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def selected(self, scope: str) -> list[SurfaceContext]:
        if scope == "facades":
            return [item for item in self.contexts if item.surface.surface_type.value == "facade"]
        if scope == "roofs":
            return [item for item in self.contexts if item.surface.surface_type.value == "roof"]
        return list(self.contexts)
