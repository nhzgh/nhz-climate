# Changelog

All notable changes to the NHZ Climate integration are documented here.

## 0.9.1 - 2026-09-25

- Expose shared outdoor and ventilation safety sources in Home Assistant's
  normal Configure dialog and honor option values when editing room zones.
- Feed the local rain gauge into all-phase cumulative precipitation while
  retaining the modelled solid-water equivalent.
- Add the HACS brand asset and repository validation metadata.

## 0.9.0 - 2026-09-25

- Prepare the integration for installation as a custom HACS repository.
- Add the S01 psychrometric and ventilation calculation core.
- Add the S02 surface geometry and experimental local GTI calculation core.
- Add the S04 native-soil capability and provenance model.
- Add the S03 unshaded incident-solar power, energy and scalar aggregation core.
- Add the S07 advisory-only ventilation policy and replay core.
- Add ventilation-zone config subentries, event-driven HA entities and
  LTS-capable pilot measurements/cumulative counters.
- Use searchable Home Assistant sensor selectors for all entity source fields.
- Use a configurable provider interval with stable per-entry jitter and remove
  the extra one-minute post-start provider refresh.
- Add six configurable SL facade/roof planes with oriented vendor-GTI,
  incident power, accumulated energy and conservative facade/roof/envelope
  aggregates. Values remain explicitly labelled unshaded working values.
- Add true cohort-based cumulative P10/P50/P90 precipitation comparisons for
  7, 30 and 90 days plus rolling-365-day monthly rows.
- Show only all-phase precipitation in the standard dashboard; retain liquid
  rain and snow comparison entities for optional specialist views.
- Change today's temperature anomaly to the complete local calendar-day mean,
  combining completed local observations with the remaining hourly forecast.

## 0.8.0 - 2026-09-21

- Start the SL-only 30-day pilot feature line. The recommendation is an
  explicitly labelled working value and never controls a window or HVAC
  actuator.
- Deploy the three confirmed SL zones to xj1 with 18 measurement and 27
  monotonic LTS series; no pilot dashboard is required.
