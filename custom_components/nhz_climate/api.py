from __future__ import annotations

from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientSession


class NhzClimateError(Exception):
    """Base API error."""


class NhzClimateAuthError(NhzClimateError):
    """Authentication failed."""


class NhzClimateConnectionError(NhzClimateError):
    """Connection failed."""


class NhzClimateDataUnavailableError(NhzClimateError):
    """Requested curated product has not been generated yet."""


class NhzClimateApi:
    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "home-assistant-nhz-climate/0.6.0",
        }

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self._session.get(
                f"{self._base_url}{path}",
                params=params,
                headers=self._headers,
                timeout=90,
            ) as response:
                if response.status == 401:
                    raise NhzClimateAuthError("Invalid API token")
                if response.status == 404:
                    raise NhzClimateDataUnavailableError("Climate product unavailable")
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (NhzClimateAuthError, NhzClimateDataUnavailableError):
            raise
        except (ClientError, TimeoutError, ValueError) as exc:
            raise NhzClimateConnectionError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise NhzClimateConnectionError("API response is not an object")
        return payload

    async def sites(self) -> list[dict[str, Any]]:
        payload = await self._get("/v1/sites")
        items = payload.get("items")
        if not isinstance(items, list):
            raise NhzClimateConnectionError("API response has no sites list")
        return items

    async def latest(
        self, site: str, dataset: str, variables: tuple[str, ...]
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/sites/{quote(site, safe='')}/latest",
            {"dataset": dataset, "variables": ",".join(variables)},
        )

    async def temperature_profile(
        self, site: str, dataset: str, baseline: str = "1991-2020"
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/sites/{quote(site, safe='')}/temperature-profile",
            {"dataset": dataset, "baseline": baseline},
        )

    async def profiles(
        self,
        site: str,
        dataset: str,
        variables: tuple[str, ...],
        baseline: str,
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/sites/{quote(site, safe='')}/profiles",
            {
                "dataset": dataset,
                "baseline": baseline,
                "variables": ",".join(variables),
            },
        )

    async def daily_profiles(
        self,
        site: str,
        dataset: str,
        variables: tuple[str, ...],
        baseline: str,
        days: int = 366,
        window_days: int = 1,
    ) -> dict[str, Any]:
        return await self._get(
            f"/v1/sites/{quote(site, safe='')}/daily-profiles",
            {
                "dataset": dataset,
                "baseline": baseline,
                "variables": ",".join(variables),
                "days": str(days),
                "window_days": str(window_days),
            },
        )

    async def precipitation_comparison(
        self,
        site: str,
        dataset: str,
        variable: str = "rain",
        days: int = 365,
        baseline: str = "1970-2025",
        include_hourly: bool = False,
    ) -> dict[str, Any]:
        """Return a server-calculated historic precipitation comparison.

        The API deliberately returns aggregates and source provenance, never
        reference raw hours.  This keeps the browser token-free and prevents
        it from manufacturing percentiles by summing hourly percentiles.
        """
        return await self._get(
            f"/v1/sites/{quote(site, safe='')}/precipitation-comparison",
            {
                "dataset": dataset,
                "variable": variable,
                "days": str(days),
                "baseline": baseline,
                "include_hourly": str(include_hourly).lower(),
            },
        )
