from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    PROFILE_ENTITIES,
    SUBENTRY_TYPE_SURFACE,
    SUBENTRY_TYPE_VENTILATION_ZONE,
)
from .coordinator import NhzClimateConfigEntry, NhzClimateCoordinator
from .soil_sensor import build_soil_entities
from .surface_sensor import build_surface_entities
from .ventilation_sensor import build_ventilation_zone_entities

PARALLEL_UPDATES = 0

DEVICE_CLASSES = {
    "temperature_2m": SensorDeviceClass.TEMPERATURE,
    "relative_humidity_2m": SensorDeviceClass.HUMIDITY,
    "pressure_msl": SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    "surface_pressure": SensorDeviceClass.ATMOSPHERIC_PRESSURE,
    "precipitation": SensorDeviceClass.PRECIPITATION,
    "rain": SensorDeviceClass.PRECIPITATION,
    "wind_speed_10m": SensorDeviceClass.WIND_SPEED,
    "wind_gusts_10m": SensorDeviceClass.WIND_SPEED,
    "wind_direction_10m": SensorDeviceClass.WIND_DIRECTION,
    "shortwave_radiation": SensorDeviceClass.IRRADIANCE,
}

PROFILE_NAMES = {
    "relative_humidity_2m": "Relative Feuchte Klimaprofil",
    "dew_point_2m": "Taupunkt Klimaprofil",
    "apparent_temperature": "Gefühlte Temperatur Klimaprofil",
    "precipitation": "Niederschlag Klimaprofil",
    "rain": "Regen Klimaprofil",
    "snowfall": "Schneefall Klimaprofil",
    "surface_pressure": "Oberflächendruck Klimaprofil",
    "cloud_cover": "Bewölkung Klimaprofil",
    "wind_speed_10m": "Windgeschwindigkeit Klimaprofil",
    "wind_direction_10m": "Windrichtung Klimaprofil",
    "wind_gusts_10m": "Windböen Klimaprofil",
    "shortwave_radiation": "Globalstrahlung Klimaprofil",
    "direct_radiation": "Direktstrahlung Klimaprofil",
    "diffuse_radiation": "Diffusstrahlung Klimaprofil",
    "direct_normal_irradiance": "Direkte Normalstrahlung Klimaprofil",
    "et0_fao_evapotranspiration": "ET0 Klimaprofil",
    "vapour_pressure_deficit": "Dampfdruckdefizit Klimaprofil",
}

FORECAST_FIELDS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "precipitation": "precipitation",
    "cloud_cover": "cloud_coverage",
    "wind_speed_10m": "wind_speed",
    "wind_direction_10m": "wind_bearing",
}

SOURCE_SUFFIXES = {
    "relative_humidity_2m": "humidity", "dew_point_2m": "dew_point",
    "apparent_temperature": "apparent_temperature", "precipitation": "precipitation",
    "rain": "rain", "snowfall": "snowfall", "surface_pressure": "pressure",
    "cloud_cover": "cloud_cover", "wind_speed_10m": "wind_speed",
    "wind_direction_10m": "wind_direction", "wind_gusts_10m": "wind_gusts",
    "shortwave_radiation": "solar_ghi", "direct_radiation": "solar_direct",
    "diffuse_radiation": "solar_diffuse", "direct_normal_irradiance": "solar_dni",
    "et0_fao_evapotranspiration": "evapotranspiration",
    "vapour_pressure_deficit": "vapour_pressure_deficit",
}


# The API currently returns IFS day 3--15 and EC46 day 10--46 planning
# products. The limits retain that complete current horizon but prevent an API
# expansion from turning one Home Assistant state attribute into an unbounded
# Recorder payload.
MAX_DRY_WINDOW_MODELS = 2
DRY_WINDOW_MODEL_PRIORITY = ("ifs_eps", "ec46_eps")
# Four six-hour starts per day, each with one 24-hour and one 48-hour window,
# plus a small boundary allowance. These bounds retain the complete current
# planning horizons without allowing a future API response to grow forever.
MAX_DRY_WINDOWS_BY_MODEL = {"ifs_eps": 112, "ec46_eps": 304}


def _finite_probability(value: Any) -> float | None:
    """Return a JSON probability only when it is a usable closed interval."""
    if isinstance(value, bool):
        return None
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(probability) or not 0 <= probability <= 1:
        return None
    return probability


def _compact_forecast_number(value: Any) -> float | None:
    """Keep planning attributes bounded and reject non-finite API values."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if isfinite(number) else None


def _compact_dry_window(window: Any) -> dict[str, Any] | None:
    """Reduce one API planning window to the dashboard-safe public contract."""
    if not isinstance(window, dict):
        return None
    hours = window.get("window_hours")
    if hours not in (24, 48):
        return None
    probability = _finite_probability(window.get("dry_probability"))
    valid_from = window.get("valid_from")
    valid_to = window.get("valid_to")
    if probability is None or not valid_from or not valid_to:
        return None
    try:
        start = datetime.fromisoformat(str(valid_from).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(valid_to).replace("Z", "+00:00"))
    except ValueError:
        return None
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or end - start != timedelta(hours=hours)
    ):
        return None
    # Short internal keys make validation/selection cheap before the public
    # columnar encoder removes repeated timestamps and keys: h hours, s/e UTC
    # bounds, d probability, c coverage, l/m/q P10/P50/P90 in mm.
    compact = {"h": hours, "s": str(valid_from), "e": str(valid_to), "d": probability}
    coverage = _finite_probability(window.get("coverage_ratio"))
    if coverage is not None:
        compact["c"] = coverage
    for output_key, key in (("l", "p10_mm"), ("m", "p50_mm"), ("q", "p90_mm")):
        value = _compact_forecast_number(window.get(key))
        if value is not None:
            compact[output_key] = value
    return compact


def _columnar_dry_window_series(windows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Encode fixed-duration forecast rows without repeated ISO timestamps.

    Every API window is UTC and starts on an exact hourly boundary.  A series
    stores its first timestamp once; all following starts are integer-hour
    offsets.  The arrays use identical positions, including explicit ``None``
    for an unavailable optional coverage or quantile.  This is lossless for
    the planning contract while remaining below Home Assistant's attribute
    size limit for the current complete IFS and EC46 horizons.
    """
    result: dict[str, dict[str, Any]] = {}
    for hours in (24, 48):
        rows = [window for window in windows if window["h"] == hours]
        if not rows:
            continue
        base = rows[0]["s"]
        try:
            base_time = datetime.fromisoformat(base.replace("Z", "+00:00"))
        except ValueError:
            continue
        if base_time.tzinfo is None:
            continue
        offsets: list[int] = []
        for row in rows:
            try:
                start_time = datetime.fromisoformat(row["s"].replace("Z", "+00:00"))
            except ValueError:
                offsets = []
                break
            if start_time.tzinfo is None:
                continue
            offset_seconds = (start_time - base_time).total_seconds()
            if offset_seconds % 3600:
                continue
            offsets.append(int(offset_seconds // 3600))
        if len(offsets) != len(rows):
            # Invalid timestamps are not eligible planning values. Never
            # replace them with a guessed calendar position.
            continue
        columns: dict[str, Any] = {
            "base": base,
            "offset_hours": offsets,
            "dry_probability": [row["d"] for row in rows],
        }
        coverage = [row.get("c") for row in rows]
        # Full coverage is the overwhelmingly common case and is harmless to
        # omit. A present array remains positional so partial/missing rows are
        # represented exactly rather than silently treated as complete.
        if any(value is None or value < 1 for value in coverage):
            columns["coverage_ratio"] = coverage
        for output_key, input_key in (
            ("p10_mm", "l"), ("p50_mm", "m"), ("p90_mm", "q")
        ):
            values = [row.get(input_key) for row in rows]
            if any(value is not None for value in values):
                columns[output_key] = values
        result[f"{hours}h"] = columns
    return result


def _columnar_dry_window_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only model provenance plus compact positional planning series."""
    result: list[dict[str, Any]] = []
    for model in models:
        series = _columnar_dry_window_series(model["windows"])
        if not series:
            continue
        compact = {
            "model_key": model["model_key"],
            "provider_model": model["provider_model"],
            "series": series,
        }
        if snapshot_at := model.get("snapshot_at"):
            compact["snapshot_at"] = snapshot_at
        result.append(compact)
    return result


def compact_dry_window_planning(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Publish a small planning contract and its deterministic best candidate.

    Planning values are forecast ensemble summaries, not observed values or
    LTS statistics.  The entity never synthesizes dry weather from a missing
    amount/probability.  Selection therefore needs a genuine probability and
    complete time bounds.  The sensor state specifically selects the
    highest-probability 24-hour window; probability ties use the earliest
    window, then documented model priority. Cards receive both 24/48-hour
    series and make their own duration-specific selection.
    """
    if not isinstance(payload, list):
        return [], None

    models: list[dict[str, Any]] = []
    candidates: list[tuple[str, dict[str, Any]]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        model_key = item.get("model_key")
        provider_model = item.get("provider_model")
        if not isinstance(model_key, str) or not model_key.strip():
            continue
        if not isinstance(provider_model, str) or not provider_model.strip():
            continue
        raw_windows = item.get("windows")
        # The current API exposes the full horizon in ``windows``.  Only old
        # API responses lack that field, in which case its two shortcuts are
        # still useful as a bounded compatibility fallback.
        if isinstance(raw_windows, list):
            raw_window_items = raw_windows
        else:
            raw_window_items = [item.get("next_24h"), item.get("next_48h")]
        windows = [
            compact
            for compact in (_compact_dry_window(window) for window in raw_window_items)
            if compact is not None
        ]
        if not windows:
            continue
        windows = list({
            (window["h"], window["s"], window["e"]): window
            for window in windows
        }.values())
        windows.sort(key=lambda window: (window["s"], window["h"]))
        compact_model_key = model_key.strip()[:80]
        limit = MAX_DRY_WINDOWS_BY_MODEL.get(compact_model_key)
        if limit is None:
            continue
        windows = windows[:limit]
        compact_model: dict[str, Any] = {
            "model_key": compact_model_key,
            "provider_model": provider_model.strip()[:120],
            "windows": windows,
        }
        snapshot_at = item.get("snapshot_at")
        if snapshot_at:
            compact_model["snapshot_at"] = str(snapshot_at)
        models.append(compact_model)
        candidates.extend(
            (compact_model["model_key"], window)
            for window in windows
            if window["h"] == 24
        )

    priority = {model: index for index, model in enumerate(DRY_WINDOW_MODEL_PRIORITY)}
    models.sort(
        key=lambda model: (
            priority.get(model["model_key"], len(priority)),
            min(window["s"] for window in model["windows"]),
            model["model_key"],
        )
    )
    selected = min(
        candidates,
        key=lambda item: (
            -item[1]["d"],
            item[1]["s"],
            priority.get(item[0], len(priority)),
            item[0],
        ),
        default=None,
    )
    bounded = models[:MAX_DRY_WINDOW_MODELS]
    public_models = _columnar_dry_window_models(bounded)
    if selected is None:
        # A card can still render valid 48-hour planning rows even though the
        # entity state is explicitly unavailable without a 24-hour candidate.
        return public_models, None

    # Keep the selected model even if an API change makes it fall outside the
    # ordinary display cap; otherwise "best_available" would be opaque.
    selected_key, selected_window = selected
    selected_model = next(model for model in models if model["model_key"] == selected_key)
    if selected_key not in {model["model_key"] for model in bounded}:
        bounded = [selected_model, *bounded[: MAX_DRY_WINDOW_MODELS - 1]]
        public_models = _columnar_dry_window_models(bounded)
    selected_contract = {
        "model_key": selected_key,
        "provider_model": selected_model["provider_model"],
        "window_hours": selected_window["h"],
        "valid_from": selected_window["s"],
        "valid_to": selected_window["e"],
        "dry_probability": selected_window["d"],
    }
    for output_key, key in (("coverage_ratio", "c"), ("p10_mm", "l"),
                            ("p50_mm", "m"), ("p90_mm", "q")):
        if (value := selected_window.get(key)) is not None:
            selected_contract[output_key] = value
    if snapshot_at := selected_model.get("snapshot_at"):
        selected_contract["snapshot_at"] = snapshot_at
    return public_models, selected_contract


def _compact_number(value: Any) -> Any:
    """Keep dashboard values legible without recorder-sized float strings."""
    if isinstance(value, float):
        return round(value, 3)
    return value


def _compact_reference(reference: dict[str, Any] | None, *, daily: bool = False) -> dict[str, Any]:
    if not isinstance(reference, dict):
        return {}
    # P10/P50/P90 are all required for a cumulative comparison.  P50 must
    # not be inferred from the mean; precipitation is strongly skewed.
    keys = ("mean", "p10", "p50", "p90", "coverage_ratio")
    result = {
        key: _compact_number(reference[key])
        for key in keys
        if reference.get(key) is not None
    }
    # This field is only present for the legacy, explicitly non-cohort
    # fallback.  It is meaningful to a card/diagnostic but its other metadata
    # is not required for rendering.
    if reference.get("method"):
        result["method"] = reference["method"]
    return result


def _source_label(actual: dict[str, Any]) -> str | None:
    """Summarize verbose source segments before Recorder drops them."""
    labels: set[str] = set()
    for segment in actual.get("source_segments", []):
        if not isinstance(segment, dict):
            continue
        if segment.get("source_class") == "local_observation" or segment.get("source_entity"):
            labels.add("Lokal")
        model = str(segment.get("model", "")).lower()
        if "era5" in model:
            labels.add("ERA5")
        elif "icon" in model:
            labels.add("ICON")
    if labels:
        return " + ".join(label for label in ("Lokal", "ERA5", "ICON") if label in labels)
    source_class = actual.get("source_class")
    if source_class == "local_observation":
        return "Lokal"
    if source_class == "local_observation_with_modelled_solid":
        return "Lokal + Modellschnee"
    if source_class == "reanalysis":
        return "ERA5"
    if source_class == "forecast_archive":
        return "ICON"
    return str(source_class) if source_class else None


def _compact_actual(actual: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(actual, dict):
        return {}
    keys = ("sum", "coverage_ratio", "provisional", "mixed_sources", "source_class")
    result = {
        key: _compact_number(actual[key])
        for key in keys
        if actual.get(key) is not None and key != "source_class"
    }
    if source := _source_label(actual):
        result["source_class"] = source
    return result


def _compact_month_row(row: dict[str, Any], *, daily: bool = False) -> dict[str, Any]:
    actual = row.get("actual") or row.get("modelled_actual") or row.get("local_actual")
    result = {
        key: row[key]
        for key in (("local_date", "partial") if daily
                    else ("local_start", "local_end", "partial"))
        if row.get(key) is not None
    }
    reference = _compact_reference(row.get("reference"), daily=daily)
    if reference:
        result["reference"] = reference
    compact_actual = _compact_actual(actual)
    if compact_actual:
        # The card already treats `actual` as the canonical current curve and
        # exposes `source_class`/`provisional`; retaining the duplicate
        # modelled/local branches and source_segments adds no render value.
        result["actual"] = compact_actual
    return result


def _compact_cumulative_day(row: dict[str, Any]) -> dict[str, Any]:
    """Encode cumulative day points densely enough for HA state attributes.

    90 daily points plus 13 monthly rows must fit under HA's 16 KiB state
    attribute limit.  Keys are intentionally short but stable: ``d`` date,
    ``a`` actual, ``m`` mean, ``l`` P10, ``q`` P50, ``h`` P90, ``c`` coverage,
    ``p`` partial.  ``a: null`` is an explicit data gap, never dry weather.
    """
    start = row.get("local_start") or row.get("local_date")
    date = str(start)[:10] if start is not None else None
    actual = row.get("actual") or row.get("modelled_actual") or {}
    reference = row.get("reference") or {}
    result: dict[str, Any] = {"d": date, "a": _compact_number(
        actual.get("sum", actual.get("sum_mm"))
    )}
    aliases = (("m", "mean"), ("l", "p10"), ("q", "p50"), ("h", "p90"))
    for compact, key in aliases:
        value = reference.get(key, reference.get(f"{key}_mm"))
        if value is not None:
            result[compact] = _compact_number(value)
    coverage = actual.get("coverage_ratio")
    # Full coverage is the common case and the card treats an absent value as
    # 100 %. Omitting it saves enough attribute budget for a 90-day curve.
    if coverage is not None and float(coverage) < 0.999999:
        result["c"] = _compact_number(coverage)
    if row.get("partial"):
        result["p"] = True
    return result


def compact_monthly_comparisons(comparisons: dict[str, Any]) -> dict[str, Any]:
    """Publish the card contract below Home Assistant's 16 KiB state limit.

    Hourly API data, source-segment internals, reference-year lists and
    duplicate modelled/local branches have already served their coordinator
    purpose.  They must not be persisted repeatedly by Recorder.
    """
    compacted: dict[str, Any] = {}
    for range_key, payload in comparisons.items():
        if not isinstance(payload, dict):
            continue
        result = {
            key: payload[key]
            for key in ("site_timezone", "through_utc", "unit")
            if payload.get(key) is not None
        }
        months = [
            _compact_month_row(row)
            for row in payload.get("months", [])
            if isinstance(row, dict)
        ]
        if months:
            result["months"] = months
        total = _compact_month_row(payload.get("total", {}))
        if total:
            result["total"] = total
        if range_key in {"7d", "30d", "90d"}:
            daily = [
                _compact_cumulative_day(row)
                for row in payload.get("daily", [])
                if isinstance(row, dict)
            ]
            if daily:
                result["daily"] = daily
        compacted[range_key] = result
    return compacted


def _variable_profile(
    coordinator: NhzClimateCoordinator, container_key: str, variable: str
) -> dict[str, Any]:
    return next(
        (
            item
            for item in coordinator.data.get(container_key, {}).get("items", [])
            if item.get("variable") == variable
        ),
        {},
    )


async def async_setup_entry(
    hass,
    entry: NhzClimateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities = [
        NhzClimateSensor(coordinator, variable) for variable in coordinator.variables
    ]
    entities.extend(
        [
            NhzClimateComparisonSensor(coordinator),
            NhzClimateNormalHighSensor(coordinator),
            NhzClimateNormalLowSensor(coordinator),
            NhzClimateForecastHighSensor(coordinator),
            NhzClimateDryWindowPlanningSensor(coordinator),
        ]
    )
    entities.extend(
        NhzClimateVariableProfileSensor(coordinator, variable, source_entity)
        for variable, source_entity in PROFILE_ENTITIES.items()
        if variable != "temperature_2m"
    )
    entities.extend(
        NhzClimateMonthlyComparisonSensor(coordinator, variable)
        for variable in ("rain", "snowfall", "precipitation")
    )
    entities.extend(build_soil_entities(coordinator))
    async_add_entities(entities)

    # Ventilation zones share the existing entry and therefore the same
    # provider coordinator, but their live calculations use only HA states.
    # Adding a room does not create another Open-Meteo request stream.
    coordinator.ventilation_contexts = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_VENTILATION_ZONE:
            continue
        context, zone_entities = build_ventilation_zone_entities(
            hass, entry, subentry
        )
        coordinator.ventilation_contexts[subentry.subentry_id] = context
        entry.async_on_unload(context.async_stop)
        async_add_entities(
            zone_entities,
            config_subentry_id=subentry.subentry_id,
        )

    # Solar surfaces are local, event-driven calculations over explicitly
    # selected oriented vendor-GTI entities. They share this config entry but
    # do not add provider requests.
    surface_subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_SURFACE
    ]
    collection, configured_surfaces, aggregate_entities = build_surface_entities(
        hass, entry, surface_subentries
    )
    coordinator.surface_collection = collection
    coordinator.surface_contexts = {}
    for subentry, context, surface_entities in configured_surfaces:
        coordinator.surface_contexts[subentry.subentry_id] = context
        entry.async_on_unload(context.async_stop)
        async_add_entities(
            surface_entities,
            config_subentry_id=subentry.subentry_id,
        )
    if configured_surfaces:
        async_add_entities(aggregate_entities)


class NhzClimateSensor(CoordinatorEntity[NhzClimateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = "Weather data by Open-Meteo.com"

    def __init__(self, coordinator: NhzClimateCoordinator, variable: str) -> None:
        super().__init__(coordinator)
        self.variable = variable
        self._attr_unique_id = f"{coordinator.site}_{coordinator.dataset}_{variable}"
        self._attr_name = variable.replace("_", " ").title()
        self._attr_device_class = DEVICE_CLASSES.get(variable)

    @property
    def _item(self) -> dict[str, Any] | None:
        for item in self.coordinator.data.get("items", []):
            if item.get("variable") == self.variable:
                return item
        return None

    @property
    def available(self) -> bool:
        item = self._item
        return super().available and item is not None and item.get("value") is not None

    @property
    def native_value(self) -> float | None:
        item = self._item
        return item.get("value") if item else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        item = self._item
        if not item:
            return None
        unit = item.get("unit")
        if unit == "°C":
            return UnitOfTemperature.CELSIUS
        if unit == "%":
            return PERCENTAGE
        return unit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        item = self._item or {}
        return {
            "source_class": self.coordinator.data.get("source_class"),
            "dataset": self.coordinator.dataset,
            "observed_at": item.get("observed_at"),
            "quality_flags": item.get("quality_flags"),
            "grid_latitude": item.get("resolved_grid_latitude"),
            "grid_longitude": item.get("resolved_grid_longitude"),
            "grid_elevation_m": item.get("resolved_grid_elevation_m"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_{self.coordinator.dataset}")},
            name=f"NHZ Climate {self.coordinator.site}",
            manufacturer="NHZ",
            model=f"Open-Meteo {self.coordinator.dataset}",
            configuration_url="https://oc.nhz.de/docs",
        )


class NhzClimateComparisonBase(CoordinatorEntity[NhzClimateCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: NhzClimateCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.site}_{coordinator.dataset}_{key}"
        self._attr_suggested_object_id = f"nhz_climate_{coordinator.site}_{key}"
        self._attr_name = name

    @property
    def profile(self) -> dict[str, Any]:
        return self.coordinator.data.get("temperature_profile", {})

    @property
    def forecast_high(self) -> float | None:
        high = self.coordinator.data.get("temperature_calendar_day_high", {})
        value = high.get("value") if isinstance(high, dict) else None
        return float(value) if value is not None else None

    @property
    def normal_high(self) -> float | None:
        value = self.profile.get("daily_max", {}).get("mean")
        return float(value) if value is not None else None

    @property
    def normal_low(self) -> float | None:
        value = self.profile.get("daily_min", {}).get("mean")
        return float(value) if value is not None else None

    @property
    def available(self) -> bool:
        return super().available and bool(self.profile)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_{self.coordinator.dataset}")},
            name=f"NHZ Climate {self.coordinator.site}",
            manufacturer="NHZ",
            model=f"Open-Meteo {self.coordinator.dataset}",
            configuration_url="https://oc.nhz.de/docs",
        )


class NhzClimateComparisonSensor(NhzClimateComparisonBase):
    def __init__(self, coordinator: NhzClimateCoordinator) -> None:
        super().__init__(
            coordinator,
            "temperature_anomaly_today",
            "Temperaturabweichung heute (IST + Prognose)",
        )

    @property
    def anomaly(self) -> dict[str, Any]:
        value = self.coordinator.data.get("temperature_calendar_day_anomaly", {})
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        # Keep diagnostic coverage attributes visible when a genuine data gap
        # makes the numeric state unknown.
        return super().available

    @property
    def native_value(self) -> float | None:
        value = self.anomaly.get("value")
        if value is None:
            return None
        return round(float(value), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        daily = _variable_profile(self.coordinator, "daily_profiles", "temperature_2m")
        daily_all = _variable_profile(
            self.coordinator, "daily_profiles_all", "temperature_2m"
        )
        seven_day = _variable_profile(
            self.coordinator, "seven_day_profiles", "temperature_2m"
        )
        seven_day_all = _variable_profile(
            self.coordinator, "seven_day_profiles_all", "temperature_2m"
        )
        regional = self.coordinator.data.get("regional_forecast", {})
        return {
            "baseline": self.profile.get("baseline"),
            "profile_date": self.profile.get("date"),
            "comparison_semantics": self.anomaly.get("semantics"),
            "estimated_day_mean": self.anomaly.get("estimated_day_mean"),
            "normal_day_mean": self.anomaly.get("normal_day_mean"),
            "expected_hours": self.anomaly.get("expected_hours"),
            "covered_hours": self.anomaly.get("covered_hours"),
            "actual_hours": self.anomaly.get("actual_hours"),
            "forecast_hours": self.anomaly.get("forecast_hours"),
            "current_proxy_hours": self.anomaly.get("current_proxy_hours"),
            "actual_through_utc": self.anomaly.get("actual_through_utc"),
            "range_start_utc": self.anomaly.get("range_start_utc"),
            "range_end_utc": self.anomaly.get("range_end_utc"),
            "quality_flags": self.anomaly.get("quality_flags"),
            "normal_low": self.normal_low,
            "normal_high": self.normal_high,
            "forecast_high": self.forecast_high,
            "hourly_normal": self.profile.get("hourly", []),
            "hourly_all": self.coordinator.data.get("temperature_profile_all", {}).get("hourly", []),
            "hourly_forecast": self.coordinator.data.get("hourly_forecast", []),
            "forecast_field": "temperature",
            "daily_normal": daily.get("daily", []),
            "daily_all": daily_all.get("daily", []),
            "daily_statistic": daily.get("statistic"),
            "seven_day_normal": seven_day.get("daily", []),
            "seven_day_all": seven_day_all.get("daily", []),
            "seven_day_statistic": seven_day.get("statistic"),
            "forecast_entity": self.coordinator.data.get("forecast_entity"),
            "forecast_availability": regional.get("availability"),
            "forecast_stale": regional.get("stale"),
            "forecast_model": regional.get("deterministic_model"),
            "forecast_run": regional.get("model_run_id"),
            "forecast_run_reference_time": regional.get("run_reference_time"),
            "algorithm_version": self.profile.get("algorithm_version"),
            "local_hourly_actual": self.coordinator.data.get(
                "local_hourly_actual", {}
            ).get("temperature_2m"),
        }


class NhzClimateNormalHighSensor(NhzClimateComparisonBase):
    def __init__(self, coordinator: NhzClimateCoordinator) -> None:
        super().__init__(coordinator, "temperature_normal_high", "Normale Höchsttemperatur")

    @property
    def native_value(self) -> float | None:
        return self.normal_high


class NhzClimateNormalLowSensor(NhzClimateComparisonBase):
    def __init__(self, coordinator: NhzClimateCoordinator) -> None:
        super().__init__(coordinator, "temperature_normal_low", "Normale Tiefsttemperatur")

    @property
    def native_value(self) -> float | None:
        return self.normal_low


class NhzClimateForecastHighSensor(NhzClimateComparisonBase):
    def __init__(self, coordinator: NhzClimateCoordinator) -> None:
        super().__init__(coordinator, "temperature_forecast_high", "Höchsttemperatur heute")

    @property
    def native_value(self) -> float | None:
        return self.forecast_high

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        high = self.coordinator.data.get("temperature_calendar_day_high", {})
        return high if isinstance(high, dict) else {}


class NhzClimateVariableProfileSensor(
    CoordinatorEntity[NhzClimateCoordinator], SensorEntity
):
    """One compact entity containing the hourly profiles for one variable."""

    _attr_has_entity_name = True
    _attr_attribution = "Weather data by Open-Meteo.com"
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: NhzClimateCoordinator,
        variable: str,
        source_entity: str,
    ) -> None:
        super().__init__(coordinator)
        self.variable = variable
        if coordinator.site == "h240":
            self.source_entity = (
                f"sensor.outdoor_environment_strassenseite_{SOURCE_SUFFIXES[variable]}"
            )
        else:
            self.source_entity = source_entity
        self._attr_unique_id = f"{coordinator.site}_{coordinator.dataset}_{variable}_profile"
        self._attr_name = PROFILE_NAMES[variable]
        self._attr_device_class = DEVICE_CLASSES.get(variable)

    def _profile(self, key: str) -> dict[str, Any] | None:
        return _variable_profile(self.coordinator, key, self.variable) or None

    @property
    def profile(self) -> dict[str, Any] | None:
        return self._profile("profiles")

    @property
    def current_point(self) -> dict[str, Any] | None:
        container = self.coordinator.data.get("profiles", {})
        profile = self.profile
        if not profile:
            return None
        timezone = ZoneInfo(container.get("timezone", "UTC"))
        hour = datetime.now(timezone).hour
        return next(
            (point for point in profile.get("hourly", []) if point.get("local_hour") == hour),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.current_point is not None

    @property
    def native_value(self) -> float | None:
        point = self.current_point
        return point.get("mean") if point else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        profile = self.profile
        if not profile:
            return None
        unit = profile.get("unit")
        if unit == "°C":
            return UnitOfTemperature.CELSIUS
        if unit == "%":
            return PERCENTAGE
        return unit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        profile = self.profile or {}
        all_profile = self._profile("profiles_all") or {}
        point = self.current_point or {}
        source = self.coordinator.hass.states.get(self.source_entity)
        daily = self._profile("daily_profiles") or {}
        daily_all = self._profile("daily_profiles_all") or {}
        seven_day = self._profile("seven_day_profiles") or {}
        seven_day_all = self._profile("seven_day_profiles_all") or {}
        forecast_field = FORECAST_FIELDS.get(self.variable)
        regional = self.coordinator.data.get("regional_forecast", {})
        return {
            "site_timezone": self.coordinator.data.get("profiles", {}).get("timezone"),
            "baseline": self.coordinator.data.get("profiles", {}).get("baseline"),
            "all_data_baseline": self.coordinator.data.get("profiles_all", {}).get("baseline"),
            "statistic": profile.get("statistic"),
            "current_hour_p10": point.get("p10"),
            "current_hour_p90": point.get("p90"),
            "source_entity": self.source_entity,
            "source_value": source.state if source else None,
            "hourly_normal": profile.get("hourly", []),
            "hourly_all": all_profile.get("hourly", []),
            "daily_normal": daily.get("daily", []),
            "daily_all": daily_all.get("daily", []),
            "daily_statistic": daily.get("statistic"),
            "seven_day_normal": seven_day.get("daily", []),
            "seven_day_all": seven_day_all.get("daily", []),
            "seven_day_statistic": seven_day.get("statistic"),
            "profile_date": self.coordinator.data.get("profiles", {}).get("date"),
            "forecast_field": forecast_field,
            "hourly_forecast": (
                self.coordinator.data.get("hourly_forecast", [])
                if forecast_field else []
            ),
            "forecast_availability": regional.get("availability"),
            "forecast_stale": regional.get("stale"),
            "forecast_model": regional.get("deterministic_model"),
            "forecast_run": regional.get("model_run_id"),
            "forecast_run_reference_time": regional.get("run_reference_time"),
            "forecast_probability_6h": regional.get("precipitation_probability_6h", []),
            # Optional additive S10 comparison contract.  Older API payloads
            # simply omit these fields; keeping the empty defaults makes the
            # entity/card backwards compatible while allowing IFS overlays
            # and compact planning windows when the API starts returning them.
            "forecast_comparisons": regional.get(
                "comparison_forecasts",
                regional.get(
                    "forecast_comparisons",
                    regional.get("forecast_comparison", regional.get("comparison", [])),
                ),
            ),
            "forecast_planning": regional.get(
                "planning_forecast",
                regional.get("forecast_planning", regional.get("planning", {})),
            ),
            "local_hourly_actual": self.coordinator.data.get(
                "local_hourly_actual", {}
            ).get(
                "rain" if self.variable == "precipitation" else self.variable
            ),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_{self.coordinator.dataset}")},
            name=f"NHZ Climate {self.coordinator.site}",
            manufacturer="NHZ",
            model=f"Open-Meteo {self.coordinator.dataset}",
            configuration_url="https://oc.nhz.de/docs",
        )


class NhzClimateDryWindowPlanningSensor(
    CoordinatorEntity[NhzClimateCoordinator], SensorEntity
):
    """Token-free, bounded ensemble planning windows for a dashboard card.

    This is intentionally not a precipitation sensor: a forecast probability
    is neither a measured precipitation amount nor an LTS statistic.  Its
    percentage state is only a convenient summary of the selected planning
    window; the time bounds and model provenance stay explicit in attributes.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _attr_attribution = "Precipitation planning forecast by NHZ / Open-Meteo"

    def __init__(self, coordinator: NhzClimateCoordinator) -> None:
        super().__init__(coordinator)
        # Forecast planning does not vary by climate baseline/dataset.  Keep
        # both identifier and preferred object id stable for one physical site.
        self._attr_unique_id = f"{coordinator.site}_dry_window_planning"
        self._attr_suggested_object_id = (
            f"nhz_climate_{coordinator.site}_dry_window_planning"
        )
        self._attr_name = f"{coordinator.site.upper()} Trockenfenster Planung"

    @property
    def _regional_forecast(self) -> dict[str, Any]:
        forecast = self.coordinator.data.get("regional_forecast", {})
        return forecast if isinstance(forecast, dict) else {}

    @property
    def _contract(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        # ``planning_forecast`` is the current API field.  The fallbacks make
        # a rolling API upgrade non-breaking without exposing arbitrary raw
        # coordinator content to Recorder or a card.
        forecast = self._regional_forecast
        payload = forecast.get(
            "planning_forecast",
            forecast.get("forecast_planning", forecast.get("planning", [])),
        )
        return compact_dry_window_planning(payload)

    @property
    def available(self) -> bool:
        _, selected = self._contract
        return super().available and selected is not None

    @property
    def native_value(self) -> float | None:
        _, selected = self._contract
        probability = selected.get("dry_probability") if selected else None
        return round(probability * 100, 1) if probability is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows, selected = self._contract
        forecast = self._regional_forecast
        if selected is None:
            reason = (
                "forecast_unavailable"
                if forecast.get("availability") == "unavailable"
                else "planning_24h_windows_unavailable"
            )
        else:
            reason = None
        return {
            "contract_version": "dry_window_planning_v2",
            "semantics": (
                "ensemble forecast dry probability for an absolute planning "
                "window; not an observation or LTS statistic"
            ),
            "availability": "available" if selected is not None else "unavailable",
            "unavailable_reason": reason,
            "selection": "best_available",
            "selection_policy": (
                "highest dry probability among complete 24-hour windows; "
                "ties use earliest window, then ifs_eps before ec46_eps"
            ),
            "selected_window": selected,
            "planning_models": windows,
            "site_timezone": forecast.get("timezone"),
            "upstream_availability": forecast.get("availability"),
            "upstream_stale": forecast.get("stale"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_{self.coordinator.dataset}")},
            name=f"NHZ Climate {self.coordinator.site}",
            manufacturer="NHZ",
            model=f"Open-Meteo {self.coordinator.dataset}",
            configuration_url="https://oc.nhz.de/docs",
        )


class NhzClimateMonthlyComparisonSensor(
    CoordinatorEntity[NhzClimateCoordinator], SensorEntity
):
    """Token-free aggregate payload for the precipitation comparison card."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_suggested_display_precision = 1
    _attr_attribution = "Climate reference by NHZ / Open-Meteo"

    def __init__(self, coordinator: NhzClimateCoordinator, variable: str) -> None:
        super().__init__(coordinator)
        self.variable = variable
        self._attr_native_unit_of_measurement = "cm" if variable == "snowfall" else "mm"
        label = {
            "rain": "Regen",
            "snowfall": "Schneefall",
            "precipitation": "Niederschlag",
        }[variable]
        self._attr_name = f"{label} Monatsvergleich"
        self._attr_unique_id = (
            f"{coordinator.site}_{coordinator.dataset}_{variable}_monthly_comparison"
        )
        self._attr_suggested_object_id = (
            f"nhz_climate_{coordinator.site}_{variable}_monthly_comparison"
        )

    @property
    def comparisons(self) -> dict[str, Any]:
        return self.coordinator.data.get("monthly_comparisons", {}).get(
            self.variable, {}
        )

    @property
    def available(self) -> bool:
        return super().available and bool(self.comparisons)

    @property
    def native_value(self) -> float | None:
        comparison = self.comparisons.get("30d", {})
        actual = comparison.get("actual") or comparison.get("total", {}).get(
            "modelled_actual", {}
        )
        value = actual.get("sum") if isinstance(actual, dict) else None
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # API credentials remain in the config entry; this payload contains
        # only the compact aggregate/provenance contract for dashboards.
        return {
            "variable": self.variable,
            "monthly_comparisons": compact_monthly_comparisons(self.comparisons),
            "source_entity": (
                self.coordinator._local_source_entity("rain") or None
                if self.variable in {"rain", "precipitation"} else None
            ),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.site}_{self.coordinator.dataset}")},
            name=f"NHZ Climate {self.coordinator.site}",
            manufacturer="NHZ",
            model=f"Open-Meteo {self.coordinator.dataset}",
            configuration_url="https://oc.nhz.de/docs",
        )
