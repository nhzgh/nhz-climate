from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import NhzClimateConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    "resolved_grid_latitude",
    "resolved_grid_longitude",
    "resolved_grid_elevation_m",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NhzClimateConfigEntry
) -> dict[str, Any]:
    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "config_subentries": {
            subentry.subentry_id: {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": async_redact_data(dict(subentry.data), TO_REDACT),
            }
            for subentry in entry.subentries.values()
        },
        "coordinator": async_redact_data(entry.runtime_data.data, TO_REDACT),
        "ventilation_pilot": {
            subentry_id: context.diagnostics()
            for subentry_id, context in entry.runtime_data.ventilation_contexts.items()
        },
        "surfaces": {
            subentry_id: context.diagnostics()
            for subentry_id, context in getattr(
                entry.runtime_data, "surface_contexts", {}
            ).items()
        },
    }
