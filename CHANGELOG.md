# Changelog

All notable changes to the NHZ Climate integration are documented here.

## 0.10.0 - 2026-09-27

- Judge unchanged local Home Assistant measurements by current entity
  availability and valid value/unit/range instead of treating an old
  `last_updated` timestamp as a failed heartbeat. Model fallback values keep
  their stricter age and provider-timestep checks.
- Stop rejecting independently updated local temperature/humidity pairs for
  timestamp skew while retaining exact model-timestep validation.
- Add the fourth ventilation recommendation `optional` for climate-neutral
  conditions: opening a window is neither required nor harmful, for example
  when the user wants to reduce CO2. This does not claim that CO2 was measured.
- Record the optional recommendation in a separate monotonic pilot-duration
  sensor and expose humidity and thermal partial decisions unchanged.
- Reload the integration automatically after adding, changing, or removing a
  ventilation zone or building surface, so its entities appear immediately.
- Update the packaged integration icon.

## 0.9.3 - 2026-09-27

- Treat a missing or unavailable rain-rate source as an omitted optional
  safety interlock instead of making the ventilation decision unavailable;
  every measured rain event and its drydown still block ventilation.
- Label the add-site, add-zone and add-surface actions explicitly in the Home
  Assistant integration UI and classify the integration as a hub.
- Replace the non-serializable URL validator in the initial config form with
  Home Assistant's URL selector, fixing the Add Service 500 response.

## 0.9.2 - 2026-09-27

- Make every optional source entity genuinely clearable in the initial and
  options flows instead of restoring an empty or previous selector default.
- Filter entity pickers by physical device class and use a weather-domain
  picker for the forecast entity.
- Preserve all submitted values when one source fails validation and report
  the error on the affected field.
- Stop injecting SL source entities into a new configuration before its site
  has been validated.

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
