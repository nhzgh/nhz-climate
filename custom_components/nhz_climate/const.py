from __future__ import annotations

from datetime import timedelta

DOMAIN = "nhz_climate"
CONF_BASE_URL = "base_url"
CONF_TOKEN = "token"
CONF_SITE = "site"
CONF_DATASET = "dataset"
CONF_VARIABLES = "variables"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_RAIN_SOURCE_ENTITY = "rain_source_entity"
CONF_TEMPERATURE_SOURCE_ENTITY = "temperature_source_entity"

# Ventilation pilot configuration.  Shared outdoor sources are stored on the
# parent entry; each room is a separate config subentry so its identity and
# recorder entities remain stable when other rooms are changed.
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
CONF_OUTDOOR_HUMIDITY_ENTITY = "outdoor_humidity_entity"
CONF_PRESSURE_ENTITY = "pressure_entity"
CONF_RAIN_RATE_ENTITY = "rain_rate_entity"
CONF_RAIN_COUNTER_ENTITY = "rain_counter_entity"
CONF_WIND_GUST_ENTITY = "wind_gust_entity"
CONF_PROVIDER_UPDATE_MINUTES = "provider_update_minutes"
CONF_ZONE_ID = "zone_id"
CONF_NAME = "name"
CONF_INDOOR_TEMPERATURE_ENTITY = "indoor_temperature_entity"
CONF_INDOOR_HUMIDITY_ENTITY = "indoor_humidity_entity"
CONF_CREATE_SL_VENTILATION_ZONES = "create_sl_ventilation_zones"
SUBENTRY_TYPE_VENTILATION_ZONE = "ventilation_zone"

# Surface/incident-solar configuration.  A surface is an independently
# editable config subentry: its lower-case ID is its persistent recorder
# identity, while the display name is deliberately free to change.
CONF_SURFACE_ID = "surface_id"
CONF_SURFACE_TYPE = "surface_type"
CONF_TILT_DEG = "tilt_deg"
CONF_AZIMUTH_DEG = "azimuth_deg"
CONF_GROSS_AREA_M2 = "gross_area_m2"
CONF_WINDOW_AREA_M2 = "window_area_m2"
CONF_OTHER_OPENING_AREA_M2 = "other_opening_area_m2"
CONF_GTI_ENTITY = "gti_entity"
CONF_CREATE_SL_SURFACES = "create_sl_surfaces"
SUBENTRY_TYPE_SURFACE = "surface"

DEFAULT_BASE_URL = "https://oc.nhz.de"
DEFAULT_DATASET = "era5_seamless"
DEFAULT_FORECAST_ENTITY = "weather.forecast_sl"
DEFAULT_PROVIDER_UPDATE_MINUTES = 60

SL_SOURCE_PRESETS = {
    CONF_RAIN_SOURCE_ENTITY: "sensor.sl_gw1100a_total_rain",
    CONF_TEMPERATURE_SOURCE_ENTITY: "sensor.gw1100a_lokal_aussentemperatur",
    CONF_OUTDOOR_TEMPERATURE_ENTITY: "sensor.gw1100a_lokal_aussentemperatur",
    CONF_OUTDOOR_HUMIDITY_ENTITY: "sensor.gw1100a_lokal_aussenluftfeuchte",
    CONF_PRESSURE_ENTITY: "sensor.outdoor_environment_west_pressure",
    CONF_RAIN_RATE_ENTITY: "sensor.sl_gw1100a_rain_rate",
    CONF_RAIN_COUNTER_ENTITY: "sensor.sl_gw1100a_total_rain",
    CONF_WIND_GUST_ENTITY: "sensor.sl_gw1100a_wind_gust",
}

# A site=sl user flow presents these as a reviewable, opt-in initial setup.
# They are deliberately not applied to imports/YAML or to any other site.
SL_VENTILATION_ZONE_PRESETS = (
    {
        CONF_ZONE_ID: "nanils",
        CONF_NAME: "Nanils",
        CONF_INDOOR_TEMPERATURE_ENTITY: "sensor.sz_climatecontrol_room_temperature",
        CONF_INDOOR_HUMIDITY_ENTITY: "sensor.sz_climatecontrol_room_humidity",
    },
    {
        CONF_ZONE_ID: "nino",
        CONF_NAME: "Nino",
        CONF_INDOOR_TEMPERATURE_ENTITY: "sensor.nino_climatecontrol_room_temperature",
        CONF_INDOOR_HUMIDITY_ENTITY: "sensor.nino_climatecontrol_room_humidity",
    },
    {
        CONF_ZONE_ID: "kueche",
        CONF_NAME: "Küche",
        CONF_INDOOR_TEMPERATURE_ENTITY: "sensor.kuche_climatecontrol_room_temperature",
        CONF_INDOOR_HUMIDITY_ENTITY: "sensor.kuche_climatecontrol_room_humidity",
    },
)

# These values are deliberately configuration data rather than entity logic.
# They are plan-derived working geometry, visibly confirmed during a fresh SL
# setup, and can be changed through individual surface subentries later.
# Open-Meteo azimuth: 0° south, negative east, positive west.
SL_SURFACE_PRESETS = (
    {
        CONF_SURFACE_ID: "north_facade",
        CONF_SURFACE_TYPE: "facade",
        CONF_NAME: "Nordfassade",
        CONF_TILT_DEG: 90.0,
        CONF_AZIMUTH_DEG: -175.0,
        CONF_GROSS_AREA_M2: 80.39,
        CONF_WINDOW_AREA_M2: 2.50,
        CONF_OTHER_OPENING_AREA_M2: 0.0,
        CONF_GTI_ENTITY: "sensor.fassade_nord_solar_gti",
    },
    {
        CONF_SURFACE_ID: "east_facade",
        CONF_SURFACE_TYPE: "facade",
        CONF_NAME: "Ostfassade",
        CONF_TILT_DEG: 90.0,
        CONF_AZIMUTH_DEG: -95.0,
        CONF_GROSS_AREA_M2: 69.38,
        CONF_WINDOW_AREA_M2: 11.92,
        CONF_OTHER_OPENING_AREA_M2: 5.98,
        CONF_GTI_ENTITY: "sensor.fassade_ost_solar_gti",
    },
    {
        CONF_SURFACE_ID: "south_facade",
        CONF_SURFACE_TYPE: "facade",
        CONF_NAME: "Südfassade",
        CONF_TILT_DEG: 90.0,
        CONF_AZIMUTH_DEG: -5.0,
        CONF_GROSS_AREA_M2: 80.39,
        CONF_WINDOW_AREA_M2: 3.16,
        CONF_OTHER_OPENING_AREA_M2: 2.21,
        CONF_GTI_ENTITY: "sensor.fassade_sud_solar_gti",
    },
    {
        CONF_SURFACE_ID: "west_facade",
        CONF_SURFACE_TYPE: "facade",
        CONF_NAME: "Westfassade",
        CONF_TILT_DEG: 90.0,
        CONF_AZIMUTH_DEG: 85.0,
        CONF_GROSS_AREA_M2: 69.38,
        CONF_WINDOW_AREA_M2: 15.48,
        CONF_OTHER_OPENING_AREA_M2: 0.0,
        CONF_GTI_ENTITY: "sensor.fassade_west_solar_gti",
    },
    {
        CONF_SURFACE_ID: "east_roof",
        CONF_SURFACE_TYPE: "roof",
        CONF_NAME: "Dach Ost",
        CONF_TILT_DEG: 15.0,
        CONF_AZIMUTH_DEG: -95.0,
        CONF_GROSS_AREA_M2: 61.0,
        CONF_GTI_ENTITY: "sensor.outdoor_environment_ost_solar_gti",
    },
    {
        CONF_SURFACE_ID: "west_roof",
        CONF_SURFACE_TYPE: "roof",
        CONF_NAME: "Dach West",
        CONF_TILT_DEG: 17.0,
        CONF_AZIMUTH_DEG: 85.0,
        CONF_GROSS_AREA_M2: 94.0,
        CONF_GTI_ENTITY: "sensor.outdoor_environment_west_solar_gti",
    },
)
DEFAULT_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "shortwave_radiation",
)
# The current local-IST boundary advances once per completed hour.  A six-hour
# coordinator cadence would mislabel several finished hours as unavailable.
DEFAULT_UPDATE_INTERVAL = timedelta(hours=1)
PRIMARY_BASELINE = "1970-2025"
SECONDARY_BASELINE = "1950-2026"
MONTHLY_COMPARISON_WINDOWS = (7, 30, 90, 365)

# Local sources are intentionally blank by default.  Site-specific source
# choices belong in a deployment generator/config entry, never this reusable
# integration.
LOCAL_SOURCE_BY_VARIABLE = {
    "rain": CONF_RAIN_SOURCE_ENTITY,
    "temperature_2m": CONF_TEMPERATURE_SOURCE_ENTITY,
}

# Directly comparable Outdoor Environment entities. Derived indices, air
# quality, pollen, UV and categorical fields intentionally stay vendor-only.
PROFILE_ENTITIES = {
    "temperature_2m": "sensor.outdoor_environment_west_temperature",
    "relative_humidity_2m": "sensor.outdoor_environment_west_humidity",
    "dew_point_2m": "sensor.outdoor_environment_west_dew_point",
    "apparent_temperature": "sensor.outdoor_environment_west_apparent_temperature",
    "precipitation": "sensor.outdoor_environment_west_precipitation",
    "rain": "sensor.outdoor_environment_west_rain",
    "snowfall": "sensor.outdoor_environment_west_snowfall",
    "surface_pressure": "sensor.outdoor_environment_west_pressure",
    "cloud_cover": "sensor.outdoor_environment_west_cloud_cover",
    "wind_speed_10m": "sensor.outdoor_environment_west_wind_speed",
    "wind_direction_10m": "sensor.outdoor_environment_west_wind_direction",
    "wind_gusts_10m": "sensor.outdoor_environment_west_wind_gusts",
    "shortwave_radiation": "sensor.outdoor_environment_west_solar_ghi",
    "direct_radiation": "sensor.outdoor_environment_west_solar_direct",
    "diffuse_radiation": "sensor.outdoor_environment_west_solar_diffuse",
    "direct_normal_irradiance": "sensor.outdoor_environment_west_solar_dni",
    "et0_fao_evapotranspiration": "sensor.outdoor_environment_west_evapotranspiration",
    "vapour_pressure_deficit": "sensor.outdoor_environment_west_vapour_pressure_deficit",
}
