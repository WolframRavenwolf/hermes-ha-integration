"""Diagnostic sensors for Hermes Agent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, HEALTH_STATUSES
from .entity import HermesDiagnosticsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[Any]], None],
) -> None:
    """Set up Hermes diagnostic sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            HermesHealthSensor(coordinator, entry),
            HermesApiLatencySensor(coordinator, entry),
            HermesLastSuccessfulConnectionSensor(coordinator, entry),
        ]
    )


class HermesHealthSensor(HermesDiagnosticsEntity, SensorEntity):
    """Optional authenticated /health/detailed status: ok or degraded."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(HEALTH_STATUSES)

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "health")

    @property
    def available(self) -> bool:
        data = self.diagnostics
        return bool(data and data.health_available)

    @property
    def native_value(self) -> str | None:
        data = self.diagnostics
        if data and data.health_available:
            return data.health_status
        return None


class HermesApiLatencySensor(HermesDiagnosticsEntity, SensorEntity):
    """Duration of the authenticated connection probe, not model latency."""

    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "api_latency")

    @property
    def available(self) -> bool:
        data = self.diagnostics
        return bool(data and data.connected and data.latency_ms is not None)

    @property
    def native_value(self) -> int | None:
        data = self.diagnostics
        if data and data.connected:
            return data.latency_ms
        return None


class HermesLastSuccessfulConnectionSensor(HermesDiagnosticsEntity, SensorEntity):
    """UTC timestamp of the last successful authenticated connection."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_successful_connection")

    @property
    def available(self) -> bool:
        data = self.diagnostics
        return bool(data and data.last_successful_connection is not None)

    @property
    def native_value(self) -> datetime | None:
        data = self.diagnostics
        if data:
            return data.last_successful_connection
        return None
