"""Home Assistant entities for the advisory-only ventilation pilot."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_INDOOR_HUMIDITY_ENTITY,
    CONF_INDOOR_TEMPERATURE_ENTITY,
    CONF_NAME,
    CONF_OUTDOOR_HUMIDITY_ENTITY,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PRESSURE_ENTITY,
    CONF_RAIN_COUNTER_ENTITY,
    CONF_RAIN_RATE_ENTITY,
    CONF_SITE,
    CONF_WIND_GUST_ENTITY,
    CONF_ZONE_ID,
    DOMAIN,
)
from .pilot_metrics import PilotMetrics
from .ventilation import LOCAL_OBSERVATION, MODEL_FALLBACK
from .ventilation_runtime import (
    EntityStateInput,
    OutdoorRuntimeInputs,
    VentilationRuntimeSnapshot,
    VentilationZoneRuntime,
    ZoneRuntimeInputs,
)

PILOT_HEARTBEAT = timedelta(minutes=5)
PILOT_STORE_VERSION = 1


def _configured(entry: ConfigEntry, key: str) -> str:
    """Read a configured entity id while retaining legacy option support."""
    return str(entry.options.get(key, entry.data.get(key, ""))).strip()


class VentilationZoneContext:
    """Share one stateful calculation and pilot accumulator across a zone."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.subentry = subentry
        self.zone_id = str(subentry.data[CONF_ZONE_ID])
        self.zone_name = str(subentry.data.get(CONF_NAME) or subentry.title or self.zone_id)
        self.runtime = VentilationZoneRuntime(self.zone_id, self.zone_name)
        self.metrics = PilotMetrics()
        self.snapshot: VentilationRuntimeSnapshot | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._debounce_unsub: Callable[[], None] | None = None
        self._started = False
        self._start_lock = asyncio.Lock()
        self._evaluation_lock = asyncio.Lock()
        self._store = Store(
            hass,
            PILOT_STORE_VERSION,
            f"{DOMAIN}.ventilation_pilot.{entry.entry_id}.{subentry.subentry_id}",
            private=True,
        )

    @property
    def source_entity_ids(self) -> tuple[str, ...]:
        """Return all source ids once, so zones add no provider requests."""
        ids = (
            str(self.subentry.data.get(CONF_INDOOR_TEMPERATURE_ENTITY, "")),
            str(self.subentry.data.get(CONF_INDOOR_HUMIDITY_ENTITY, "")),
            _configured(self.entry, CONF_OUTDOOR_TEMPERATURE_ENTITY),
            _configured(self.entry, CONF_OUTDOOR_HUMIDITY_ENTITY),
            _configured(self.entry, CONF_PRESSURE_ENTITY),
            _configured(self.entry, CONF_RAIN_RATE_ENTITY),
            _configured(self.entry, CONF_RAIN_COUNTER_ENTITY),
            _configured(self.entry, CONF_WIND_GUST_ENTITY),
        )
        return tuple(dict.fromkeys(entity_id for entity_id in ids if entity_id))

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def async_start(self) -> None:
        """Restore counters, start event listeners and establish a baseline."""
        async with self._start_lock:
            if self._started:
                return
            stored = await self._store.async_load()
            if isinstance(stored, dict):
                self.metrics = PilotMetrics.from_snapshot(stored)
            self._unsubscribers.append(
                async_track_state_change_event(
                    self.hass,
                    self.source_entity_ids,
                    self._state_changed,
                )
            )
            self._unsubscribers.append(
                async_track_time_interval(
                    self.hass,
                    self._heartbeat,
                    PILOT_HEARTBEAT,
                )
            )
            self._started = True
            await self.async_evaluate(dt_util.utcnow())

    @callback
    def async_stop(self) -> None:
        """Detach listeners; the Store writes its delayed snapshot on shutdown."""
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None
        while self._unsubscribers:
            self._unsubscribers.pop()()
        self._started = False

    @callback
    def _state_changed(self, _event: Event) -> None:
        # Temperature and humidity commonly arrive as two consecutive state
        # changes.  Coalescing them prevents a transient skew/unavailable point.
        if self._debounce_unsub is not None:
            self._debounce_unsub()
        self._debounce_unsub = async_call_later(self.hass, 1, self._debounced_evaluate)

    @callback
    def _debounced_evaluate(self, now: datetime) -> None:
        self._debounce_unsub = None
        self.hass.async_create_task(self.async_evaluate(now))

    @callback
    def _heartbeat(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_evaluate(now))

    def _state_input(
        self,
        entity_id: str,
        *,
        source_class: str = LOCAL_OBSERVATION,
    ) -> EntityStateInput:
        state = self.hass.states.get(entity_id) if entity_id else None
        return EntityStateInput(
            entity_id=entity_id,
            state=state.state if state is not None else None,
            unit_of_measurement=(
                state.attributes.get("unit_of_measurement") if state is not None else None
            ),
            last_updated=state.last_updated if state is not None else None,
            source_class=source_class,
        )

    def _inputs(self) -> ZoneRuntimeInputs:
        rain_counter_id = _configured(self.entry, CONF_RAIN_COUNTER_ENTITY)
        outdoor = OutdoorRuntimeInputs(
            temperature=self._state_input(
                _configured(self.entry, CONF_OUTDOOR_TEMPERATURE_ENTITY)
            ),
            relative_humidity=self._state_input(
                _configured(self.entry, CONF_OUTDOOR_HUMIDITY_ENTITY)
            ),
            # The agreed SL pressure is the Outdoor Environment model value.
            pressure=self._state_input(
                _configured(self.entry, CONF_PRESSURE_ENTITY),
                source_class=MODEL_FALLBACK,
            ),
            rain_rate=self._state_input(_configured(self.entry, CONF_RAIN_RATE_ENTITY)),
            rain_counter=(
                self._state_input(rain_counter_id) if rain_counter_id else None
            ),
            wind_gust=self._state_input(_configured(self.entry, CONF_WIND_GUST_ENTITY)),
        )
        return ZoneRuntimeInputs(
            zone_id=self.zone_id,
            zone_name=self.zone_name,
            indoor_temperature=self._state_input(
                str(self.subentry.data.get(CONF_INDOOR_TEMPERATURE_ENTITY, ""))
            ),
            indoor_relative_humidity=self._state_input(
                str(self.subentry.data.get(CONF_INDOOR_HUMIDITY_ENTITY, ""))
            ),
            outdoor=outdoor,
        )

    async def async_evaluate(self, now: datetime) -> None:
        """Evaluate once and publish one coherent snapshot to every entity."""
        async with self._evaluation_lock:
            self.snapshot = self.runtime.evaluate(self._inputs(), now=now)
            self.metrics.update(
                now,
                self.snapshot.decision.status,
                self.snapshot.decision.reason_codes,
            )
            self._store.async_delay_save(self.metrics.snapshot, delay=30)
            for listener in tuple(self._listeners):
                listener()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "subentry_id": self.subentry.subentry_id,
            "source_entity_ids": list(self.source_entity_ids),
            "current": self.snapshot.as_attributes() if self.snapshot else None,
            "pilot": self.metrics.snapshot(),
        }


class VentilationEntity(SensorEntity):
    """Base entity attached to one zone context."""

    _attr_has_entity_name = True

    def __init__(self, context: VentilationZoneContext, key: str, name: str) -> None:
        self.context = context
        self._attr_name = name
        self._attr_unique_id = (
            f"{context.entry.data.get(CONF_SITE, 'site')}_{context.zone_id}_ventilation_{key}"
        )
        self._attr_suggested_object_id = (
            f"nhz_climate_{context.zone_id}_ventilation_{key}"
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.context.add_listener(self._handle_context_update))
        await self.context.async_start()

    @callback
    def _handle_context_update(self) -> None:
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{self.context.entry.data.get(CONF_SITE, 'site')}_{self.context.zone_id}",
                )
            },
            name=f"NHZ Lüftung {self.context.zone_name}",
            manufacturer="NHZ",
            model="Ventilation advisory (Arbeitswerte)",
            configuration_url="https://oc.nhz.de/docs",
        )


class VentilationDecisionSensor(VentilationEntity):
    """Advisory only: this entity is deliberately not a control command."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ja", "ambivalent", "nein"]

    def __init__(self, context: VentilationZoneContext) -> None:
        super().__init__(context, "decision", "Fenster öffnen (Arbeitswert)")

    @property
    def available(self) -> bool:
        return bool(
            self.context.snapshot
            and self.context.snapshot.decision.status != "unavailable"
        )

    @property
    def native_value(self) -> str | None:
        return self.context.snapshot.decision.status if self.available else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.context.snapshot
        if snapshot is None:
            return {"advisory_only": True, "working_values": True}
        return {
            **snapshot.as_attributes(),
            "advisory_only": True,
            "working_values": True,
            "reference_airflow_m3_per_h": 1.0,
        }


POTENTIALS: dict[str, tuple[str, str, Callable[[Any], float]]] = {
    "delta_humidity_ratio": (
        "Absolute Feuchtedifferenz außen minus innen",
        "g/kg",
        lambda value: value.delta_humidity_ratio_g_per_kg,
    ),
    "delta_temperature": (
        "Temperaturdifferenz außen minus innen",
        "K",
        lambda value: value.delta_sensible_temperature_k,
    ),
    "delta_enthalpy": (
        "Enthalpiedifferenz außen minus innen",
        "kJ/kg",
        lambda value: value.delta_enthalpy_kj_per_kg,
    ),
    "reference_moisture_rate": (
        "Feuchtetransport bei 1 m³/h",
        "g/h",
        lambda value: value.reference_moisture_rate_g_per_h,
    ),
    "reference_sensible_power": (
        "Fühlbare Wärmeleistung bei 1 m³/h",
        UnitOfPower.WATT,
        lambda value: value.reference_sensible_power_w,
    ),
    "reference_enthalpy_power": (
        "Gesamtwärmeleistung bei 1 m³/h",
        UnitOfPower.WATT,
        lambda value: value.reference_enthalpy_power_w,
    ),
}


class VentilationPotentialSensor(VentilationEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, context: VentilationZoneContext, key: str) -> None:
        name, unit, value_fn = POTENTIALS[key]
        super().__init__(context, key, name)
        self._attr_native_unit_of_measurement = unit
        self._value_fn = value_fn

    @property
    def available(self) -> bool:
        return bool(
            self.context.snapshot
            and self.context.snapshot.potential.available
            and self.context.snapshot.potential.potential is not None
        )

    @property
    def native_value(self) -> float | None:
        if not self.available:
            return None
        assert self.context.snapshot is not None
        assert self.context.snapshot.potential.potential is not None
        return self._value_fn(self.context.snapshot.potential.potential)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.context.snapshot
        if snapshot is None:
            return {}
        return {
            "zone_id": snapshot.zone_id,
            "evaluated_at": snapshot.evaluated_at.isoformat(),
            "source_quality": snapshot.potential.source_quality,
            "quality_flags": list(snapshot.potential.quality_flags),
            "algorithm_version": snapshot.potential.algorithm_version,
            "sign_convention": "outdoor_minus_indoor",
            "reference_airflow_m3_per_h": 1.0,
        }


PILOT_TOTALS: dict[str, tuple[str, str]] = {
    "yes_hours": ("Pilotzeit Empfehlung ja", UnitOfTime.HOURS),
    "ambivalent_hours": ("Pilotzeit Empfehlung ambivalent", UnitOfTime.HOURS),
    "no_hours": ("Pilotzeit Empfehlung nein", UnitOfTime.HOURS),
    "unavailable_hours": ("Pilotzeit Daten nicht verfügbar", UnitOfTime.HOURS),
    "rain_lock_hours": ("Pilotzeit Regensperre", UnitOfTime.HOURS),
    "gust_lock_hours": ("Pilotzeit Böensperre", UnitOfTime.HOURS),
    "status_transitions": ("Pilot Zustandswechsel", "events"),
    "rain_events": ("Pilot Regenereignisse", "events"),
    "gust_events": ("Pilot Böensperrereignisse", "events"),
}


class VentilationPilotTotalSensor(VentilationEntity):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, context: VentilationZoneContext, key: str) -> None:
        name, unit = PILOT_TOTALS[key]
        super().__init__(context, f"pilot_{key}", name)
        self.key = key
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = 3 if unit == UnitOfTime.HOURS else 0
        if unit == UnitOfTime.HOURS:
            self._attr_device_class = SensorDeviceClass.DURATION

    @property
    def native_value(self) -> float | int:
        return getattr(self.context.metrics.totals, self.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "pilot_window": "30_days",
            "large_gaps_excluded": True,
            "max_continuous_gap_minutes": self.context.metrics.max_gap.total_seconds() / 60,
        }


def build_ventilation_zone_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
) -> tuple[VentilationZoneContext, list[SensorEntity]]:
    """Create all current and LTS-capable entities for one zone."""
    context = VentilationZoneContext(hass, entry, subentry)
    entities: list[SensorEntity] = [VentilationDecisionSensor(context)]
    entities.extend(VentilationPotentialSensor(context, key) for key in POTENTIALS)
    entities.extend(VentilationPilotTotalSensor(context, key) for key in PILOT_TOTALS)
    return context, entities
