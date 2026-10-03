# NHZ Climate

Custom Home Assistant integration for climate references, cumulative
precipitation comparisons, configurable building surfaces and advisory-only
ventilation metrics.

The repository is intended for installation as a HACS custom **Integration**
repository. It is not submitted to the default HACS catalog.

## Installation

1. Add `https://github.com/nhzgh/nhz-climate` in HACS as a custom repository
   of type **Integration**.
2. Install **NHZ Climate** and restart Home Assistant.
3. Add the integration under **Settings → Devices & services**.
4. Configure the read-only climate API URL, token, site and the desired local
   Home Assistant source entities.

The integration does not contain API credentials, host-specific secrets or
location coordinates. The API remains a separately operated service.

## Release and deployment rule

Home Assistant sites install and update this integration only from a published
GitHub release through HACS. A commit on `main` is not deployable. Maintainers
publish a version with the **Publish HACS release** GitHub Actions workflow;
the workflow requires the requested semantic version to match `manifest.json`,
runs HACS validation, creates the GitHub release and verifies that it is
published on the exact approved commit supplied to the workflow. Site rollout starts only after HACS
reports that release as its latest version.

## 0.10 feature scope

- ERA5 climate profiles for 1970–2025 and the full available history.
- True cohort-based cumulative precipitation P10/P50/P90 for 7, 30 and 90
  days, plus rolling-365-day monthly comparisons.
- Separate rain, snowfall and all-phase precipitation entities.
- The configured local gauge replaces only liquid rain in the all-phase
  cumulative actual; modelled solid-water equivalent remains included.
- Configurable facade and roof subentries with oriented GTI, incident power,
  accumulated energy and conservative aggregates.
- Calendar-day temperature anomaly based on completed local observations, the
  current explicitly selected outdoor sensor and the remaining forecast.
- Configurable advisory-only ventilation zones and LTS-compatible pilot
  metrics. Local HA measurements remain valid while their entities are
  available, even if their value has not changed recently; provider-model
  fallbacks retain strict timestep checks.
- Four explainable ventilation states: `ja`, `ambivalent`, `nein` and
  `optional`. `optional` means that ventilation is climatically neutral and
  may be used for another purpose such as CO2 reduction; it does not imply a
  CO2 measurement.
- The window-open sensor additionally exposes compact room-climate and
  one-/eight-hour projection attributes for the card: fixed initial working
  values are 60 m³ room volume, 100 m³/h air flow, 22 °C / 50 % rF targets and
  an 8-hour thermal time constant. The current outdoor observation supplies
  the first (possibly partial) hour; later intervals use the coordinator's
  hourly weather forecast. The 8-hour projection remains incomplete rather
  than repeating a current observation when forecast coverage is missing;
  it uses only the current weather-service response, not older points kept
  for same-day climate charts.
- Newly added or changed ventilation zones and surfaces become active without
  a manual integration reload.

Surface values are incident, unshaded working values. They are not an
EnergyPlus heating/cooling-load model.

## License

MIT
