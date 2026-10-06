# Changelog

All notable changes to the NHZ Climate integration are documented here.

## 0.10.5 - 2026-10-06

- Restrict `optional` in `duration_target_v1` to climate-neutral, non-harmful
  trajectories. A room that starts inside the broad comfort corridor but
  moves away from its 22 °C / 50 % rF target or crosses a corridor boundary
  now publishes `avoid` with no suggested opening duration.

## 0.10.4 - 2026-10-06

- Refine the advisory-only window recommendation using an exact standard-room
  trajectory at 15-minute endpoints through eight hours. The model retains
  the published working values: 60 m³, 100 m³/h and an 8-hour thermal time
  constant.
- Recommend ventilation only when normalised distance to 22 °C / 50 % rF
  improves by at least 5 %. Temperature is scaled by 2 K and relative
  humidity by 10 percentage points, matching the half-width of the accepted
  20–24 °C / 40–60 % rF corridor.
- Do not newly cross a comfort boundary or worsen a dimension which is
  already outside that corridor. Keep the absolute-humidity direction as a
  physical moisture gate, independent of the relative-humidity display.
- Treat the trajectory as one continuous path: after either dimension reaches
  its corridor it may not leave it again, and any non-floating-point increase
  after the best normalised target distance ends the useful duration. Thus a
  later recovery never turns an earlier unsafe or worsening segment into an
  overnight recommendation.
- Publish the versioned `duration_target_v1` contract with trajectory,
  15/30/60-minute and 8-hour summaries, action, recommended duration,
  target distances, improvement and limiting reason.
- Map duration-aware actions compatibly: `short_airing`, `ventilate` and
  `overnight` publish legacy state `ja`; `avoid` publishes `nein`; and
  `optional` remains `optional`. The refined result enters the existing
  15-minute stability gate before any state is published. Rain and gust
  safety locks continue to override every climate recommendation.
- When the fresh hourly forecast is incomplete, permit only an explicitly
  marked short recommendation from the covered near-term interval; never
  make a one-hour or overnight recommendation from repeated current weather.

## 0.10.3 - 2026-10-03

- Add compact, per-zone ventilation projection attributes for the current
  room climate plus one and eight hours. They use the agreed standard room
  (60 m³), reference air flow (100 m³/h), 22 °C / 50 % rF targets and an
  explicit 8-hour thermal working time constant.
- Calculate moisture with ideal outdoor-air mixing and relative humidity at
  the simultaneously projected room temperature. Publish the separate
  air-only limit alongside the thermally buffered working result.
- Use the current local outdoor observation through the current hour, then
  step through the already-loaded hourly weather forecast. A one-hour result
  can explicitly fall back to the current observation; eight hours stays
  incomplete when a forecast interval is absent. The advisory and weather
  safety locks are unchanged.
- Keep the current raw weather response separate from the intentionally
  merged same-day climate curve, so a failed refresh cannot leave an old
  future forecast looking valid for an eight-hour ventilation projection.

## 0.10.2 - 2026-10-01

- Calculate today's high temperature over the complete local calendar day:
  completed local hourly LTS maxima plus the remaining hourly forecast.
- Keep the high unavailable when either segment has an uncovered hour and
  expose coverage, source counts, time bounds and calculation semantics as
  diagnostic attributes.

## 0.10.1 - 2026-09-29

- Keep the last confirmed ventilation recommendation available while a changed
  assessment passes the 15-minute stability interval, instead of temporarily
  exposing the entity as unavailable.
- Expose the unconfirmed candidate, its reasons, component assessments, start
  time and remaining stabilization time as separate diagnostic attributes.
- Apply rain and strong-gust safety locks immediately. After a lock clears,
  keep the effective `no` visible until the normal assessment is stable, and
  expose whether the safety lock itself is currently active.
- Continue to report genuine missing or invalid required inputs immediately as
  unavailable when no independent safety lock decides the result; recovery
  and integration startup both begin with a fresh stability interval.

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
