"""LTS-safe entities for incident facade and roof solar radiation.

No entity in this module estimates absorbed radiation, transmitted solar gain,
or a heating/cooling load.  It publishes only unshaded incident GTI, scalar
plane power, and locally observed accumulated incident energy.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfEnergy, UnitOfIrradiance, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_SITE, DOMAIN
from .surface_runtime import MODEL_KEY, SurfaceCollectionContext, SurfaceContext


_POWER_FIELDS = {
    "total": "total_w",
    "window": "window_w",
    "opaque": "opaque_w",
    "other_opening": "other_opening_w",
}

_COMPONENT_NAMES = {
    "total": "Auftreffende Solarleistung ohne lokale Verschattungskorrektur",
    "window": "Auftreffende Solarleistung Fenster ohne lokale Verschattungskorrektur",
    "opaque": "Auftreffende Solarleistung opak ohne lokale Verschattungskorrektur",
    "other_opening": "Auftreffende Solarleistung sonstige Öffnungen ohne lokale Verschattungskorrektur",
}


class _SurfaceEntity(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, context: SurfaceContext, key: str, name: str) -> None:
        self.context = context
        self.key = key
        self._attr_name = name
        site = str(context.entry.data.get(CONF_SITE, "site"))
        self._attr_unique_id = f"{site}_{context.surface.surface_id}_surface_{key}"
        self._attr_suggested_object_id = (
            f"nhz_climate_{context.surface.surface_id}_{key}"
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.context.add_listener(self._update))
        await self.context.async_start()

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        site = str(self.context.entry.data.get(CONF_SITE, "site"))
        return DeviceInfo(
            identifiers={(DOMAIN, f"{site}_{self.context.surface.surface_id}")},
            name=f"NHZ Fläche {self.context.surface.name}",
            manufacturer="NHZ",
            model="Incident solar, unshaded (Arbeitswerte)",
            configuration_url="https://oc.nhz.de/docs",
        )

    @property
    def _common_attributes(self) -> dict[str, Any]:
        surface = self.context.surface
        snapshot = self.context.snapshot
        return {
            "surface_id": surface.surface_id,
            "surface_type": surface.surface_type.value,
            "tilt_deg": surface.tilt_deg,
            "azimuth_deg": surface.azimuth_deg,
            "gross_area_m2": surface.gross_area_m2,
            "window_area_m2": surface.window_area_m2,
            "other_opening_area_m2": surface.other_opening_area_m2,
            "opaque_area_m2": (
                surface.gross_area_m2
                - surface.window_area_m2
                - surface.other_opening_area_m2
            ),
            "model_key": MODEL_KEY,
            "model_boundary": "incident_solar_only",
            "local_shading_correction": "not_applied",
            "source_entity": snapshot.source_entity if snapshot else self.context.source_entity or None,
            "source_updated_at": (
                snapshot.source_updated_at.isoformat()
                if snapshot and snapshot.source_updated_at else None
            ),
            "quality": snapshot.power.quality if snapshot and snapshot.power else "unavailable",
            "missing_reason": snapshot.missing_reason if snapshot else "not_evaluated",
            "integration_coverage_seconds": (
                round(snapshot.integration_coverage_seconds, 1) if snapshot else 0.0
            ),
        }


class SurfaceGtiSensor(_SurfaceEntity):
    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_native_unit_of_measurement = UnitOfIrradiance.WATTS_PER_SQUARE_METER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, context: SurfaceContext) -> None:
        super().__init__(context, "gti_unshaded", "GTI ohne lokale Verschattungskorrektur")

    @property
    def available(self) -> bool:
        return bool(self.context.snapshot and self.context.snapshot.available)

    @property
    def native_value(self) -> float | None:
        return self.context.snapshot.source_value_w_m2 if self.context.snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._common_attributes, "quantity": "global_tilted_irradiance"}


class SurfacePowerSensor(_SurfaceEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, context: SurfaceContext, component: str) -> None:
        super().__init__(context, f"{component}_power_w", _COMPONENT_NAMES[component])
        self.component = component

    @property
    def available(self) -> bool:
        snapshot = self.context.snapshot
        return bool(snapshot and snapshot.power and getattr(snapshot.power, _POWER_FIELDS[self.component]) is not None)

    @property
    def native_value(self) -> float | None:
        snapshot = self.context.snapshot
        return getattr(snapshot.power, _POWER_FIELDS[self.component]) if snapshot and snapshot.power else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._common_attributes, "component": self.component}


class SurfaceEnergySensor(_SurfaceEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, context: SurfaceContext, component: str) -> None:
        energy_name = _COMPONENT_NAMES[component].replace(
            "Solarleistung", "Solarenergie"
        )
        super().__init__(context, f"{component}_energy_kwh", energy_name)
        self.component = component

    @property
    def available(self) -> bool:
        return bool(
            self.context.snapshot
            and self.context.snapshot.integration_coverage_seconds > 0
        )

    @property
    def native_value(self) -> float | None:
        snapshot = self.context.snapshot
        return snapshot.energy_kwh[self.component] if snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._common_attributes,
            "component": self.component,
            "integration_method": "previous_observed_vendor_gti_times_elapsed_utc",
            "coverage_note": "Energy starts when this integration is enabled; it is not backfilled.",
        }


class SurfaceAggregateSensor(SensorEntity):
    """Conservative aggregate over scalar surface powers or energies."""

    _attr_has_entity_name = True
    _attr_suggested_display_precision = 2

    def __init__(self, collection: SurfaceCollectionContext, scope: str, quantity: str) -> None:
        self.collection = collection
        self.scope = scope
        self.quantity = quantity
        label = {"facades": "Fassaden", "roofs": "Dächer", "envelope": "Gebäudehülle"}[scope]
        if quantity == "power":
            self._attr_name = f"{label} auftreffende Solarleistung ohne lokale Verschattungskorrektur"
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_name = f"{label} auftreffende Solarenergie ohne lokale Verschattungskorrektur"
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        site = (
            str(collection.contexts[0].entry.data.get(CONF_SITE, "site"))
            if collection.contexts else "site"
        )
        self._attr_unique_id = f"{site}_surface_{scope}_{quantity}"
        self._attr_suggested_object_id = f"nhz_climate_{scope}_incident_solar_{quantity}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.collection.add_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def _contexts(self) -> list[SurfaceContext]:
        return self.collection.selected(self.scope)

    def _available_values(self) -> list[float]:
        values: list[float] = []
        for context in self._contexts:
            snapshot = context.snapshot
            if not snapshot:
                continue
            if self.quantity == "power":
                if snapshot.power and snapshot.power.total_w is not None:
                    values.append(snapshot.power.total_w)
            elif snapshot.integration_coverage_seconds > 0:
                values.append(snapshot.energy_kwh["total"])
        return values

    @property
    def available(self) -> bool:
        return bool(self._available_values())

    @property
    def native_value(self) -> float | None:
        values = self._available_values()
        return sum(values) if values else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        contexts = self._contexts
        available = self._available_values()
        known = [item.surface.surface_id for item in contexts if item.snapshot and (
            item.snapshot.available if self.quantity == "power"
            else item.snapshot.integration_coverage_seconds > 0
        )]
        return {
            "scope": self.scope,
            "surface_ids": [item.surface.surface_id for item in contexts],
            "available_surface_ids": known,
            "missing_surface_ids": [item.surface.surface_id for item in contexts if item.surface.surface_id not in known],
            "coverage_fraction": len(known) / len(contexts) if contexts else 0.0,
            "quality": "ok" if len(known) == len(contexts) else "partial",
            "model_key": MODEL_KEY,
            "model_boundary": "incident_solar_only",
            "local_shading_correction": "not_applied",
            "aggregation": "sum_scalar_w" if self.quantity == "power" else "sum_surface_kwh",
            "value_count": len(available),
        }


def build_surface_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentries: Iterable[ConfigSubentry],
) -> tuple[SurfaceCollectionContext, list[tuple[ConfigSubentry, SurfaceContext, list[SensorEntity]]], list[SensorEntity]]:
    """Build per-surface entities plus facade/roof/envelope scalar summaries."""
    configured: list[tuple[ConfigSubentry, SurfaceContext, list[SensorEntity]]] = []
    contexts: list[SurfaceContext] = []
    for subentry in subentries:
        context = SurfaceContext(hass, entry, subentry)
        contexts.append(context)
        entities: list[SensorEntity] = [SurfaceGtiSensor(context), SurfacePowerSensor(context, "total"), SurfaceEnergySensor(context, "total")]
        if context.surface.surface_type.value == "facade":
            components = ("window", "opaque", "other_opening")
        else:
            components = ("opaque",)
        for component in components:
            entities.extend((SurfacePowerSensor(context, component), SurfaceEnergySensor(context, component)))
        configured.append((subentry, context, entities))
    collection = SurfaceCollectionContext(contexts)
    aggregates = [
        SurfaceAggregateSensor(collection, scope, quantity)
        for scope in ("facades", "roofs", "envelope")
        for quantity in ("power", "energy")
    ]
    return collection, configured, aggregates
