"""The Hermes Conversation integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HermesApiClient
from .compat import entry_value, resolve_connection_config
from .const import DEFAULT_TIMEOUT, DOMAIN, LEGACY_CONF_MODEL, LEGACY_CONF_TIMEOUT

_LOGGER = logging.getLogger(__name__)
PLATFORMS: tuple[Platform, ...] = (Platform.CONVERSATION,)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hermes Conversation from a config entry."""
    session = async_get_clientsession(hass)

    connection = resolve_connection_config(entry)
    client = HermesApiClient(
        session=session,
        host=connection.host,
        port=connection.port,
        api_key=connection.api_key,
        profile=connection.profile,
        use_ssl=connection.use_ssl,
        verify_ssl=connection.verify_ssl,
        model=entry_value(entry, LEGACY_CONF_MODEL, None),
        request_timeout=entry_value(entry, LEGACY_CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )

    hass.data.setdefault(DOMAIN, {})
    session_map: dict[str, dict[str, object]] = {}

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "sessions": session_map,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Hermes Conversation set up successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
