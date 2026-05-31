"""Conversation agent for Hermes Agent."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
    MATCH_ALL,
    async_set_agent,
    async_unset_agent,
)
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import intent, template

from .api import HermesApiClient, HermesApiError, HermesStreamSetupError
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
    DOMAIN,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODE_AUTO,
    LEGACY_CONF_INSTRUCTIONS,
)

try:
    from homeassistant.components.conversation import ChatLog, async_get_chat_log
    from homeassistant.helpers.chat_session import async_get_chat_session
except ImportError:
    ChatLog = Any
    async_get_chat_log = None
    async_get_chat_session = None

_LOGGER = logging.getLogger(__name__)
_MAX_CACHED_CONVERSATIONS = 50
_QUESTION_MARKERS = ("?", "\uFF1F")
_TRAILING_CLOSERS = "\"')]}" + "\u201d\u2019\u00bb"
_AUTO_FOLLOW_UP_PROMPT = (
    "When voice auto follow-up is active and you want the user to reply, "
    "give any needed answer first and end with one short, direct question as "
    "the final sentence. Do not add any words after the question mark."
)

_UNSAFE_SPEECH_TAG_PATTERN = (
    "think|analysis|tool_call|tool_calls|function_call|function_calls|"
    "tool_result|tool_results"
)
_UNSAFE_SPEECH_BLOCK_RE = re.compile(
    rf"<\s*({_UNSAFE_SPEECH_TAG_PATTERN})\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE_SPEECH_OPEN_RE = re.compile(
    rf"<\s*(?:{_UNSAFE_SPEECH_TAG_PATTERN})\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE_SPEECH_TAG_RE = re.compile(
    rf"<\s*/?\s*(?:{_UNSAFE_SPEECH_TAG_PATTERN})\b[^>]*>",
    re.IGNORECASE,
)
_UNSAFE_SPEECH_START_RE = re.compile(
    rf"<\s*({_UNSAFE_SPEECH_TAG_PATTERN})\b[^>]*>",
    re.IGNORECASE,
)
_MAX_UNSAFE_CLOSE_TAG_LENGTH = 80


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[list[Any]], None],
) -> None:
    """Set up the Hermes conversation entity."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HermesConversationAgent(
                hass,
                entry,
                entry_data["client"],
                session_map=entry_data["sessions"],
            )
        ]
    )


class _UnsafeSpeechStreamFilter:
    """Incrementally drop hidden reasoning and tool markup from streamed speech."""

    def __init__(self) -> None:
        self._buffer = ""
        self._discard_until_tag: str | None = None

    def feed(self, text: str) -> str:
        """Add a stream delta and return the safe text that can be emitted now."""
        if not text:
            return ""
        self._buffer += text
        return self._drain(final=False)

    def flush(self) -> str:
        """Return any remaining safe text at end of stream."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        safe_parts: list[str] = []

        while self._buffer:
            if self._discard_until_tag:
                close_re = re.compile(
                    rf"<\s*/\s*{re.escape(self._discard_until_tag)}\s*>",
                    re.IGNORECASE,
                )
                close_match = close_re.search(self._buffer)
                if close_match is None:
                    if final:
                        self._buffer = ""
                        self._discard_until_tag = None
                    else:
                        self._buffer = self._buffer[-_MAX_UNSAFE_CLOSE_TAG_LENGTH:]
                    break

                self._buffer = self._buffer[close_match.end() :]
                self._discard_until_tag = None
                continue

            open_match = _UNSAFE_SPEECH_START_RE.search(self._buffer)
            if open_match is not None:
                safe_parts.append(self._buffer[: open_match.start()])
                tag = open_match.group(1).lower()
                close_re = re.compile(
                    rf"<\s*/\s*{re.escape(tag)}\s*>",
                    re.IGNORECASE,
                )
                close_match = close_re.search(self._buffer, open_match.end())
                if close_match is None:
                    self._buffer = self._buffer[open_match.end() :]
                    self._discard_until_tag = tag
                    if not final:
                        self._buffer = self._buffer[-_MAX_UNSAFE_CLOSE_TAG_LENGTH:]
                    else:
                        self._buffer = ""
                        self._discard_until_tag = None
                    break

                self._buffer = self._buffer[close_match.end() :]
                continue

            safe_parts.append(self._consume_safe_buffer(final=final))
            break

        return _sanitize_stream_text_for_speech("".join(safe_parts))

    def _consume_safe_buffer(self, *, final: bool) -> str:
        if final:
            safe = self._buffer
            self._buffer = ""
            return safe

        last_lt = self._buffer.rfind("<")
        if last_lt != -1 and ">" not in self._buffer[last_lt:]:
            safe = self._buffer[:last_lt]
            self._buffer = self._buffer[last_lt:]
            return safe

        safe = self._buffer
        self._buffer = ""
        return safe


def _remove_unsafe_speech_markup(text: str) -> str:
    """Remove hidden reasoning and tool-call markup before it reaches TTS."""
    cleaned = text
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _UNSAFE_SPEECH_BLOCK_RE.sub("", cleaned)
    cleaned = _UNSAFE_SPEECH_OPEN_RE.sub("", cleaned)
    return _UNSAFE_SPEECH_TAG_RE.sub("", cleaned)


def _sanitize_stream_text_for_speech(text: str) -> str:
    """Apply safe, local cleanup to a speech stream delta."""
    if not text:
        return text
    cleaned = text.replace("\r\n", "\n")
    cleaned = _remove_unsafe_speech_markup(cleaned)
    cleaned = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)
    return (
        cleaned.replace("```", "")
        .replace("`", "")
        .replace("**", "")
        .replace("__", "")
        .replace("~~", "")
    )


def _sanitize_text_for_speech(text: str) -> str:
    """Convert markdown-ish assistant output into plain speech-friendly text."""
    if not text:
        return text

    cleaned = text.replace("\r\n", "\n")
    cleaned = _remove_unsafe_speech_markup(cleaned)
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
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


class HermesConversationAgent(ConversationEntity, AbstractConversationAgent):
    """Hermes Agent conversation entity for Home Assistant."""

    _attr_should_poll = False
    _attr_supports_streaming = True

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
        self._attr_unique_id = entry.entry_id
        self._attr_name = getattr(entry, "title", None) or "Hermes Agent"
        self._attr_supported_features = ConversationEntityFeature.CONTROL
        # conversation_id -> list of {"role": ..., "content": ...}
        self._history: OrderedDict[str, list[dict[str, str]]] = OrderedDict()

    @property
    def supported_languages(self) -> list[str] | str:
        """Return supported languages (all — the LLM handles it)."""
        return MATCH_ALL

    @property
    def supports_streaming(self) -> bool:
        """Return if the entity supports streaming responses."""
        return True

    async def async_added_to_hass(self) -> None:
        """Register a legacy agent alias for older Home Assistant callers."""
        if super_added := getattr(super(), "async_added_to_hass", None):
            await super_added()
        async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Remove the legacy agent alias when Home Assistant unloads the entity."""
        async_unset_agent(self.hass, self.entry)
        if super_removed := getattr(super(), "async_will_remove_from_hass", None):
            await super_removed()

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Handle a modern Home Assistant conversation turn."""
        return await self._async_process_with_error_handling(user_input, chat_log)

    async def async_process(
        self, user_input: ConversationInput
    ) -> ConversationResult:
        """Process a conversation turn."""
        return await self._async_process_with_error_handling(user_input)

    async def _async_process_with_error_handling(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog | None = None,
    ) -> ConversationResult:
        """Process a conversation turn and convert unexpected errors."""
        try:
            if (
                chat_log is None
                and async_get_chat_log is not None
                and async_get_chat_session is not None
            ):
                with (
                    async_get_chat_session(
                        self.hass,
                        user_input.conversation_id,
                    ) as session,
                    async_get_chat_log(
                        self.hass,
                        session,
                        user_input,
                    ) as active_chat_log,
                ):
                    return await self._async_process_inner(
                        user_input, chat_log=active_chat_log
                    )
            return await self._async_process_inner(user_input, chat_log=chat_log)
        except Exception:
            _LOGGER.exception("Unexpected error in async_process")
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "An internal error occurred. Check the logs.",
            )
            return self._build_conversation_result(
                intent_response,
                getattr(chat_log, "conversation_id", None)
                or user_input.conversation_id
                or "default",
                continue_conversation=False,
            )

    async def _async_process_inner(
        self, user_input: ConversationInput, chat_log: ChatLog | None = None
    ) -> ConversationResult:
        """Inner processing — wrapped by async_process for error logging."""
        conv_id = (
            getattr(chat_log, "conversation_id", None)
            or user_input.conversation_id
            or str(uuid.uuid4())
        )
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
            if chat_log is None:
                response_text = await self._get_response(messages, session_id=session_id)
            else:
                response_text = await self._stream_chat_log_response(
                    chat_log,
                    messages,
                    session_id=session_id,
                )
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

            while len(self._history) > _MAX_CACHED_CONVERSATIONS:
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

    async def _stream_chat_log_response(
        self,
        chat_log: ChatLog,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> str:
        """Stream safe assistant deltas into Home Assistant's chat log."""
        chunks: list[str] = []

        async def _stream() -> AsyncIterator[dict[str, str]]:
            started = False
            try:
                async for chunk in self._iter_voice_safe_response(
                    messages, session_id=session_id
                ):
                    if not chunk:
                        continue
                    if not started:
                        yield {"role": "assistant"}
                        started = True
                    chunks.append(chunk)
                    yield {"content": chunk}
            except HermesApiError as err:
                if chunks:
                    _LOGGER.warning(
                        "Hermes stream failed after content started; keeping partial response: %s",
                        err,
                    )
                    return
                raise

        try:
            async for _content in chat_log.async_add_delta_content_stream(
                self._agent_id(), _stream()
            ):
                pass
        except HermesApiError as err:
            if chunks:
                _LOGGER.warning(
                    "Hermes stream failed after content started; keeping partial response: %s",
                    err,
                )
                return "".join(chunks)
            raise

        return "".join(chunks)

    async def _iter_voice_safe_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield speech-safe assistant text chunks from Hermes streaming."""
        speech_filter = _UnsafeSpeechStreamFilter()
        stream_started = False

        try:
            async for chunk in self.client.async_stream_message(
                messages, session_id=session_id
            ):
                stream_started = True
                if safe_chunk := speech_filter.feed(chunk):
                    yield safe_chunk
        except HermesStreamSetupError as err:
            if stream_started:
                raise
            _LOGGER.debug(
                "Hermes streaming setup failed; falling back to non-streaming: %s",
                err,
            )
            result = await self.client.async_send_message(messages, session_id=session_id)
            if safe_text := _sanitize_text_for_speech(result.text):
                yield safe_text
            return

        if final_chunk := speech_filter.flush():
            yield final_chunk

    def _agent_id(self) -> str:
        """Return the best available Home Assistant agent identifier."""
        return getattr(self, "entity_id", None) or self.entry.entry_id

    async def _get_response(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> str:
        """Get a response from the API using voice-safe streaming."""
        chunks: list[str] = []
        try:
            async for chunk in self._iter_voice_safe_response(messages, session_id):
                chunks.append(chunk)
        except HermesStreamSetupError as err:
            _LOGGER.debug(
                "Hermes streaming setup failed; falling back to non-streaming: %s",
                err,
            )
            result = await self.client.async_send_message(messages, session_id=session_id)
            return result.text
        except HermesApiError:
            if chunks:
                _LOGGER.warning(
                    "Hermes stream failed after content started; not retrying to avoid duplicate tool calls"
                )
            raise
        return "".join(chunks)

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
