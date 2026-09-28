"""Shared diagnostics polling for Hermes Agent."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import HermesApiClient, HermesAuthError, HermesConnectionError
from .const import (
    DIAGNOSTICS_SCAN_INTERVAL_SECONDS,
    ERROR_CATEGORY_AUTH,
    ERROR_CATEGORY_UNREACHABLE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class HermesDiagnosticsData:
    """Whitelisted diagnostic snapshot; never stores raw API payloads."""

    connected: bool
    health_available: bool
    health_status: str | None
    latency_ms: int | None
    last_successful_connection: datetime | None
    error_category: str | None


def _offline(
    previous: HermesDiagnosticsData | None,
    error_category: str | None,
) -> HermesDiagnosticsData:
    return HermesDiagnosticsData(
        connected=False,
        health_available=False,
        health_status=None,
        latency_ms=None,
        last_successful_connection=(
            previous.last_successful_connection if previous else None
        ),
        error_category=error_category,
    )


def _offline_for_exception(
    previous: HermesDiagnosticsData | None,
    err: Exception,
) -> HermesDiagnosticsData:
    if isinstance(err, HermesAuthError):
        category = ERROR_CATEGORY_AUTH
    elif isinstance(err, HermesConnectionError):
        category = getattr(err, "error_category", ERROR_CATEGORY_UNREACHABLE)
    else:
        category = ERROR_CATEGORY_UNREACHABLE
    _LOGGER.debug("Hermes diagnostics connection failed: %s", category)
    return _offline(previous, category)


class HermesDiagnosticsCoordinator(DataUpdateCoordinator[HermesDiagnosticsData]):
    """Poll authenticated Hermes reachability and optional detailed health."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HermesApiClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{getattr(entry, 'title', None) or 'Hermes Agent'} diagnostics",
            update_interval=timedelta(seconds=DIAGNOSTICS_SCAN_INTERVAL_SECONDS),
            config_entry=entry,
        )
        self.entry = entry
        self.client = client
        self.data = _offline(None, None)

    async def _async_update_data(self) -> HermesDiagnosticsData:
        previous = self.data if isinstance(self.data, HermesDiagnosticsData) else None
        started = time.monotonic()
        try:
            await self.client.async_check_connection()
        except Exception as err:
            return _offline_for_exception(previous, err)

        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        try:
            detailed = await self.client.async_get_detailed_health()
        except Exception as err:
            return _offline_for_exception(previous, err)
        if detailed.auth_failed:
            _LOGGER.debug("Hermes diagnostics connection failed: %s", ERROR_CATEGORY_AUTH)
            return _offline(previous, ERROR_CATEGORY_AUTH)

        return HermesDiagnosticsData(
            connected=True,
            health_available=detailed.available,
            health_status=detailed.status if detailed.available else None,
            latency_ms=latency_ms,
            last_successful_connection=datetime.now(timezone.utc),
            error_category=None if detailed.available else detailed.error_category,
        )
