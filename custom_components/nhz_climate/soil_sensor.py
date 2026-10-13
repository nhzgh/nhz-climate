"""Compact Home Assistant entities for fixed-policy native soil snapshots."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NhzClimateCoordinator
from .soil import FIXED_ZONE_SOURCE_POLICY, SOIL_ZONES, SoilZone

ZONE_NAMES = {
    "upper_root_zone": "Oberer Wurzelraum",
    "main_root_zone": "Hauptwurzelraum",
    "deep_root_zone": "Tiefer Wurzelraum",
}
QUANTITY_NAMES = {
    "soil_temperature": "Bodentemperatur",
    "soil_moisture": "Bodenfeuchte",
}


def _zone_payload(coordinator: NhzClimateCoordinator, zone: SoilZone) -> dict[str, Any]:
    soil = coordinator.data.get("soil", {}) if coordinator.data else {}
    for item in soil.get("zones", []):
        if isinstance(item, dict) and item.get("zone") == zone:
            return item
    configured = FIXED_ZONE_SOURCE_POLICY.get(coordinator.site.lower(), {}).get(zone)
    return {
        "zone": zone,
        "availability": "unavailable",
        "source_model_key": None,
        "configured_source_model_key": configured,
        "selection_reason": "fixed_site_zone_policy",
        "unavailable_reason": (
            "source_not_available_by_policy"
            if configured is None and coordinator.site.lower() == "h240" and zone == "deep_root_zone"
            else "backend_soil_product_unavailable"
        ),
        "values": {},
    }


def _compact_attributes(zone: dict[str, Any], point: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return scalar provenance only; raw hourly arrays belong in the backend."""
    attributes = {
        key: zone.get(key)
        for key in (
            "zone", "availability", "source_model_key", "configured_source_model_key",
            "source_model_run_at_utc", "source_retrieved_at_utc", "valid_from_utc",
            "valid_to_utc", "selection_reason", "algorithm_version",
            "unavailable_reason",
        )
        if zone.get(key) is not None
    }
    if point:
        attributes.update({
            key: point.get(key)
            for key in (
                "quantity", "source_variable", "interval_semantics", "depth_semantics",
                "point_depth_cm", "layer_top_cm", "layer_bottom_cm", "coverage_ratio",
                "quality_flags", "provider_request_id",
            )
            if point.get(key) is not None
        })
        if point.get("unit") is not None:
            attributes["source_unit"] = point["unit"]
    return attributes


class NhzClimateSoilBase(CoordinatorEntity[NhzClimateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = "Weather data by Open-Meteo.com"

    def __init__(self, coordinator: NhzClimateCoordinator, zone: SoilZone) -> None:
        super().__init__(coordinator)
        self.zone = zone

    @property
    def zone_payload(self) -> dict[str, Any]:
        return _zone_payload(self.coordinator, self.zone)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_native_soil")},
            name=f"NHZ Climate Boden {self.coordinator.site}",
            manufacturer="NHZ",
            model="Native Bodenreihen",
            configuration_url="https://oc.nhz.de/docs",
        )


class NhzClimateSoilValueSensor(NhzClimateSoilBase):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: NhzClimateCoordinator,
        zone: SoilZone,
        quantity: str,
    ) -> None:
        super().__init__(coordinator, zone)
        self.quantity = quantity
        self._attr_unique_id = f"{coordinator.site}_soil_{zone}_{quantity}"
        self._attr_name = f"{ZONE_NAMES[zone]} {QUANTITY_NAMES[quantity]}"
        if quantity == "soil_temperature":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def point(self) -> dict[str, Any] | None:
        value = self.zone_payload.get("values", {}).get(self.quantity)
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        point = self.point
        return (
            super().available
            and self.zone_payload.get("availability") == "available"
            and point is not None
            and point.get("availability", "available") == "available"
            and point.get("value") is not None
        )

    @property
    def native_value(self) -> float | None:
        return self.point.get("value") if self.point else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self.point.get("unit") if self.point else None
        return UnitOfTemperature.CELSIUS if unit == "°C" else unit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _compact_attributes(self.zone_payload, self.point)


class NhzClimateSoilSourceSensor(NhzClimateSoilBase):
    def __init__(self, coordinator: NhzClimateCoordinator, zone: SoilZone) -> None:
        super().__init__(coordinator, zone)
        self._attr_unique_id = f"{coordinator.site}_soil_{zone}_source_model_key"
        self._attr_name = f"{ZONE_NAMES[zone]} Quellenmodell"

    @property
    def native_value(self) -> str:
        return str(self.zone_payload.get("source_model_key") or "unavailable")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _compact_attributes(self.zone_payload)


def build_soil_entities(
    coordinator: NhzClimateCoordinator,
) -> list[SensorEntity]:
    if coordinator.site.lower() not in FIXED_ZONE_SOURCE_POLICY:
        return []
    entities: list[SensorEntity] = []
    for zone in SOIL_ZONES:
        entities.extend((
            NhzClimateSoilValueSensor(coordinator, zone, "soil_temperature"),
            NhzClimateSoilValueSensor(coordinator, zone, "soil_moisture"),
            NhzClimateSoilSourceSensor(coordinator, zone),
        ))
    return entities
