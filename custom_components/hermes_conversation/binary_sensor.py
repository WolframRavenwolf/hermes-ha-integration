"""Diagnostic binary sensors for Hermes Agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import HermesDiagnosticsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[Any]], None],
) -> None:
    """Set up Hermes diagnostic binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([HermesApiConnectivityBinarySensor(coordinator, entry)])


class HermesApiConnectivityBinarySensor(HermesDiagnosticsEntity, BinarySensorEntity):
    """Authenticated API reachability; off is not the same as unavailable."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "api_connectivity")

    @property
    def available(self) -> bool:
        return self.diagnostics is not None

    @property
    def is_on(self) -> bool:
        data = self.diagnostics
        return bool(data and data.connected)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.diagnostics
        if data and data.error_category:
            return {"error_category": data.error_category}
        return None
