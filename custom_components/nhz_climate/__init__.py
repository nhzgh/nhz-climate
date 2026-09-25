from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_validation as cv

from .api import NhzClimateApi
from .const import (
    CONF_BASE_URL,
    CONF_DATASET,
    CONF_FORECAST_ENTITY,
    CONF_SITE,
    CONF_TOKEN,
    CONF_VARIABLES,
    DEFAULT_BASE_URL,
    DEFAULT_DATASET,
    DEFAULT_FORECAST_ENTITY,
    DEFAULT_VARIABLES,
    DOMAIN,
)
from .coordinator import NhzClimateConfigEntry, NhzClimateCoordinator

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_TOKEN): cv.string,
                vol.Required(CONF_SITE): cv.slug,
                vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): cv.url,
                vol.Optional(CONF_DATASET, default=DEFAULT_DATASET): cv.slug,
                vol.Optional(
                    CONF_FORECAST_ENTITY, default=DEFAULT_FORECAST_ENTITY
                ): cv.entity_id,
                vol.Optional(CONF_VARIABLES, default=list(DEFAULT_VARIABLES)): vol.All(
                    cv.ensure_list, [cv.slug]
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if yaml_config := config.get(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=dict(yaml_config),
            )
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: NhzClimateConfigEntry
) -> bool:
    api = NhzClimateApi(
        aiohttp_client.async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_TOKEN],
    )
    coordinator = NhzClimateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NhzClimateConfigEntry
) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
