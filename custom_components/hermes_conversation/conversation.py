"""Conversation agent for Hermes Agent."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Any

from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
    MATCH_ALL,
)
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent, template

from .api import HermesApiClient, HermesApiError
from .compat import entry_value, resolve_continued_conversation_mode
from .const import (
    CONF_ALWAYS_SPEAK_FALLBACK,
    CONF_API_KEY,
    CONF_CONTEXT_MAX_CHARS,
    CONF_ENABLE_SESSION_REUSE,
    CONF_EXPOSE_DEVICE_CONTEXT,
    CONF_FALLBACK_MEDIA_PLAYER,
    CONF_FALLBACK_TTS_ENGINE,
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    DEFAULT_ALWAYS_SPEAK_FALLBACK,
    DEFAULT_CONTEXT_MAX_CHARS,
    DEFAULT_ENABLE_SESSION_REUSE,
    DEFAULT_EXPOSE_DEVICE_CONTEXT,
    DEFAULT_FALLBACK_MEDIA_PLAYER,
    DEFAULT_FALLBACK_TTS_ENGINE,
    DEFAULT_INCLUDE_EXPOSED_ENTITIES,
    DEFAULT_MAX_HISTORY_MESSAGES,
    DEFAULT_PROMPT,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODE_AUTO,
    LEGACY_CONF_INSTRUCTIONS,
)

_LOGGER = logging.getLogger(__name__)
_QUESTION_MARKERS = ("?", "\uFF1F")
_TRAILING_CLOSERS = "\"')]}" + "\u201d\u2019\u00bb"
_AUTO_FOLLOW_UP_PROMPT = (
    "When voice auto follow-up is active and you want the user to reply, "
    "give any needed answer first and end with one short, direct question as "
    "the final sentence. Do not add any words after the question mark."
)


def _sanitize_text_for_speech(text: str) -> str:
    """Convert markdown-ish assistant output into plain speech-friendly text."""
    if not text:
        return text

    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"```(?:[\w+-]+)?\n?(.*?)```", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^>+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+[.)]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"(\*\*|__)(.*?)\1", r"\2", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"(?<!_)_(?!\s)(.*?)(?<!\s)_(?!_)", r"\1", cleaned)
    cleaned = re.sub(r"~~(.*?)~~", r"\1", cleaned)
    cleaned = re.sub(r"\[(.*?)\]\[[^\]]*\]", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


class HermesConversationAgent(AbstractConversationAgent):
    """Hermes Agent conversation agent for Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: HermesApiClient,
        session_map: dict[str, dict[str, Any]],
    ) -> None:
        """Initialise the conversation agent."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.session_map = session_map
        # conversation_id -> list of {"role": ..., "content": ...}
        self._history: OrderedDict[str, list[dict[str, str]]] = OrderedDict()

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages (all — the LLM handles it)."""
        return MATCH_ALL

    async def async_process(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Process a conversation turn."""
        try:
            return await self._async_process_inner(user_input)
        except Exception:
            _LOGGER.exception("Unexpected error in async_process")
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "An internal error occurred. Check the logs.",
            )
            return self._build_conversation_result(
                intent_response,
                user_input.conversation_id or "default",
                continue_conversation=False,
            )

    async def _async_process_inner(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Inner processing — wrapped by async_process for error logging."""
        conv_id = user_input.conversation_id or str(uuid.uuid4())
        follow_up_mode = self._continued_conversation_mode()
        session_reuse = self._session_reuse_enabled()
        session_key = self._build_session_key(user_input, conv_id) if session_reuse else None
        session_id = self._get_active_session_id(session_key) if session_key else None

        # Resolve username from HA auth
        user_name = await self._get_user_name(user_input)

        # Build system prompt (optional — Hermes Agent has its own)
        system_prompt = self._render_system_prompt(user_name)

        # Append extra system prompt from HA voice pipeline if present
        extra = getattr(user_input, "extra_system_prompt", None)
        if extra:
            system_prompt = (system_prompt + "\n\n" + extra) if system_prompt else extra

        # Append origin context when requested
        if self._device_context_enabled():
            context_lines = self._build_origin_context(user_input)
            if context_lines:
                origin_block = "Origin context:\n" + "\n".join(f"- {line}" for line in context_lines)
                system_prompt = (system_prompt + "\n\n" + origin_block) if system_prompt else origin_block

        system_prompt = self._append_auto_follow_up_prompt(
            system_prompt,
            follow_up_mode,
        )

        if session_reuse:
            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_input.text})
        else:
            history = self._history.setdefault(conv_id, [])
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.extend(history)
            messages.append({"role": "user", "content": user_input.text})

        try:
            response_text = await self._get_response(messages, session_id=session_id)
            spoken_text = _sanitize_text_for_speech(response_text)
        except HermesApiError as err:
            _LOGGER.error("Hermes API error: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Error communicating with Hermes Agent: {err}",
            )
            return self._build_conversation_result(
                intent_response,
                conv_id,
                continue_conversation=False,
            )

        if session_key:
            self._remember_session(session_key, self.client.last_session_id)

        if not session_reuse:
            history = self._history.setdefault(conv_id, [])
            history.append({"role": "user", "content": user_input.text})
            history.append({"role": "assistant", "content": spoken_text})
            self._history.move_to_end(conv_id)

            while len(history) > DEFAULT_MAX_HISTORY_MESSAGES:
                history.pop(0)
                if history and history[0]["role"] == "assistant":
                    history.pop(0)

            while len(self._history) > 50:
                self._history.popitem(last=False)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(spoken_text)
        await self._async_speak_fallback(spoken_text, user_input)
        continue_conversation = self._should_continue_conversation(
            follow_up_mode,
            spoken_text,
        )

        return self._build_conversation_result(
            intent_response,
            conv_id,
            continue_conversation=continue_conversation,
        )

    async def _get_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> str:
        """Get a response from the API.

        Home Assistant voice needs clean final speech, not intermediate stream events.
        Hermes's streaming endpoint can legitimately include tool-progress deltas for chat
        UIs, which then get spoken out loud here. Use the non-streaming endpoint so the
        voice pipeline only receives the final assistant text.
        """
        result = await self.client.async_send_message(messages, session_id=session_id)
        return result.text

    async def _get_user_name(self, user_input: ConversationInput) -> str:
        """Resolve the display name of the user from HA auth."""
        try:
            context = getattr(user_input, "context", None)
            if context is None:
                return "the user"
            user_id = getattr(context, "user_id", None)
            if not user_id:
                return "the user"
            user = await self.hass.auth.async_get_user(user_id)
            if user and user.name:
                return user.name
        except Exception:
            _LOGGER.debug("Could not resolve username", exc_info=True)
        return "the user"

    def _render_system_prompt(self, user_name: str) -> str:
        """Render the system prompt template with HA context."""
        prompt_template = entry_value(
            self.entry,
            CONF_PROMPT,
            DEFAULT_PROMPT,
            legacy_keys=(LEGACY_CONF_INSTRUCTIONS,),
        )
        if not prompt_template:
            return ""

        variables: dict[str, Any] = {
            "ha_name": self.hass.config.location_name,
            "user_name": user_name,
        }

        include_entities = entry_value(
            self.entry,
            CONF_INCLUDE_EXPOSED_ENTITIES,
            DEFAULT_INCLUDE_EXPOSED_ENTITIES,
        )
        if include_entities:
            variables["exposed_entities"] = self._get_exposed_entities()
        else:
            variables["exposed_entities"] = []

        try:
            tpl = template.Template(prompt_template, self.hass)
            return tpl.async_render(variables)
        except template.TemplateError as err:
            _LOGGER.warning("System prompt template error: %s", err)
            return prompt_template

    def _append_auto_follow_up_prompt(
        self,
        system_prompt: str,
        follow_up_mode: str,
    ) -> str:
        """Append guidance that makes auto follow-up turns cleaner."""
        if follow_up_mode != FOLLOW_UP_MODE_AUTO:
            return system_prompt

        if system_prompt:
            return f"{system_prompt}\n\n{_AUTO_FOLLOW_UP_PROMPT}"

        return _AUTO_FOLLOW_UP_PROMPT

    def _get_exposed_entities(self) -> list[dict[str, str]]:
        """Get a list of entities exposed to the conversation agent."""
        max_chars = entry_value(
            self.entry,
            CONF_CONTEXT_MAX_CHARS,
            DEFAULT_CONTEXT_MAX_CHARS,
        )
        entities: list[dict[str, str]] = []
        total_chars = 0

        for state in self.hass.states.async_all():
            try:
                if not async_should_expose(
                    self.hass, "conversation", state.entity_id
                ):
                    continue
            except Exception:
                continue

            entity_info = {
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "state": str(state.state),
            }

            line = f"- {entity_info['entity_id']} ({entity_info['name']}): {entity_info['state']}"
            total_chars += len(line) + 1
            if total_chars > max_chars:
                break
            entities.append(entity_info)

        return entities

    def _continued_conversation_mode(self) -> str:
        return resolve_continued_conversation_mode(self.entry)

    def _should_continue_conversation(
        self,
        follow_up_mode: str,
        response_text: str,
    ) -> bool:
        """Return whether the voice pipeline should keep listening."""
        if follow_up_mode == FOLLOW_UP_MODE_ALWAYS:
            return True
        if follow_up_mode != FOLLOW_UP_MODE_AUTO:
            return False
        return self._response_invites_follow_up(response_text)

    def _response_invites_follow_up(self, response_text: str) -> bool:
        """Detect when Hermes ended with a question worth keeping HA open for."""
        stripped_text = response_text.strip().rstrip(_TRAILING_CLOSERS)
        return stripped_text.endswith(_QUESTION_MARKERS)

    def _session_reuse_enabled(self) -> bool:
        if not bool(
            entry_value(
                self.entry,
                CONF_ENABLE_SESSION_REUSE,
                DEFAULT_ENABLE_SESSION_REUSE,
            )
        ):
            return False

        # Hermes Agent deliberately rejects X-Hermes-Session-Id continuation
        # unless the API server is protected by API-key authentication.  If the
        # user configured this integration without an API key, do not send the
        # session header; otherwise the second voice turn would fail with 403.
        api_key = entry_value(
            self.entry,
            CONF_API_KEY,
            "",
            prefer_options=False,
        )
        if not api_key:
            _LOGGER.debug("Hermes session reuse disabled because no API key is configured")
            return False

        return True

    def _session_timeout_seconds(self) -> int:
        try:
            return max(
                0,
                int(
                    entry_value(
                        self.entry,
                        CONF_SESSION_TIMEOUT_SECONDS,
                        DEFAULT_SESSION_TIMEOUT_SECONDS,
                    )
                ),
            )
        except (TypeError, ValueError):
            return DEFAULT_SESSION_TIMEOUT_SECONDS

    def _device_context_enabled(self) -> bool:
        return bool(
            entry_value(
                self.entry,
                CONF_EXPOSE_DEVICE_CONTEXT,
                DEFAULT_EXPOSE_DEVICE_CONTEXT,
            )
        )

    def _build_session_key(self, user_input: ConversationInput, conversation_id: str) -> str:
        device_id = getattr(user_input, "device_id", None)
        satellite_id = getattr(user_input, "satellite_id", None)
        if device_id:
            return f"device:{device_id}"
        if satellite_id:
            return f"satellite:{satellite_id}"
        return f"conversation:{conversation_id}"

    def _get_active_session_id(self, session_key: str | None) -> str | None:
        if not session_key:
            return None

        record = self.session_map.get(session_key)
        if not record:
            return None

        session_id = record.get("session_id")
        last_used_at = float(record.get("last_used_at", 0) or 0)
        timeout_seconds = self._session_timeout_seconds()
        if timeout_seconds and (time.time() - last_used_at) > timeout_seconds:
            self.session_map.pop(session_key, None)
            return None

        if isinstance(session_id, str) and session_id.strip():
            return session_id
        return None

    def _remember_session(self, session_key: str, session_id: str | None) -> None:
        if not session_id:
            self.session_map.pop(session_key, None)
            return

        self.session_map[session_key] = {
            "session_id": session_id,
            "last_used_at": time.time(),
        }

    def _build_origin_context(self, user_input: ConversationInput) -> list[str]:
        lines: list[str] = []
        language = getattr(user_input, "language", None)
        device_id = getattr(user_input, "device_id", None)
        satellite_id = getattr(user_input, "satellite_id", None)

        if language:
            lines.append(f"Language: {language}")
        if device_id:
            lines.extend(self._describe_device(device_id))
        if satellite_id:
            lines.extend(self._describe_satellite(satellite_id))
        return lines

    def _describe_device(self, device_id: str) -> list[str]:
        device_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        device = device_reg.async_get(device_id)
        if not device:
            return [f"Home Assistant device_id: {device_id}"]

        lines = [f"Origin device: {device.name_by_user or device.name or device_id}"]
        if device.area_id:
            area = area_reg.async_get_area(device.area_id)
            if area:
                lines.append(f"Origin area: {area.name}")
        return lines

    def _describe_satellite(self, satellite_id: str) -> list[str]:
        state = self.hass.states.get(satellite_id) if "." in satellite_id else None
        if not state:
            return [f"Assist satellite: {satellite_id}"]
        friendly_name = state.attributes.get("friendly_name", satellite_id)
        return [f"Assist satellite: {friendly_name} ({satellite_id})"]

    def _build_conversation_result(
        self,
        intent_response: intent.IntentResponse,
        conversation_id: str,
        *,
        continue_conversation: bool = False,
    ) -> ConversationResult:
        """Build a conversation result, preserving compatibility with older HA."""
        try:
            return ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
                continue_conversation=continue_conversation,
            )
        except TypeError:
            return ConversationResult(
                response=intent_response,
                conversation_id=conversation_id,
            )

    async def _async_speak_fallback(
        self, text: str, user_input: ConversationInput
    ) -> None:
        if not text.strip():
            return

        if not (
            getattr(user_input, "device_id", None)
            or getattr(user_input, "satellite_id", None)
        ):
            return

        speak_fallback = entry_value(
            self.entry,
            CONF_ALWAYS_SPEAK_FALLBACK,
            DEFAULT_ALWAYS_SPEAK_FALLBACK,
        )
        if not speak_fallback:
            return

        media_player_entity = entry_value(
            self.entry,
            CONF_FALLBACK_MEDIA_PLAYER,
            DEFAULT_FALLBACK_MEDIA_PLAYER,
        )
        tts_entity = entry_value(
            self.entry,
            CONF_FALLBACK_TTS_ENGINE,
            DEFAULT_FALLBACK_TTS_ENGINE,
        )
        if not media_player_entity or not tts_entity:
            return

        service_data = {
            "entity_id": tts_entity,
            "media_player_entity_id": media_player_entity,
            "message": text,
            "cache": True,
        }

        language = getattr(user_input, "language", None)
        if language:
            service_data["language"] = language

        try:
            await self.hass.services.async_call(
                "tts",
                "speak",
                service_data,
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Fallback TTS failed: %s", err)
