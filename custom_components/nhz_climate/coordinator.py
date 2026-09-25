from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NhzClimateApi, NhzClimateDataUnavailableError, NhzClimateError
from .const import (
    CONF_DATASET,
    CONF_FORECAST_ENTITY,
    CONF_PROVIDER_UPDATE_MINUTES,
    LOCAL_SOURCE_BY_VARIABLE,
    MONTHLY_COMPARISON_WINDOWS,
    PRIMARY_BASELINE,
    SECONDARY_BASELINE,
    CONF_SITE,
    CONF_VARIABLES,
    DEFAULT_DATASET,
    DEFAULT_FORECAST_ENTITY,
    DEFAULT_PROVIDER_UPDATE_MINUTES,
    DEFAULT_VARIABLES,
    DOMAIN,
    PROFILE_ENTITIES,
)
from .local_history import (
    RAIN_VARIABLE,
    TEMPERATURE_VARIABLE,
    all_phase_precipitation_from_local_rain,
    calendar_day_temperature_anomaly,
    last_completed_hour,
    local_segment,
    normalize_daily_references,
)

type NhzClimateConfigEntry = ConfigEntry["NhzClimateCoordinator"]


class NhzClimateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: NhzClimateConfigEntry,
        api: NhzClimateApi,
    ) -> None:
        configured_minutes = max(
            15,
            min(
                360,
                int(
                    entry.data.get(
                        CONF_PROVIDER_UPDATE_MINUTES,
                        DEFAULT_PROVIDER_UPDATE_MINUTES,
                    )
                ),
            ),
        )
        # A stable per-entry +/-5 % offset prevents synchronized requests from
        # several sites without changing cadence after every restart.
        digest = sha256(entry.entry_id.encode()).digest()
        jitter_fraction = ((digest[0] / 255.0) - 0.5) / 10.0
        update_interval = timedelta(
            minutes=configured_minutes * (1.0 + jitter_fraction)
        )
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name=f"{DOMAIN}-{entry.entry_id}",
            update_interval=update_interval,
            config_entry=entry,
        )
        self.api = api
        self.site = entry.data[CONF_SITE]
        self.dataset = entry.data.get(CONF_DATASET, DEFAULT_DATASET)
        self.variables = tuple(entry.data.get(CONF_VARIABLES, DEFAULT_VARIABLES))
        self.forecast_entity = entry.data.get(CONF_FORECAST_ENTITY, DEFAULT_FORECAST_ENTITY)
        self.ventilation_contexts: dict[str, Any] = {}

    def _local_source_entity(self, variable: str) -> str:
        """Return an explicitly configured local source, never a site default."""
        key = LOCAL_SOURCE_BY_VARIABLE.get(variable)
        if not key:
            return ""
        return str(self.config_entry.options.get(key, self.config_entry.data.get(key, ""))).strip()

    def _merge_hourly_forecast(
        self, current: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Keep earlier forecast points so today's curve remains complete."""
        previous = self.data.get("hourly_forecast", []) if self.data else []
        merged = {
            item["datetime"]: item
            for item in (*previous, *current)
            if item.get("datetime")
        }
        return [merged[key] for key in sorted(merged)]

    async def _async_forecast(self, forecast_type: str) -> list[dict[str, Any]]:
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": forecast_type},
                target={"entity_id": self.forecast_entity},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError:
            return []
        if not isinstance(response, dict):
            return []
        forecast = response.get(self.forecast_entity, {}).get("forecast", [])
        fields = (
            "datetime", "temperature", "templow", "humidity", "precipitation",
            "cloud_coverage", "uv_index", "wind_bearing", "wind_speed", "condition",
        )
        return [
            {field: item.get(field) for field in fields if item.get(field) is not None}
            for item in forecast
            if item.get("datetime")
        ]

    async def _async_temperature_profile(self, baseline: str) -> dict[str, Any]:
        try:
            return await self.api.temperature_profile(self.site, self.dataset, baseline)
        except NhzClimateDataUnavailableError:
            return {}

    async def _async_profiles(self, baseline: str) -> dict[str, Any]:
        try:
            return await self.api.profiles(
                self.site, self.dataset, tuple(PROFILE_ENTITIES), baseline
            )
        except NhzClimateDataUnavailableError:
            return {}

    async def _async_daily_profiles(
        self, baseline: str, window_days: int = 1
    ) -> dict[str, Any]:
        try:
            return await self.api.daily_profiles(
                self.site, self.dataset, tuple(PROFILE_ENTITIES), baseline,
                window_days=window_days,
            )
        except NhzClimateDataUnavailableError:
            return {}

    async def _async_precipitation_comparison(
        self, variable: str, days: int
    ) -> dict[str, Any]:
        try:
            return await self.api.precipitation_comparison(
                self.site,
                self.dataset,
                variable=variable,
                days=days,
                baseline=PRIMARY_BASELINE,
                include_hourly=(
                    days in (7, 30, 90)
                    or (
                        variable in (RAIN_VARIABLE, "precipitation")
                        and bool(self._local_source_entity(RAIN_VARIABLE))
                    )
                ),
            )
        except NhzClimateDataUnavailableError:
            return {}

    async def _async_local_statistics(
        self,
        variable: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict[str, Any] | None:
        """Read only complete local LTS buckets for one configured source."""
        source_entity = self._local_source_entity(variable)
        if not source_entity:
            return None
        statistic_type = "mean" if variable == TEMPERATURE_VARIABLE else "change"
        try:
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_utc,
                end_utc,
                {source_entity},
                "hour",
                None,
                {statistic_type},
            )
        except Exception:  # Recorder unavailability must retain API fallback.
            return None
        return local_segment(
            result.get(source_entity, []),
            start_utc,
            end_utc,
            variable,
            source_entity,
        )

    @staticmethod
    def _parse_utc(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None

    async def _with_local_rain_actual(
        self,
        comparison: dict[str, Any],
        *,
        modelled_liquid_comparison: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prefer local liquid rain, preserving modelled solid precipitation.

        For the liquid-only ``rain`` variable the local counter replaces the
        modelled rain hour directly.  For all-phase ``precipitation`` it
        replaces only the modelled liquid component; any non-negative
        difference between modelled total precipitation and modelled rain is
        retained as solid-water equivalent.  This lets the standard dashboard
        use the accurate gauge without silently discarding snowfall.
        """
        result = self._normalize_comparison(comparison) if comparison else comparison
        liquid_modelled_hours = (
            modelled_liquid_comparison.get("hourly_modelled_actual", [])
            if isinstance(modelled_liquid_comparison, dict)
            else None
        )
        # This internal-only response is intentionally removed before the
        # coordinator data reaches an entity attribute.
        modelled_hours = result.pop("hourly_modelled_actual", []) if result else []
        if not result:
            return result
        start_utc = self._parse_utc(result.get("range_start_utc"))
        server_through = self._parse_utc(result.get("through_utc"))
        site_timezone = result.get("site_timezone")
        if not start_utc or not server_through or not isinstance(site_timezone, str):
            return result
        if not self._local_source_entity(RAIN_VARIABLE):
            if result.get("days") in (7, 30, 90) and isinstance(modelled_hours, list):
                result["daily_incremental"] = self._daily_from_hourly(
                    modelled_hours, site_timezone
                )
            return result
        end_utc = min(
            server_through,
            last_completed_hour(datetime.now(timezone.utc), site_timezone),
        )
        if end_utc <= start_utc:
            return result
        segment = await self._async_local_statistics(RAIN_VARIABLE, start_utc, end_utc)
        if segment is None:
            return result
        # Raw hourly samples are used only while assembling the response and
        # are never retained as entity attributes (a 365-day payload would be
        # needlessly large).
        result["local_actual"] = {
            key: value for key, value in segment.items() if key != "hourly"
        }
        if not isinstance(modelled_hours, list) or end_utc != server_through:
            return result
        hourly_values = {
            self._parse_utc(point["start"]): point["value"]
            for point in segment["hourly"]
            if self._parse_utc(point["start"]) is not None
        }

        modelled_by_start = {
            self._parse_utc(point.get("interval_start_utc")): point
            for point in modelled_hours
            if isinstance(point, dict)
            and self._parse_utc(point.get("interval_start_utc")) is not None
        }
        liquid_by_start = {
            self._parse_utc(point.get("interval_start_utc")): point
            for point in (liquid_modelled_hours or [])
            if isinstance(point, dict)
            and self._parse_utc(point.get("interval_start_utc")) is not None
        }

        def local_replacement(
            hour: datetime, local_rain: float
        ) -> tuple[float, dict[str, Any]] | None:
            """Return liquid-only or all-phase local replacement for one hour."""
            if liquid_modelled_hours is None:
                return local_rain, {
                    "source_class": "local_observation",
                    "source_entity": self._local_source_entity(RAIN_VARIABLE),
                    "method": "home_assistant_lts_change",
                }
            total_point = modelled_by_start.get(hour, {})
            liquid_point = liquid_by_start.get(hour, {})
            combined = all_phase_precipitation_from_local_rain(
                local_rain,
                total_point.get("value_mm"),
                liquid_point.get("value_mm"),
            )
            if combined is None:
                return None
            total_source = (
                total_point.get("source")
                if isinstance(total_point.get("source"), dict)
                else {}
            )
            return combined, {
                "source_class": "local_observation_with_modelled_solid",
                "source_entity": self._local_source_entity(RAIN_VARIABLE),
                "model": total_source.get("model"),
                "dataset": total_source.get("dataset"),
                "method": "local_rain_plus_modelled_solid_water_equivalent",
                "attribution": total_source.get("attribution"),
            }

        replacement_values: dict[datetime, float] = {}
        for hour, local_value in hourly_values.items():
            if hour is None:
                continue
            replacement = local_replacement(hour, local_value)
            if replacement is not None:
                replacement_values[hour] = replacement[0]

        def metric(range_start: datetime, range_end: datetime) -> dict[str, Any]:
            expected_hours = int((range_end - range_start).total_seconds() // 3600)
            selected: list[tuple[datetime, float | None, dict[str, Any]]] = []
            hour = range_start
            while hour < range_end:
                local_value = hourly_values.get(hour)
                if local_value is not None:
                    replacement = local_replacement(hour, local_value)
                    if replacement is not None:
                        selected.append((hour, replacement[0], replacement[1]))
                        hour += timedelta(hours=1)
                        continue
                modelled = modelled_by_start.get(hour, {})
                value = modelled.get("value_mm")
                try:
                    model_value = float(value) if value is not None else None
                except (TypeError, ValueError):
                    model_value = None
                source = modelled.get("source") if isinstance(modelled.get("source"), dict) else {}
                selected.append((hour, model_value, source))
                hour += timedelta(hours=1)

            available = [value for _, value, _ in selected if value is not None]
            segments: list[dict[str, Any]] = []
            for hour_start, value, source in selected:
                if value is None:
                    continue
                identity = (
                    source.get("source_class"), source.get("source_entity"),
                    source.get("model"), source.get("dataset"), source.get("method"),
                    source.get("attribution"),
                )
                if segments and segments[-1]["_identity"] == identity and segments[-1]["end_utc"] == hour_start.isoformat():
                    segments[-1]["end_utc"] = (hour_start + timedelta(hours=1)).isoformat()
                    segments[-1]["hours"] += 1
                    continue
                segments.append({
                    "_identity": identity,
                    "start_utc": hour_start.isoformat(),
                    "end_utc": (hour_start + timedelta(hours=1)).isoformat(),
                    "hours": 1,
                    **{key: value for key, value in source.items() if key != "hours"},
                })
            for item in segments:
                item.pop("_identity", None)
            complete = len(available) == expected_hours
            return {
                "sum": sum(available) if complete else None,
                "coverage_ratio": len(available) / expected_hours if expected_hours else 0.0,
                "expected_hours": expected_hours,
                "available_hours": len(available),
                "complete": complete,
                "provisional": any(
                    source.get("source_class") != "local_observation"
                    for _, value, source in selected if value is not None
                ),
                "mixed_sources": len({
                    source.get("source_class") for _, value, source in selected
                    if value is not None
                }) > 1,
                "source_class": (
                    "mixed" if len(segments) > 1
                    else (segments[0].get("source_class") if segments else None)
                ),
                "source_segments": segments,
            }

        actual = metric(start_utc, end_utc)
        result["actual"] = actual
        if result.get("days") in (7, 30, 90):
            result["daily_incremental"] = self._daily_from_hourly(
                modelled_hours,
                site_timezone,
                replacement_values,
                local_source_class=(
                    "local_observation_with_modelled_solid"
                    if liquid_modelled_hours is not None
                    else "local_observation"
                ),
            )
        normalized_months = []
        for month in result.get("months") or []:
            row = dict(month)
            month_start = self._parse_utc(row.get("local_start"))
            month_end = self._parse_utc(row.get("local_end"))
            if not month_start or not month_end or month_end > end_utc:
                normalized_months.append(row)
                continue
            row["actual"] = metric(month_start, month_end)
            normalized_months.append(row)
        result["months"] = normalized_months
        if isinstance(result.get("total"), dict):
            result["total"] = {**result["total"], "actual": actual}
        return result

    def _daily_from_hourly(
        self,
        modelled_hours: list[dict[str, Any]],
        site_timezone: str,
        local_values: dict[datetime | None, float] | None = None,
        local_source_class: str = "local_observation",
    ) -> list[dict[str, Any]]:
        """Compact a resolved hourly series into local daily points.

        The last, still-open local day is explicitly partial.  It carries no
        normal in ``_attach_daily_reference`` and can therefore not be drawn
        against a full-day reference by accident.
        """
        zone = ZoneInfo(site_timezone)
        by_day: dict[str, list[tuple[datetime, float | None, str]]] = {}
        for point in modelled_hours:
            if not isinstance(point, dict):
                continue
            start = self._parse_utc(point.get("interval_start_utc"))
            if start is None:
                continue
            if local_values is not None and start in local_values:
                value, source_class = local_values[start], local_source_class
            else:
                raw = point.get("value_mm")
                try:
                    value = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    value = None
                source = point.get("source")
                source_class = source.get("source_class", "unknown") if isinstance(source, dict) else "unknown"
            by_day.setdefault(start.astimezone(zone).date().isoformat(), []).append(
                (start, value, source_class)
            )
        daily: list[dict[str, Any]] = []
        for local_date, values in sorted(by_day.items()):
            starts = [item[0] for item in values]
            local_start = datetime.fromisoformat(local_date).replace(tzinfo=zone)
            local_end = (local_start + timedelta(days=1)).astimezone(zone)
            expected = int((local_end.astimezone(timezone.utc) - local_start.astimezone(timezone.utc)).total_seconds() // 3600)
            available = [item[1] for item in values if item[1] is not None]
            partial = len(starts) != expected
            daily.append({
                "local_date": local_date,
                "range_start_utc": min(starts).isoformat(),
                "range_end_utc": (max(starts) + timedelta(hours=1)).isoformat(),
                "partial": partial,
                "actual": {
                    "sum": sum(available) if len(available) == len(starts) else None,
                    "coverage_ratio": len(available) / len(starts) if starts else 0.0,
                    "expected_hours": len(starts),
                    "available_hours": len(available),
                    "source_class": (
                        "mixed" if len({item[2] for item in values if item[1] is not None}) > 1
                        else next((item[2] for item in values if item[1] is not None), None)
                    ),
                },
            })
        return daily

    def _with_modelled_daily(self, comparison: dict[str, Any]) -> dict[str, Any]:
        """Remove API-only raw hours after deriving compact model daily data."""
        result = self._normalize_comparison(comparison) if comparison else comparison
        if not result:
            return result
        hourly = result.pop("hourly_modelled_actual", [])
        timezone_name = result.get("site_timezone")
        if (
            result.get("days") in (7, 30, 90)
            and isinstance(hourly, list)
            and isinstance(timezone_name, str)
        ):
            result["daily_incremental"] = self._daily_from_hourly(hourly, timezone_name)
        # Keep the exact API cumulative cohort under its private key until
        # `_attach_daily_reference` has joined it to the optional local-aware
        # actual curve. Moving it to `daily` here would discard those exact
        # references and accidentally pair cumulative actuals with ordinary
        # per-day percentiles.
        return result

    @staticmethod
    def _attach_daily_reference(
        comparison: dict[str, Any], daily_profiles: dict[str, Any], variable: str,
        hourly_profiles: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a cumulative daily curve without summing percentiles.

        New API payloads carry the exact cumulative analogue distribution.
        HA rebuilds just the actual curve after replacing liquid-rain hours
        with the configured local LTS source.  Older API payloads retain the
        former daily-normal fallback, but never fabricate percentile bands.
        """
        if not comparison:
            return comparison
        profile = next(
            (
                item for item in daily_profiles.get("items", [])
                if item.get("variable") == variable
            ),
            {},
        )
        reference_by_date = {
            str(point.get("valid_on")): {
                "mean": point.get("mean"),
                "p10": point.get("p10"),
                "p50": point.get("median"),
                "p90": point.get("p90"),
                "sample_count": point.get("sample_count"),
                "coverage_ratio": point.get("coverage_ratio"),
                "method": "daily_profile_1970_2025",
            }
            for point in profile.get("daily", [])
        }
        has_exact_daily_contract = isinstance(comparison.get("daily_reference"), list)
        exact_reference_by_bounds = {
            (start_utc.isoformat(), end_utc.isoformat()): metric
            for (start, end), metric in normalize_daily_references(
                comparison.get("daily_reference") or []
            ).items()
            if (start_utc := NhzClimateCoordinator._parse_utc(start)) is not None
            and (end_utc := NhzClimateCoordinator._parse_utc(end)) is not None
        }
        hourly_profile = next(
            (
                item for item in hourly_profiles.get("items", [])
                if item.get("variable") == variable
            ),
            {},
        )

        def partial_reference(point: dict[str, Any]) -> dict[str, Any] | None:
            start = NhzClimateCoordinator._parse_utc(point.get("range_start_utc"))
            end = NhzClimateCoordinator._parse_utc(point.get("range_end_utc"))
            if not start or not end:
                return None
            means = []
            for hour in hourly_profile.get("hourly", []):
                valid_at = NhzClimateCoordinator._parse_utc(hour.get("valid_at"))
                if valid_at is not None and start <= valid_at < end:
                    value = hour.get("mean")
                    if value is None:
                        return None
                    means.append(value)
            expected = int((end - start).total_seconds() // 3600)
            if len(means) != expected:
                return None
            return {
                "mean": sum(means),
                "p10": None,
                "p50": None,
                "p90": None,
                "sample_count": None,
                "coverage_ratio": 1.0,
                "partial": True,
                # This is useful until the API supplies a cohort-preserving
                # partial-day reference, but it must never be rendered as a
                # percentile-compatible analogue distribution.
                "method": "hourly_profile_mean_sum_approximate",
            }

        result = dict(comparison)
        # The endpoint supplies this only for 7/30 days.  It is intentionally
        # removed after joining so the dashboard consumes one compact `daily`
        # contract rather than two competing sources.
        result.pop("daily_reference", None)
        def selected_reference(point: dict[str, Any]) -> dict[str, Any] | None:
            exact = exact_reference_by_bounds.get((
                point.get("range_start_utc"), point.get("range_end_utc")
            ))
            if exact is not None:
                return exact
            if point.get("partial"):
                # If the API advertises exact daily references, an unmatched
                # partial interval is a coverage failure, never a silent
                # reversion to a non-cohort hourly-profile approximation.
                return None if has_exact_daily_contract else partial_reference(point)
            return reference_by_date.get(point.get("local_date"))

        incremental = [
            {
                **point,
                "reference": selected_reference(point),
            }
            for point in comparison.get("daily_incremental", [])
        ]
        api_cumulative = result.pop("daily_cumulative", [])
        if incremental:
            result["daily_incremental"] = incremental
            result["daily"] = NhzClimateCoordinator._cumulative_daily(
                incremental, api_cumulative
            )
        elif isinstance(api_cumulative, list):
            # No local source was selected.  The server-produced series is
            # already exact, compact and gap-aware.
            result["daily"] = api_cumulative
        return result

    @staticmethod
    def _cumulative_daily(
        incremental: list[dict[str, Any]], api_cumulative: list[Any],
    ) -> list[dict[str, Any]]:
        """Replace API model actuals with local-aware cumulative actuals.

        A missing daily amount breaks the actual line from that point.  The
        reference remains present so a dashboard can show the expected band,
        but a gap can never visually become a dry (zero-mm) interval.
        """
        references: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for point in api_cumulative:
            if not isinstance(point, dict):
                continue
            references[(point.get("local_start"), point.get("local_end"))] = point

        cumulative_sum = 0.0
        expected_hours = 0
        available_hours = 0
        complete = True
        provisional = False
        source_classes: set[str] = set()
        result: list[dict[str, Any]] = []
        for index, point in enumerate(incremental):
            actual = point.get("actual") if isinstance(point.get("actual"), dict) else {}
            day_expected = int(actual.get("expected_hours") or 0)
            day_available = int(actual.get("available_hours") or 0)
            day_value = actual.get("sum")
            try:
                value = float(day_value) if day_value is not None else None
            except (TypeError, ValueError):
                value = None
            expected_hours += day_expected
            available_hours += day_available
            if value is None or day_available != day_expected:
                complete = False
            elif complete:
                cumulative_sum += value
            provisional = provisional or bool(actual.get("provisional"))
            source = actual.get("source_class")
            if isinstance(source, str) and source:
                source_classes.add(source)
            reference_source = references.get((
                point.get("local_start") or point.get("range_start_utc"),
                point.get("local_end") or point.get("range_end_utc"),
            ))
            # API daily boundaries are local timestamps while reconstructed
            # local values use UTC range fields. Match the ordered point as a
            # safe fallback; both lists represent exactly the same window.
            if reference_source is None and index < len(api_cumulative):
                candidate = api_cumulative[index]
                reference_source = candidate if isinstance(candidate, dict) else None
            result.append({
                "index": index,
                "local_start": (
                    reference_source.get("local_start") if reference_source else point.get("local_date")
                ),
                "local_end": reference_source.get("local_end") if reference_source else None,
                "partial": bool(point.get("partial")),
                "reference": (
                    reference_source.get("reference") if reference_source else point.get("reference")
                ),
                "actual": {
                    "sum": cumulative_sum if complete else None,
                    "expected_hours": expected_hours,
                    "available_hours": available_hours,
                    "coverage_ratio": (
                        available_hours / expected_hours if expected_hours else 0.0
                    ),
                    "complete": complete,
                    "provisional": provisional,
                    "mixed_sources": len(source_classes) > 1,
                    "source_class": (
                        "mixed" if len(source_classes) > 1
                        else next(iter(source_classes), None)
                    ),
                },
            })
        return result

    @staticmethod
    def _normalize_comparison(comparison: dict[str, Any]) -> dict[str, Any]:
        """Expose stable, unit-neutral metric names to the token-free card."""
        result = dict(comparison)

        def reference(metric: dict[str, Any] | None) -> dict[str, Any] | None:
            if not isinstance(metric, dict):
                return metric
            return {
                **metric,
                "mean": metric.get("mean", metric.get("mean_mm")),
                "p10": metric.get("p10", metric.get("p10_mm")),
                "p50": metric.get("p50", metric.get("p50_mm")),
                "p90": metric.get("p90", metric.get("p90_mm")),
            }

        def actual(metric: dict[str, Any] | None) -> dict[str, Any] | None:
            if not isinstance(metric, dict):
                return metric
            return {**metric, "sum": metric.get("sum", metric.get("sum_mm"))}

        months = []
        for month in result.get("months") or []:
            if not isinstance(month, dict):
                continue
            months.append({
                **month,
                "reference": reference(month.get("reference")),
                "modelled_actual": actual(month.get("modelled_actual")),
            })
        result["months"] = months
        cumulative_daily = []
        for point in result.get("daily_cumulative") or []:
            if not isinstance(point, dict):
                continue
            cumulative_daily.append({
                **point,
                "reference": reference(point.get("reference")),
                "modelled_actual": actual(point.get("modelled_actual")),
            })
        if cumulative_daily:
            result["daily_cumulative"] = cumulative_daily
        total = result.get("total", result.get("rolling_365"))
        if isinstance(total, dict):
            result["total"] = {
                **total,
                "reference": reference(total.get("reference")),
                "modelled_actual": actual(total.get("modelled_actual")),
            }
        return result

    async def _async_recent_local_actual(
        self, variable: str, site_timezone: str
    ) -> dict[str, Any] | None:
        end_utc = last_completed_hour(datetime.now(timezone.utc), site_timezone)
        return await self._async_local_statistics(
            variable, end_utc - timedelta(hours=24), end_utc
        )

    async def _async_current_day_temperature_actual(
        self, site_timezone: str,
    ) -> dict[str, Any] | None:
        """Return complete recorded hours from this local midnight onward."""
        end_utc = last_completed_hour(datetime.now(timezone.utc), site_timezone)
        zone = ZoneInfo(site_timezone)
        local_today = datetime.now(zone).date()
        start_utc = datetime.combine(
            local_today, datetime.min.time(), tzinfo=zone
        ).astimezone(timezone.utc)
        if end_utc <= start_utc:
            return {
                "hourly": [],
                "complete": True,
                "coverage_ratio": 1.0,
                "source_entity": self._local_source_entity(TEMPERATURE_VARIABLE),
            }
        return await self._async_local_statistics(
            TEMPERATURE_VARIABLE, start_utc, end_utc
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            (
                latest, profile, profile_all, profiles, profiles_all,
                daily_profiles, daily_profiles_all,
                seven_day_profiles, seven_day_profiles_all, hourly, daily,
                *comparison_responses,
            ) = await asyncio.gather(
                self.api.latest(self.site, self.dataset, self.variables),
                self._async_temperature_profile(PRIMARY_BASELINE),
                self._async_temperature_profile(SECONDARY_BASELINE),
                self._async_profiles(PRIMARY_BASELINE),
                self._async_profiles(SECONDARY_BASELINE),
                self._async_daily_profiles(PRIMARY_BASELINE),
                self._async_daily_profiles(SECONDARY_BASELINE),
                self._async_daily_profiles(PRIMARY_BASELINE, window_days=7),
                self._async_daily_profiles(SECONDARY_BASELINE, window_days=7),
                self._async_forecast("hourly"),
                self._async_forecast("daily"),
                *(self._async_precipitation_comparison(variable, days)
                  for variable in (RAIN_VARIABLE, "snowfall", "precipitation")
                  for days in MONTHLY_COMPARISON_WINDOWS),
            )
            latest["temperature_profile"] = profile
            latest["temperature_profile_all"] = profile_all
            latest["profiles"] = profiles
            latest["profiles_all"] = profiles_all
            latest["daily_profiles"] = daily_profiles
            latest["daily_profiles_all"] = daily_profiles_all
            latest["seven_day_profiles"] = seven_day_profiles
            latest["seven_day_profiles_all"] = seven_day_profiles_all
            latest["hourly_forecast"] = self._merge_hourly_forecast(hourly)
            latest["daily_forecast"] = daily
            latest["forecast_entity"] = self.forecast_entity
            by_variable = {
                variable: comparison_responses[index * len(MONTHLY_COMPARISON_WINDOWS):
                                               (index + 1) * len(MONTHLY_COMPARISON_WINDOWS)]
                for index, variable in enumerate((RAIN_VARIABLE, "snowfall", "precipitation"))
            }
            rain_comparisons = await asyncio.gather(
                *(self._with_local_rain_actual(item)
                  for item in by_variable[RAIN_VARIABLE])
            )
            precipitation_comparisons = await asyncio.gather(
                *(
                    self._with_local_rain_actual(
                        comparison,
                        modelled_liquid_comparison=liquid_comparison,
                    )
                    for comparison, liquid_comparison in zip(
                        by_variable["precipitation"], by_variable[RAIN_VARIABLE]
                    )
                )
            )
            latest["monthly_comparisons"] = {
                RAIN_VARIABLE: {
                    f"{days}d": self._attach_daily_reference(
                        comparison, daily_profiles, RAIN_VARIABLE, profiles
                    )
                    for days, comparison in zip(MONTHLY_COMPARISON_WINDOWS, rain_comparisons)
                    if comparison
                },
                "precipitation": {
                    f"{days}d": self._attach_daily_reference(
                        comparison, daily_profiles,
                        "precipitation", profiles,
                    )
                    for days, comparison in zip(
                        MONTHLY_COMPARISON_WINDOWS, precipitation_comparisons
                    )
                    if comparison
                },
                "snowfall": {
                    f"{days}d": self._attach_daily_reference(
                        self._with_modelled_daily(comparison), daily_profiles,
                        "snowfall", profiles,
                    )
                    for days, comparison in zip(
                        MONTHLY_COMPARISON_WINDOWS, by_variable["snowfall"]
                    )
                    if comparison
                },
            }
            site_timezone = (
                profile.get("timezone")
                or profiles.get("timezone")
                or by_variable[RAIN_VARIABLE][-1].get("site_timezone")
                or "UTC"
            )
            recent_local = await asyncio.gather(
                self._async_recent_local_actual(TEMPERATURE_VARIABLE, site_timezone),
                self._async_recent_local_actual(RAIN_VARIABLE, site_timezone),
                self._async_current_day_temperature_actual(site_timezone),
            )
            latest["local_hourly_actual"] = {
                variable: segment
                for variable, segment in zip((TEMPERATURE_VARIABLE, RAIN_VARIABLE), recent_local)
                if segment is not None
            }
            current_day_temperature = recent_local[2]
            current_temperature: float | None = None
            current_temperature_state = self.hass.states.get(
                self._local_source_entity(TEMPERATURE_VARIABLE)
            )
            if current_temperature_state is not None:
                try:
                    raw_temperature = float(current_temperature_state.state)
                    unit = str(
                        current_temperature_state.attributes.get(
                            "unit_of_measurement", ""
                        )
                    )
                    if unit == "°C":
                        current_temperature = raw_temperature
                    elif unit == "°F":
                        current_temperature = (raw_temperature - 32.0) * 5.0 / 9.0
                    elif unit == "K":
                        current_temperature = raw_temperature - 273.15
                except (TypeError, ValueError):
                    current_temperature = None
            latest["temperature_calendar_day_anomaly"] = calendar_day_temperature_anomaly(
                now=datetime.now(timezone.utc),
                site_timezone=site_timezone,
                hourly_normal=profile.get("hourly", []),
                local_actual=current_day_temperature,
                hourly_forecast=latest["hourly_forecast"],
                current_observation=current_temperature,
            )
            return latest
        except NhzClimateError as exc:
            raise UpdateFailed(f"Unable to update NHZ Climate: {exc}") from exc
