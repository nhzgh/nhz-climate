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

## 0.9 feature scope

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
  metrics.

Surface values are incident, unshaded working values. They are not an
EnergyPlus heating/cooling-load model.

## License

MIT
