from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, config_validation as cv
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import NhzClimateApi, NhzClimateAuthError, NhzClimateError
from .const import (
    CONF_BASE_URL,
    CONF_CREATE_SL_VENTILATION_ZONES,
    CONF_CREATE_SL_SURFACES,
    CONF_DATASET,
    CONF_FORECAST_ENTITY,
    CONF_INDOOR_HUMIDITY_ENTITY,
    CONF_INDOOR_TEMPERATURE_ENTITY,
    CONF_NAME,
    CONF_OUTDOOR_HUMIDITY_ENTITY,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_PRESSURE_ENTITY,
    CONF_RAIN_SOURCE_ENTITY,
    CONF_RAIN_COUNTER_ENTITY,
    CONF_RAIN_RATE_ENTITY,
    CONF_PROVIDER_UPDATE_MINUTES,
    CONF_SITE,
    CONF_TEMPERATURE_SOURCE_ENTITY,
    CONF_TOKEN,
    CONF_VARIABLES,
    CONF_WIND_GUST_ENTITY,
    CONF_ZONE_ID,
    CONF_SURFACE_ID,
    CONF_SURFACE_TYPE,
    CONF_TILT_DEG,
    CONF_AZIMUTH_DEG,
    CONF_GROSS_AREA_M2,
    CONF_WINDOW_AREA_M2,
    CONF_OTHER_OPENING_AREA_M2,
    CONF_GTI_ENTITY,
    DEFAULT_BASE_URL,
    DEFAULT_DATASET,
    DEFAULT_FORECAST_ENTITY,
    DEFAULT_PROVIDER_UPDATE_MINUTES,
    DEFAULT_VARIABLES,
    DOMAIN,
    SUBENTRY_TYPE_VENTILATION_ZONE,
    SUBENTRY_TYPE_SURFACE,
    SL_VENTILATION_ZONE_PRESETS,
    SL_SURFACE_PRESETS,
    SL_SOURCE_PRESETS,
)
from .surfaces import Surface, SurfaceType, SurfaceValidationError


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    if CONF_BASE_URL in normalized:
        normalized[CONF_BASE_URL] = str(normalized[CONF_BASE_URL]).rstrip("/")
    raw = normalized.get(CONF_VARIABLES, DEFAULT_VARIABLES)
    if isinstance(raw, str):
        normalized[CONF_VARIABLES] = [
            value.strip() for value in raw.split(",") if value.strip()
        ]
    else:
        normalized[CONF_VARIABLES] = list(raw)
    for key in (
        CONF_RAIN_SOURCE_ENTITY,
        CONF_TEMPERATURE_SOURCE_ENTITY,
        CONF_OUTDOOR_TEMPERATURE_ENTITY,
        CONF_OUTDOOR_HUMIDITY_ENTITY,
        CONF_PRESSURE_ENTITY,
        CONF_RAIN_RATE_ENTITY,
        CONF_RAIN_COUNTER_ENTITY,
        CONF_WIND_GUST_ENTITY,
    ):
        normalized[key] = str(normalized.get(key, "")).strip()
    normalized[CONF_PROVIDER_UPDATE_MINUTES] = int(
        normalized.get(CONF_PROVIDER_UPDATE_MINUTES, DEFAULT_PROVIDER_UPDATE_MINUTES)
    )
    return normalized


def _source_error(hass, entity_id: str, variable: str) -> str | None:
    """Validate an optional local source against its measurement semantics."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return "source_not_found"
    attributes = state.attributes
    unit = attributes.get("unit_of_measurement")
    state_class = attributes.get("state_class")
    if variable in {"rain", "rain_counter"}:
        if unit != "mm":
            return f"{variable}_source_unit"
        if state_class != "total_increasing":
            return f"{variable}_source_state_class"
    elif variable in {"temperature_2m", "temperature"}:
        if unit != "°C":
            return f"{variable}_source_unit"
        if state_class != "measurement":
            return f"{variable}_source_state_class"
    elif variable == "humidity":
        if unit != "%":
            return "humidity_source_unit"
        if state_class != "measurement":
            return "humidity_source_state_class"
    elif variable == "pressure":
        if unit not in {"Pa", "hPa", "mbar"}:
            return "pressure_source_unit"
        if state_class != "measurement":
            return "pressure_source_state_class"
    elif variable == "rain_rate":
        if unit not in {"mm/h", "mm/hr"}:
            return "rain_rate_source_unit"
        if state_class != "measurement":
            return "rain_rate_source_state_class"
    elif variable == "wind_gust":
        if unit not in {"km/h", "m/s"}:
            return "wind_gust_source_unit"
        if state_class != "measurement":
            return "wind_gust_source_state_class"
    elif variable == "irradiance":
        if unit not in {"W/m²", "W/m2"}:
            return "irradiance_source_unit"
        if state_class != "measurement":
            return "irradiance_source_state_class"
    return None


def _sensor_selector() -> EntitySelector:
    """Return HA's searchable single-sensor entity picker."""
    return EntitySelector(EntitySelectorConfig(domain="sensor", multiple=False))


def _number_selector(minimum: float, maximum: float) -> NumberSelector:
    """A JSON-serializable numeric field for config-subentry geometry."""
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=0.01,
            mode=NumberSelectorMode.BOX,
        )
    )


def _source_schema(defaults: dict[str, Any]) -> dict:
    return {
        vol.Optional(
            CONF_RAIN_SOURCE_ENTITY,
            default=defaults.get(CONF_RAIN_SOURCE_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_TEMPERATURE_SOURCE_ENTITY,
            default=defaults.get(CONF_TEMPERATURE_SOURCE_ENTITY, ""),
        ): _sensor_selector(),
    }


def _ventilation_source_schema(
    defaults: dict[str, Any], *, include_provider_interval: bool = True
) -> dict:
    """Schema for parent-entry outdoor sources used by all room subentries.

    All fields intentionally remain optional on the parent entry.  NHZ Climate
    can be used as a history integration without ventilation zones; creating a
    zone later requires the three physical air-state sources.
    """

    schema = {
        vol.Optional(
            CONF_OUTDOOR_TEMPERATURE_ENTITY,
            default=defaults.get(CONF_OUTDOOR_TEMPERATURE_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_OUTDOOR_HUMIDITY_ENTITY,
            default=defaults.get(CONF_OUTDOOR_HUMIDITY_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_PRESSURE_ENTITY,
            default=defaults.get(CONF_PRESSURE_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_RAIN_RATE_ENTITY,
            default=defaults.get(CONF_RAIN_RATE_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_RAIN_COUNTER_ENTITY,
            default=defaults.get(CONF_RAIN_COUNTER_ENTITY, ""),
        ): _sensor_selector(),
        vol.Optional(
            CONF_WIND_GUST_ENTITY,
            default=defaults.get(CONF_WIND_GUST_ENTITY, ""),
        ): _sensor_selector(),
    }
    if include_provider_interval:
        schema[
            vol.Optional(
                CONF_PROVIDER_UPDATE_MINUTES,
                default=defaults.get(
                    CONF_PROVIDER_UPDATE_MINUTES, DEFAULT_PROVIDER_UPDATE_MINUTES
                ),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=15, max=1440))
    return schema


def _zone_schema(defaults: dict[str, Any], *, allow_zone_id: bool) -> dict:
    schema: dict = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): cv.string,
        vol.Required(
            CONF_INDOOR_TEMPERATURE_ENTITY,
            default=defaults.get(CONF_INDOOR_TEMPERATURE_ENTITY, ""),
        ): _sensor_selector(),
        vol.Required(
            CONF_INDOOR_HUMIDITY_ENTITY,
            default=defaults.get(CONF_INDOOR_HUMIDITY_ENTITY, ""),
        ): _sensor_selector(),
    }
    if allow_zone_id:
        schema = {
            # HA 2026.9's frontend schema serializer cannot serialize
            # ``cv.slug``.  Keep the text field serializable and validate the
            # stable identifier explicitly after submission.
            vol.Required(
                CONF_ZONE_ID, default=defaults.get(CONF_ZONE_ID, "")
            ): TextSelector(TextSelectorConfig()),
            **schema,
        }
    return schema


def _zone_data(data: dict[str, Any], *, zone_id: str | None = None) -> dict[str, Any]:
    """Normalize a zone without changing the stable identifier on edit."""
    normalized = dict(data)
    if zone_id is not None:
        normalized[CONF_ZONE_ID] = zone_id
    for key in (CONF_ZONE_ID, CONF_NAME, CONF_INDOOR_TEMPERATURE_ENTITY, CONF_INDOOR_HUMIDITY_ENTITY):
        if key in normalized:
            normalized[key] = str(normalized[key]).strip()
    return normalized


def _validate_ventilation_sources(hass, data: dict[str, Any]) -> dict[str, str]:
    """Return field errors for shared outdoor source choices."""
    errors: dict[str, str] = {}
    for field, variable in (
        (CONF_OUTDOOR_TEMPERATURE_ENTITY, "temperature"),
        (CONF_OUTDOOR_HUMIDITY_ENTITY, "humidity"),
        (CONF_PRESSURE_ENTITY, "pressure"),
        (CONF_RAIN_RATE_ENTITY, "rain_rate"),
        (CONF_RAIN_COUNTER_ENTITY, "rain_counter"),
        (CONF_WIND_GUST_ENTITY, "wind_gust"),
    ):
        if error := _source_error(hass, data.get(field, ""), variable):
            errors[field] = error
    return errors


def _validate_zone_sources(hass, data: dict[str, Any]) -> dict[str, str]:
    errors = {
        field: error
        for field, variable in (
            (CONF_INDOOR_TEMPERATURE_ENTITY, "temperature"),
            (CONF_INDOOR_HUMIDITY_ENTITY, "humidity"),
        )
        if (error := _source_error(hass, data.get(field, ""), variable))
    }
    if CONF_ZONE_ID in data and not re.fullmatch(
        r"[a-z0-9_]+", data.get(CONF_ZONE_ID, "")
    ):
        errors[CONF_ZONE_ID] = "invalid_slug"
    return errors


def _surface_schema(
    defaults: dict[str, Any], *, include_id: bool, include_type: bool
) -> dict:
    """Form fields for one persisted physical plane.

    The source remains optional because geometry can be recorded before a
    vendor provides a matching oriented GTI.  Its output is then unavailable,
    never fabricated as zero.
    """
    schema: dict = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): cv.string,
        vol.Required(
            CONF_TILT_DEG, default=defaults.get(CONF_TILT_DEG, 90.0)
        ): _number_selector(0, 90),
        vol.Required(
            CONF_AZIMUTH_DEG, default=defaults.get(CONF_AZIMUTH_DEG, 0.0)
        ): _number_selector(-180, 180),
        vol.Required(
            CONF_GROSS_AREA_M2, default=defaults.get(CONF_GROSS_AREA_M2, 0.0)
        ): _number_selector(0, 100000),
        vol.Optional(
            CONF_GTI_ENTITY, default=defaults.get(CONF_GTI_ENTITY, "")
        ): _sensor_selector(),
    }
    surface_type = defaults.get(CONF_SURFACE_TYPE)
    if include_type:
        schema = {
            vol.Required(
                CONF_SURFACE_TYPE, default=surface_type or SurfaceType.FACADE.value
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SurfaceType.FACADE.value, SurfaceType.ROOF.value],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            **schema,
        }
    if include_id:
        schema = {
            vol.Required(
                CONF_SURFACE_ID, default=defaults.get(CONF_SURFACE_ID, "")
            ): TextSelector(TextSelectorConfig()),
            **schema,
        }
    if surface_type == SurfaceType.FACADE.value:
        # Put the area partition next to the gross area in the form.  All
        # three are disjoint and validated together below.
        schema[vol.Required(
            CONF_WINDOW_AREA_M2,
            default=defaults.get(CONF_WINDOW_AREA_M2, 0.0),
        )] = _number_selector(0, 100000)
        schema[vol.Required(
            CONF_OTHER_OPENING_AREA_M2,
            default=defaults.get(CONF_OTHER_OPENING_AREA_M2, 0.0),
        )] = _number_selector(0, 100000)
    return schema


def _surface_data(data: dict[str, Any], *, surface_id: str | None = None,
                  surface_type: str | None = None) -> dict[str, Any]:
    """Normalize persisted config and let the domain object enforce geometry."""
    normalized = dict(data)
    if surface_id is not None:
        normalized[CONF_SURFACE_ID] = surface_id
    if surface_type is not None:
        normalized[CONF_SURFACE_TYPE] = surface_type
    for key in (CONF_SURFACE_ID, CONF_NAME, CONF_GTI_ENTITY):
        if key in normalized:
            normalized[key] = str(normalized[key]).strip()
    for key in (
        CONF_TILT_DEG, CONF_AZIMUTH_DEG, CONF_GROSS_AREA_M2,
        CONF_WINDOW_AREA_M2, CONF_OTHER_OPENING_AREA_M2,
    ):
        if key in normalized:
            normalized[key] = float(normalized[key])
    if normalized.get(CONF_SURFACE_TYPE) != SurfaceType.FACADE.value:
        normalized.pop(CONF_WINDOW_AREA_M2, None)
        normalized.pop(CONF_OTHER_OPENING_AREA_M2, None)
    return normalized


def _validate_surface(hass, data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    try:
        Surface.from_mapping(data)
    except SurfaceValidationError as exc:
        message = str(exc)
        field = next(
            (
                candidate for candidate in (
                    CONF_SURFACE_ID, CONF_SURFACE_TYPE, CONF_NAME,
                    CONF_TILT_DEG, CONF_AZIMUTH_DEG, CONF_GROSS_AREA_M2,
                    CONF_WINDOW_AREA_M2, CONF_OTHER_OPENING_AREA_M2,
                ) if candidate in message
            ),
            "base",
        )
        errors[field] = "invalid_surface"
    if error := _source_error(hass, data.get(CONF_GTI_ENTITY, ""), "irradiance"):
        errors[CONF_GTI_ENTITY] = error
    return errors


class NhzClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    _pending_user_data: dict[str, Any] | None = None
    _pending_subentries: list[dict[str, Any]] | None = None

    async def _validate(self, data: dict[str, Any]) -> str | None:
        api = NhzClimateApi(
            aiohttp_client.async_get_clientsession(self.hass),
            data[CONF_BASE_URL],
            data[CONF_TOKEN],
        )
        try:
            sites = await api.sites()
        except NhzClimateAuthError:
            return "invalid_auth"
        except NhzClimateError:
            return "cannot_connect"
        if data[CONF_SITE] not in {item.get("slug") for item in sites}:
            return "unknown_site"
        for entity_id, variable in (
            (data.get(CONF_RAIN_SOURCE_ENTITY, ""), "rain"),
            (data.get(CONF_TEMPERATURE_SOURCE_ENTITY, ""), "temperature_2m"),
        ):
            if error := _source_error(self.hass, entity_id, variable):
                return error
        if errors := _validate_ventilation_sources(self.hass, data):
            return next(iter(errors.values()))
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = normalize(user_input)
            if error := await self._validate(data):
                errors["base"] = error
            else:
                await self.async_set_unique_id(
                    f"{data[CONF_BASE_URL]}|{data[CONF_SITE]}|{data[CONF_DATASET]}"
                )
                self._abort_if_unique_id_configured()
                if data[CONF_SITE] == "sl":
                    self._pending_user_data = data
                    return await self.async_step_sl_ventilation_zones()
                return self.async_create_entry(
                    title=f"NHZ Climate {data[CONF_SITE]}", data=data
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): cv.url,
                vol.Required(CONF_TOKEN): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
                # Text selectors remain JSON-serializable in current HA;
                # site validity is checked against the API below.
                vol.Required(CONF_SITE, default="sl"): TextSelector(
                    TextSelectorConfig()
                ),
                vol.Required(CONF_DATASET, default=DEFAULT_DATASET): TextSelector(
                    TextSelectorConfig()
                ),
                vol.Required(
                    CONF_FORECAST_ENTITY, default=DEFAULT_FORECAST_ENTITY
                ): _sensor_selector(),
                vol.Required(
                    CONF_VARIABLES, default=",".join(DEFAULT_VARIABLES)
                ): str,
                # The default site is SL.  Every source remains visible and
                # editable; another site must replace these before validation.
                **_source_schema(SL_SOURCE_PRESETS),
                **_ventilation_source_schema(SL_SOURCE_PRESETS),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sl_ventilation_zones(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer, but never silently create, the agreed SL pilot rooms."""
        if self._pending_user_data is None:
            return self.async_abort(reason="unknown_site")

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_CREATE_SL_VENTILATION_ZONES] and any(
                not self._pending_user_data.get(field)
                for field in (
                    CONF_OUTDOOR_TEMPERATURE_ENTITY,
                    CONF_OUTDOOR_HUMIDITY_ENTITY,
                    CONF_PRESSURE_ENTITY,
                    CONF_RAIN_RATE_ENTITY,
                    CONF_WIND_GUST_ENTITY,
                )
            ):
                errors["base"] = "outside_sources_missing"
            else:
                subentries = (
                    [
                        {
                            "subentry_type": SUBENTRY_TYPE_VENTILATION_ZONE,
                            "title": zone[CONF_NAME],
                            "unique_id": zone[CONF_ZONE_ID],
                            "data": zone,
                        }
                        for zone in SL_VENTILATION_ZONE_PRESETS
                    ]
                    if user_input[CONF_CREATE_SL_VENTILATION_ZONES]
                    else []
                )
                self._pending_subentries = subentries
                return await self.async_step_sl_surfaces()

        return self.async_show_form(
            step_id="sl_ventilation_zones",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CREATE_SL_VENTILATION_ZONES, default=True
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_sl_surfaces(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicitly confirm plan-derived SL geometry on a new entry.

        All six reviewed surfaces use explicitly oriented vendor GTI sources.
        """
        if self._pending_user_data is None:
            return self.async_abort(reason="unknown_site")
        if user_input is not None:
            subentries = list(self._pending_subentries or [])
            if user_input[CONF_CREATE_SL_SURFACES]:
                subentries.extend(
                    {
                        "subentry_type": SUBENTRY_TYPE_SURFACE,
                        "title": surface[CONF_NAME],
                        "unique_id": surface[CONF_SURFACE_ID],
                        "data": surface,
                    }
                    for surface in SL_SURFACE_PRESETS
                )
            return self.async_create_entry(
                title=f"NHZ Climate {self._pending_user_data[CONF_SITE]}",
                data=self._pending_user_data,
                subentries=subentries,
            )
        return self.async_show_form(
            step_id="sl_surfaces",
            data_schema=vol.Schema(
                {vol.Required(CONF_CREATE_SL_SURFACES, default=True): bool}
            ),
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        data = normalize(import_data)
        await self.async_set_unique_id(
            f"{data[CONF_BASE_URL]}|{data[CONF_SITE]}|{data[CONF_DATASET]}"
        )
        self._abort_if_unique_id_configured(updates=data)
        if error := await self._validate(data):
            return self.async_abort(reason=error)
        return self.async_create_entry(title=f"NHZ Climate {data[CONF_SITE]}", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return NhzClimateOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose individual indoor rooms as stable ventilation subentries."""
        return {
            SUBENTRY_TYPE_VENTILATION_ZONE: NhzClimateVentilationZoneSubentryFlow,
            SUBENTRY_TYPE_SURFACE: NhzClimateSurfaceSubentryFlow,
        }

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update shared outdoor source wiring without recreating zones."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = normalize(user_input)
            errors = _validate_ventilation_sources(self.hass, data)
            if not errors:
                updates = {
                    key: data[key]
                    for key in (
                        CONF_OUTDOOR_TEMPERATURE_ENTITY,
                        CONF_OUTDOOR_HUMIDITY_ENTITY,
                        CONF_PRESSURE_ENTITY,
                        CONF_RAIN_RATE_ENTITY,
                        CONF_RAIN_COUNTER_ENTITY,
                        CONF_WIND_GUST_ENTITY,
                    )
                }
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                _ventilation_source_schema(
                    dict(entry.data), include_provider_interval=False
                )
            ),
            errors=errors,
        )


class NhzClimateOptionsFlow(OptionsFlow):
    """Choose optional local recorder sources without changing API credentials."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = normalize(user_input)
            for entity_id, variable, key in (
                (data[CONF_RAIN_SOURCE_ENTITY], "rain", CONF_RAIN_SOURCE_ENTITY),
                (
                    data[CONF_TEMPERATURE_SOURCE_ENTITY],
                    "temperature_2m",
                    CONF_TEMPERATURE_SOURCE_ENTITY,
                ),
            ):
                if error := _source_error(self.hass, entity_id, variable):
                    errors[key] = error
            if not errors:
                return self.async_create_entry(title="", data=data)

        defaults = dict(self.config_entry.data)
        defaults.update(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_source_schema(defaults)),
            errors=errors,
        )


class NhzClimateVentilationZoneSubentryFlow(ConfigSubentryFlow):
    """Create and modify one indoor zone for the ventilation pilot."""

    def _required_outdoor_sources_missing(self, entry: ConfigEntry) -> bool:
        """A zone needs a physical indoor/outdoor air-state comparison."""
        return any(
            not str(entry.data.get(field, "")).strip()
            for field in (
                CONF_OUTDOOR_TEMPERATURE_ENTITY,
                CONF_OUTDOOR_HUMIDITY_ENTITY,
                CONF_PRESSURE_ENTITY,
                CONF_RAIN_RATE_ENTITY,
                CONF_WIND_GUST_ENTITY,
            )
        )

    def _zone_id_in_use(
        self, entry: ConfigEntry, zone_id: str, *, except_subentry_id: str | None = None
    ) -> bool:
        return any(
            subentry.subentry_type == SUBENTRY_TYPE_VENTILATION_ZONE
            and subentry.subentry_id != except_subentry_id
            and subentry.data.get(CONF_ZONE_ID) == zone_id
            for subentry in entry.subentries.values()
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add an explicitly confirmed room; no site defaults are created."""
        entry = self._get_entry()
        errors: dict[str, str] = {}
        if self._required_outdoor_sources_missing(entry):
            errors["base"] = "outside_sources_missing"
        elif user_input is not None:
            data = _zone_data(user_input)
            errors = _validate_zone_sources(self.hass, data)
            if self._zone_id_in_use(entry, data[CONF_ZONE_ID]):
                errors[CONF_ZONE_ID] = "zone_id_exists"
            if not errors:
                return self.async_create_entry(
                    title=data[CONF_NAME], data=data, unique_id=data[CONF_ZONE_ID]
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(_zone_schema({}, allow_zone_id=True)),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a zone's display name or sensor pair, never its zone ID."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if self._required_outdoor_sources_missing(entry):
            errors["base"] = "outside_sources_missing"
        elif user_input is not None:
            data = _zone_data(user_input, zone_id=subentry.data[CONF_ZONE_ID])
            errors = _validate_zone_sources(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    subentry,
                    title=data[CONF_NAME],
                    data=data,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                _zone_schema(dict(subentry.data), allow_zone_id=False)
            ),
            errors=errors,
        )


class NhzClimateSurfaceSubentryFlow(ConfigSubentryFlow):
    """Create/edit one physical facade or roof plane.

    Stable identity and type are selected first.  Geometry and the optional
    vendor-GTI source follow in a second form, allowing facade-only area
    fields to remain absent for roof planes.
    """

    _pending_identity: dict[str, str] | None = None

    def _surface_id_in_use(
        self, entry: ConfigEntry, surface_id: str, *, except_subentry_id: str | None = None
    ) -> bool:
        return any(
            subentry.subentry_type == SUBENTRY_TYPE_SURFACE
            and subentry.subentry_id != except_subentry_id
            and subentry.data.get(CONF_SURFACE_ID) == surface_id
            for subentry in entry.subentries.values()
        )

    @staticmethod
    def _identity_schema(defaults: dict[str, Any] | None = None) -> dict:
        defaults = defaults or {}
        return {
            vol.Required(
                CONF_SURFACE_ID, default=defaults.get(CONF_SURFACE_ID, "")
            ): TextSelector(TextSelectorConfig()),
            vol.Required(
                CONF_SURFACE_TYPE,
                default=defaults.get(CONF_SURFACE_TYPE, SurfaceType.FACADE.value),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SurfaceType.FACADE.value, SurfaceType.ROOF.value],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            surface_id = str(user_input.get(CONF_SURFACE_ID, "")).strip()
            surface_type = str(user_input.get(CONF_SURFACE_TYPE, "")).strip()
            # Surface's full validation also requires geometry, so validate
            # this small identity subset explicitly here.
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", surface_id):
                errors[CONF_SURFACE_ID] = "invalid_surface_id"
            elif self._surface_id_in_use(entry, surface_id):
                errors[CONF_SURFACE_ID] = "surface_id_exists"
            if surface_type not in {SurfaceType.FACADE.value, SurfaceType.ROOF.value}:
                errors[CONF_SURFACE_TYPE] = "invalid_surface"
            if not errors:
                self._pending_identity = {
                    CONF_SURFACE_ID: surface_id,
                    CONF_SURFACE_TYPE: surface_type,
                }
                return await self.async_step_details()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(self._identity_schema()),
            errors=errors,
        )

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if self._pending_identity is None:
            return self.async_abort(reason="unknown_site")
        errors: dict[str, str] = {}
        defaults = dict(self._pending_identity)
        if user_input is not None:
            data = _surface_data(
                user_input,
                surface_id=self._pending_identity[CONF_SURFACE_ID],
                surface_type=self._pending_identity[CONF_SURFACE_TYPE],
            )
            errors = _validate_surface(self.hass, data)
            if not errors:
                return self.async_create_entry(
                    title=data[CONF_NAME],
                    data=data,
                    unique_id=data[CONF_SURFACE_ID],
                )
            defaults.update(data)
        return self.async_show_form(
            step_id="details",
            data_schema=vol.Schema(
                _surface_schema(defaults, include_id=False, include_type=False)
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        defaults = dict(subentry.data)
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _surface_data(
                user_input,
                surface_id=str(subentry.data[CONF_SURFACE_ID]),
                surface_type=str(subentry.data[CONF_SURFACE_TYPE]),
            )
            errors = _validate_surface(self.hass, data)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, subentry, title=data[CONF_NAME], data=data
                )
            defaults.update(data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                _surface_schema(defaults, include_id=False, include_type=False)
            ),
            errors=errors,
        )
