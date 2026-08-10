"""Config flow for Hermes Conversation."""

from __future__ import annotations

from collections.abc import Iterable
import asyncio
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
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .api import HermesApiClient, HermesAuthError, HermesConnectionError
from .compat import (
    entry_value,
    normalize_host,
    normalize_profile,
    resolve_connection_config,
    resolve_continued_conversation_mode,
)
from .const import (
    CONF_ALWAYS_SPEAK_FALLBACK,
    CONF_API_KEY,
    CONF_CONTINUED_CONVERSATION_MODE,
    CONF_CONTEXT_MAX_CHARS,
    CONF_ENABLE_SESSION_REUSE,
    CONF_EXPOSE_DEVICE_CONTEXT,
    CONF_FALLBACK_MEDIA_PLAYER,
    CONF_FALLBACK_TTS_ENGINE,
    CONF_HOST,
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_PORT,
    CONF_PROFILE,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_ALWAYS_SPEAK_FALLBACK,
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_ENABLE_SESSION_REUSE,
    DEFAULT_EXPOSE_DEVICE_CONTEXT,
    DEFAULT_FALLBACK_MEDIA_PLAYER,
    DEFAULT_FALLBACK_TTS_ENGINE,
    DEFAULT_PORT,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    DOMAIN,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODE_AUTO,
    FOLLOW_UP_MODE_OFF,
    LEGACY_CONF_INSTRUCTIONS,
)

_LOGGER = logging.getLogger(__name__)
_OPTIONS_UPDATE_LOCK_KEY = f"{DOMAIN}_options_update_lock"

_FOLLOW_UP_MODE_OPTIONS = [
    SelectOptionDict(value=FOLLOW_UP_MODE_OFF, label="Off"),
    SelectOptionDict(value=FOLLOW_UP_MODE_ALWAYS, label="Always"),
    SelectOptionDict(value=FOLLOW_UP_MODE_AUTO, label="Auto when Hermes asks a question"),
]


def _entry_title(profile: str) -> str:
    """Return a title that distinguishes non-primary profiles."""
    return f"Hermes Agent ({profile})" if profile else "Hermes Agent"


def _updated_entry_title(
    entry: ConfigEntry,
    current_profile: str,
    candidate_profile: str,
) -> str:
    """Update integration-managed titles while preserving user customizations."""
    if entry.title == _entry_title(current_profile):
        return _entry_title(candidate_profile)
    return entry.title


def _connection_identity(
    host: str,
    port: int,
    use_ssl: bool,
    profile: str,
) -> tuple[str, int, bool, str]:
    """Return the normalized fields that identify one API endpoint."""
    return (
        normalize_host(host),
        int(port),
        bool(use_ssl),
        normalize_profile(profile),
    )


def _connection_is_configured(
    entries: Iterable[ConfigEntry],
    host: str,
    port: int,
    use_ssl: bool,
    profile: str,
    *,
    exclude_entry_id: str | None = None,
) -> bool:
    """Return whether another entry already owns this API endpoint."""
    identity = _connection_identity(host, port, use_ssl, profile)
    for entry in entries:
        if entry.entry_id == exclude_entry_id:
            continue
        connection = resolve_connection_config(entry)
        if identity == _connection_identity(
            connection.host,
            connection.port,
            connection.use_ssl,
            connection.profile,
        ):
            return True
    return False


class HermesConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hermes Conversation."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return HermesConversationOptionsFlow()

    def _abort_if_connection_configured(
        self,
        host: str,
        port: int,
        use_ssl: bool,
        profile: str,
    ) -> None:
        """Abort if the same transport endpoint and profile already exists."""
        if _connection_is_configured(
            self._async_current_entries(),
            host,
            port,
            use_ssl,
            profile,
        ):
            raise AbortFlow("already_configured")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = ""
            profile = ""
            try:
                host = normalize_host(user_input[CONF_HOST])
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            try:
                profile = normalize_profile(user_input.get(CONF_PROFILE, ""))
            except ValueError:
                errors[CONF_PROFILE] = "invalid_profile"

            if not errors:
                port = user_input[CONF_PORT]
                api_key = user_input.get(CONF_API_KEY, "") or None
                use_ssl = user_input.get(CONF_USE_SSL, True)
                verify_ssl = user_input.get(CONF_VERIFY_SSL, False)
                session = async_get_clientsession(self.hass)
                client = HermesApiClient(
                    session,
                    host,
                    port,
                    api_key,
                    use_ssl=use_ssl,
                    verify_ssl=verify_ssl,
                    profile=profile,
                )

                try:
                    await client.async_check_connection()
                    self._abort_if_connection_configured(
                        host,
                        port,
                        use_ssl,
                        profile,
                    )
                    return self.async_create_entry(
                        title=_entry_title(profile),
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_API_KEY: api_key or "",
                            CONF_PROFILE: profile,
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
                    _LOGGER.exception(
                        "Unexpected error during connection validation"
                    )
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default="homeassistant.local"): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_PROFILE, default=""): str,
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
        errors: dict[str, str] = {}

        if user_input is not None:
            entry_snapshot = (
                self.config_entry,
                dict(self.config_entry.data),
                dict(self.config_entry.options),
                self.config_entry.title,
            )
            normalized_input = dict(user_input)
            if CONF_HOST in normalized_input:
                try:
                    normalized_input[CONF_HOST] = normalize_host(
                        normalized_input[CONF_HOST]
                    )
                except ValueError:
                    errors[CONF_HOST] = "invalid_host"
            if CONF_PROFILE in normalized_input:
                try:
                    normalized_input[CONF_PROFILE] = normalize_profile(
                        normalized_input[CONF_PROFILE]
                    )
                except ValueError:
                    errors[CONF_PROFILE] = "invalid_profile"

            if not errors:
                connection_keys = (
                    CONF_HOST,
                    CONF_PORT,
                    CONF_API_KEY,
                    CONF_PROFILE,
                    CONF_USE_SSL,
                    CONF_VERIFY_SSL,
                )
                new_data = {
                    key: value
                    for key, value in normalized_input.items()
                    if key in connection_keys
                }
                new_options = {
                    key: value
                    for key, value in normalized_input.items()
                    if key not in connection_keys
                }
                current_connection = resolve_connection_config(self.config_entry)
                candidate_host = new_data.get(CONF_HOST, current_connection.host)
                candidate_port = new_data.get(CONF_PORT, current_connection.port)
                candidate_api_key = (
                    new_data.get(CONF_API_KEY, current_connection.api_key) or None
                )
                candidate_use_ssl = new_data.get(
                    CONF_USE_SSL,
                    current_connection.use_ssl,
                )
                candidate_verify_ssl = new_data.get(
                    CONF_VERIFY_SSL,
                    current_connection.verify_ssl,
                )
                candidate_profile = new_data.get(
                    CONF_PROFILE,
                    current_connection.profile,
                )
                if _connection_is_configured(
                    self.hass.config_entries.async_entries(DOMAIN),
                    candidate_host,
                    candidate_port,
                    candidate_use_ssl,
                    candidate_profile,
                    exclude_entry_id=self.config_entry.entry_id,
                ):
                    errors["base"] = "already_configured"

                connection_changed = (
                    candidate_host,
                    candidate_port,
                    candidate_api_key,
                    candidate_use_ssl,
                    candidate_verify_ssl,
                    candidate_profile,
                ) != (
                    current_connection.host,
                    current_connection.port,
                    current_connection.api_key,
                    current_connection.use_ssl,
                    current_connection.verify_ssl,
                    current_connection.profile,
                )
                if not errors and connection_changed:
                    session = async_get_clientsession(self.hass)
                    client = HermesApiClient(
                        session,
                        candidate_host,
                        candidate_port,
                        candidate_api_key,
                        use_ssl=candidate_use_ssl,
                        verify_ssl=candidate_verify_ssl,
                        profile=candidate_profile,
                    )
                    try:
                        await client.async_check_connection()
                    except HermesAuthError:
                        errors["base"] = "invalid_auth"
                    except HermesConnectionError:
                        errors["base"] = "cannot_connect"
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected error validating updated connection"
                        )
                        errors["base"] = "unknown"

                if not errors:
                    update_lock = self.hass.data.setdefault(
                        _OPTIONS_UPDATE_LOCK_KEY,
                        asyncio.Lock(),
                    )
                    async with update_lock:
                        live_entry = self.hass.config_entries.async_get_entry(
                            self.config_entry.entry_id
                        )
                        if (
                            live_entry is None
                            or live_entry is not entry_snapshot[0]
                        ):
                            errors["base"] = "entry_changed"
                        else:
                            current_entry_state = (
                                dict(live_entry.data),
                                dict(live_entry.options),
                                live_entry.title,
                            )
                            if current_entry_state != entry_snapshot[1:]:
                                errors["base"] = "entry_changed"
                            elif _connection_is_configured(
                                self.hass.config_entries.async_entries(DOMAIN),
                                candidate_host,
                                candidate_port,
                                candidate_use_ssl,
                                candidate_profile,
                                exclude_entry_id=live_entry.entry_id,
                            ):
                                errors["base"] = "already_configured"
                            else:
                                self.hass.config_entries.async_update_entry(
                                    live_entry,
                                    data={**live_entry.data, **new_data},
                                    title=_updated_entry_title(
                                        live_entry,
                                        current_connection.profile,
                                        candidate_profile,
                                    ),
                                    options={
                                        **live_entry.options,
                                        **new_options,
                                    },
                                )

                if not errors:
                    return self.async_create_entry(title="", data=None)

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
                        CONF_PROFILE,
                        default=connection.profile,
                    ): str,
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
                        CONF_CONTINUED_CONVERSATION_MODE,
                        default=resolve_continued_conversation_mode(self.config_entry),
                    ): SelectSelector(
                        SelectSelectorConfig(options=_FOLLOW_UP_MODE_OPTIONS)
                    ),
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
            errors=errors,
        )
