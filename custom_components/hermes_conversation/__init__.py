"""The Hermes Conversation integration."""

from __future__ import annotations

import logging

from homeassistant.components.conversation import (
    async_set_agent,
    async_unset_agent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HermesApiClient
from .compat import entry_value, resolve_connection_config
from .const import DEFAULT_TIMEOUT, DOMAIN, LEGACY_CONF_MODEL, LEGACY_CONF_TIMEOUT
from .conversation import HermesConversationAgent

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hermes Conversation from a config entry."""
    session = async_get_clientsession(hass)

    connection = resolve_connection_config(entry)
    client = HermesApiClient(
        session=session,
        host=connection.host,
        port=connection.port,
        api_key=connection.api_key,
        use_ssl=connection.use_ssl,
        verify_ssl=connection.verify_ssl,
        model=entry_value(entry, LEGACY_CONF_MODEL, None),
        request_timeout=entry_value(entry, LEGACY_CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )

    hass.data.setdefault(DOMAIN, {})
    session_map: dict[str, dict[str, object]] = {}
    agent = HermesConversationAgent(hass, entry, client, session_map=session_map)

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "agent": agent,
        "sessions": session_map,
    }

    async_set_agent(hass, entry, agent)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Hermes Conversation set up successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    async_unset_agent(hass, entry)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN, None)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
