"""Config flow for Hermes Conversation."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .api import HermesApiClient, HermesAuthError, HermesConnectionError
from .compat import entry_value, resolve_connection_config
from .const import (
    CONF_ALWAYS_SPEAK_FALLBACK,
    CONF_API_KEY,
    CONF_CONTEXT_MAX_CHARS,
    CONF_ENABLE_CONTINUED_CONVERSATION,
    CONF_ENABLE_SESSION_REUSE,
    CONF_EXPOSE_DEVICE_CONTEXT,
    CONF_FALLBACK_MEDIA_PLAYER,
    CONF_FALLBACK_TTS_ENGINE,
    CONF_HOST,
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_PORT,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_ALWAYS_SPEAK_FALLBACK,
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_ENABLE_CONTINUED_CONVERSATION,
    DEFAULT_ENABLE_SESSION_REUSE,
    DEFAULT_EXPOSE_DEVICE_CONTEXT,
    DEFAULT_FALLBACK_MEDIA_PLAYER,
    DEFAULT_FALLBACK_TTS_ENGINE,
    DEFAULT_PORT,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    DOMAIN,
    LEGACY_CONF_INSTRUCTIONS,
)

_LOGGER = logging.getLogger(__name__)


class HermesConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hermes Conversation."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return HermesConversationOptionsFlow()

    def _abort_if_host_port_configured(self, host: str, port: int) -> None:
        """Abort if an entry with the same host:port already exists."""
        for entry in self._async_current_entries():
            connection = resolve_connection_config(entry)
            if connection.host == host and connection.port == port:
                raise AbortFlow("already_configured")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            api_key = user_input.get(CONF_API_KEY, "") or None
            use_ssl = user_input.get(CONF_USE_SSL, True)
            verify_ssl = user_input.get(CONF_VERIFY_SSL, False)

            session = async_get_clientsession(self.hass)
            client = HermesApiClient(
                session, host, port, api_key,
                use_ssl=use_ssl, verify_ssl=verify_ssl,
            )

            try:
                await client.async_check_connection()
                self._abort_if_host_port_configured(host, port)
                return self.async_create_entry(
                    title="Hermes Agent",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_API_KEY: api_key or "",
                        CONF_USE_SSL: use_ssl,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )
            except HermesAuthError:
                errors["base"] = "invalid_auth"
            except HermesConnectionError:
                errors["base"] = "cannot_connect"
            except AbortFlow:
                raise
            except Exception:
                _LOGGER.exception("Unexpected error during connection validation")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default="homeassistant.local"): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_API_KEY, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Optional(CONF_USE_SSL, default=True): bool,
                    vol.Optional(CONF_VERIFY_SSL, default=False): bool,
                }
            ),
            errors=errors,
        )


class HermesConversationOptionsFlow(OptionsFlow):
    """Handle options for Hermes Conversation."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Manage the options."""
        if user_input is not None:
            new_data = {}
            new_options = {}
            for key, value in user_input.items():
                if key in (CONF_HOST, CONF_PORT, CONF_API_KEY, CONF_USE_SSL, CONF_VERIFY_SSL):
                    new_data[key] = value
                else:
                    new_options[key] = value

            if new_data:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, **new_data}
                )

            return self.async_create_entry(title="", data=new_options)

        connection = resolve_connection_config(self.config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=connection.host,
                    ): str,
                    vol.Required(
                        CONF_PORT,
                        default=connection.port,
                    ): int,
                    vol.Optional(
                        CONF_API_KEY,
                        default=connection.api_key or "",
                    ): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Optional(
                        CONF_USE_SSL,
                        default=connection.use_ssl,
                    ): bool,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=connection.verify_ssl,
                    ): bool,
                    vol.Optional(
                        CONF_PROMPT,
                        default=entry_value(
                            self.config_entry,
                            CONF_PROMPT,
                            DEFAULT_PROMPT,
                            legacy_keys=(LEGACY_CONF_INSTRUCTIONS,),
                        ),
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                    vol.Optional(
                        CONF_INCLUDE_EXPOSED_ENTITIES,
                        default=entry_value(
                            self.config_entry,
                            CONF_INCLUDE_EXPOSED_ENTITIES,
                            False,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_CONTEXT_MAX_CHARS,
                        default=entry_value(
                            self.config_entry,
                            CONF_CONTEXT_MAX_CHARS,
                            DEFAULT_CONTEXT_MAX_CHARS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=200000)),
                    vol.Optional(
                        CONF_ENABLE_CONTINUED_CONVERSATION,
                        default=entry_value(
                            self.config_entry,
                            CONF_ENABLE_CONTINUED_CONVERSATION,
                            DEFAULT_ENABLE_CONTINUED_CONVERSATION,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_SESSION_REUSE,
                        default=entry_value(
                            self.config_entry,
                            CONF_ENABLE_SESSION_REUSE,
                            DEFAULT_ENABLE_SESSION_REUSE,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_SESSION_TIMEOUT_SECONDS,
                        default=entry_value(
                            self.config_entry,
                            CONF_SESSION_TIMEOUT_SECONDS,
                            DEFAULT_SESSION_TIMEOUT_SECONDS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
                    vol.Optional(
                        CONF_EXPOSE_DEVICE_CONTEXT,
                        default=entry_value(
                            self.config_entry,
                            CONF_EXPOSE_DEVICE_CONTEXT,
                            DEFAULT_EXPOSE_DEVICE_CONTEXT,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_ALWAYS_SPEAK_FALLBACK,
                        default=entry_value(
                            self.config_entry,
                            CONF_ALWAYS_SPEAK_FALLBACK,
                            DEFAULT_ALWAYS_SPEAK_FALLBACK,
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_FALLBACK_MEDIA_PLAYER,
                        default=entry_value(
                            self.config_entry,
                            CONF_FALLBACK_MEDIA_PLAYER,
                            DEFAULT_FALLBACK_MEDIA_PLAYER,
                        ),
                    ): str,
                    vol.Optional(
                        CONF_FALLBACK_TTS_ENGINE,
                        default=entry_value(
                            self.config_entry,
                            CONF_FALLBACK_TTS_ENGINE,
                            DEFAULT_FALLBACK_TTS_ENGINE,
                        ),
                    ): str,
                }
            ),
        )
