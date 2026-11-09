from __future__ import annotations

import importlib.util
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from tests.test_support import FakeConfigEntry, FakeConversationInput, FakeHass
from custom_components.hermes_conversation.api import HermesStreamSetupError
from custom_components.hermes_conversation import conversation as conversation_module
from custom_components.hermes_conversation.conversation import HermesConversationAgent
from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_CONTEXT_MAX_CHARS,
    CONF_CONTINUED_CONVERSATION_MODE,
    CONF_ENABLE_CONTINUED_CONVERSATION,
    CONF_ENABLE_SESSION_REUSE,
    CONF_INCLUDE_EXPOSED_ENTITIES,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    DEFAULT_PROMPT,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODE_AUTO,
    FOLLOW_UP_MODE_OFF,
    LEGACY_CONF_INSTRUCTIONS,
)


class FakeClient:
    def __init__(
        self,
        *,
        stream_chunks=None,
        stream_error=None,
        stream_error_after_chunks=None,
        send_text=None,
    ):
        self.calls = []
        self.last_session_id = None
        self.next_session_id = "sess-1"
        self.next_text = "stored"
        self.stream_chunks = stream_chunks
        self.stream_error = stream_error
        self.stream_error_after_chunks = stream_error_after_chunks
        self.send_text = send_text

    async def async_stream_message(self, messages, session_id=None):
        self.calls.append({"method": "stream", "messages": messages, "session_id": session_id})
        if self.stream_error is not None:
            raise self.stream_error
        if session_id is None:
            self.last_session_id = self.next_session_id
            chunks = self.stream_chunks if self.stream_chunks is not None else [self.next_text]
            for chunk in chunks:
                yield chunk
            if self.stream_error_after_chunks is not None:
                raise self.stream_error_after_chunks
            return
        self.last_session_id = session_id
        chunks = self.stream_chunks if self.stream_chunks is not None else [self.next_text]
        for chunk in chunks:
            yield chunk
        if self.stream_error_after_chunks is not None:
            raise self.stream_error_after_chunks

    async def async_send_message(self, messages, session_id=None):
        self.calls.append({"method": "send", "messages": messages, "session_id": session_id})
        if session_id is None:
            self.last_session_id = self.next_session_id
            return SimpleNamespace(
                text=self.send_text or self.next_text,
                session_id=self.next_session_id,
            )
        self.last_session_id = session_id
        return SimpleNamespace(text=self.send_text or self.next_text, session_id=session_id)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def _process_with_chat_session(self, session_factory):
        spec = importlib.util.spec_from_file_location(
            "custom_components.hermes_conversation._context_test",
            conversation_module.__file__,
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch(
            "homeassistant.helpers.chat_session.async_get_chat_session",
            session_factory,
        ):
            spec.loader.exec_module(module)
        agent = module.HermesConversationAgent(
            FakeHass(),
            FakeConfigEntry(options={CONF_PROMPT: ""}),
            FakeClient(),
            session_map={},
        )
        return await agent.async_process(
            FakeConversationInput(
                "hello", conversation_id="context-test", context=object()
            )
        )

    async def test_chat_session_explicit_none_avoids_deprecation(self):
        omitted = object()
        seen = []

        @contextmanager
        def chat_session(hass, conversation_id, *, context=omitted):
            seen.append(context)
            yield SimpleNamespace(
                conversation_id=conversation_id, async_on_cleanup=lambda callback: None
            )

        result = await self._process_with_chat_session(chat_session)
        self.assertEqual(seen, [None])
        self.assertIsNone(result.response.error)

    async def test_chat_session_supports_required_context(self):
        seen = []

        @contextmanager
        def chat_session(hass, conversation_id, *, context):
            seen.append(context)
            yield SimpleNamespace(
                conversation_id=conversation_id, async_on_cleanup=lambda callback: None
            )

        result = await self._process_with_chat_session(chat_session)
        self.assertEqual(seen, [None])
        self.assertIsNone(result.response.error)

    async def test_chat_session_keeps_legacy_signature(self):
        seen = []

        @contextmanager
        def chat_session(hass, conversation_id):
            seen.append(conversation_id)
            yield SimpleNamespace(
                conversation_id=conversation_id, async_on_cleanup=lambda callback: None
            )

        result = await self._process_with_chat_session(chat_session)
        self.assertEqual(seen, ["context-test"])
        self.assertIsNone(result.response.error)

    async def test_same_device_new_conversation_reuses_session(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={
                CONF_ENABLE_SESSION_REUSE: True,
                CONF_ENABLE_CONTINUED_CONVERSATION: False,
                CONF_PROMPT: "",
            },
        )
        client = FakeClient()
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        first = await agent.async_process(
            FakeConversationInput(
                "Remember that my favorite color is blue.",
                conversation_id="conv-1",
                device_id="device-123",
            )
        )
        second = await agent.async_process(
            FakeConversationInput(
                "What color did I just say?",
                conversation_id="conv-2",
                device_id="device-123",
            )
        )

        self.assertEqual(first.conversation_id, "conv-1")
        self.assertEqual(second.conversation_id, "conv-2")
        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        self.assertEqual(stream_calls[0]["session_id"], None)
        self.assertEqual(stream_calls[1]["session_id"], "sess-1")
        self.assertEqual(agent.session_map["device:device-123"]["session_id"], "sess-1")
        self.assertNotIn(
            "Remember that my favorite color is blue.",
            [message["content"] for message in stream_calls[1]["messages"]],
        )
        self.assertNotIn(
            "stored",
            [message["content"] for message in stream_calls[1]["messages"]],
        )

    async def test_session_timeout_expires_reuse(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={
                CONF_ENABLE_SESSION_REUSE: True,
                CONF_PROMPT: "",
                CONF_SESSION_TIMEOUT_SECONDS: 1,
            }
        )
        client = FakeClient()
        session_map = {"device:device-123": {"session_id": "stale", "last_used_at": time.time() - 10}}
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map=session_map)

        await agent.async_process(
            FakeConversationInput(
                "Do you remember me?",
                conversation_id="conv-2",
                device_id="device-123",
            )
        )

        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        self.assertEqual(stream_calls[0]["session_id"], None)
        self.assertEqual(agent.session_map["device:device-123"]["session_id"], "sess-1")

    async def test_disabling_reuse_keeps_fresh_sessions(self):
        entry = FakeConfigEntry(
            options={
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        await agent.async_process(FakeConversationInput("one", conversation_id="conv-1", device_id="device-123"))
        await agent.async_process(FakeConversationInput("two", conversation_id="conv-2", device_id="device-123"))

        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        self.assertEqual(stream_calls[0]["session_id"], None)
        self.assertEqual(stream_calls[1]["session_id"], None)
        self.assertEqual(agent.session_map, {})

    async def test_reuse_without_api_key_does_not_send_session_header(self):
        entry = FakeConfigEntry(
            data={},
            options={
                CONF_ENABLE_SESSION_REUSE: True,
                CONF_PROMPT: "",
            },
        )
        client = FakeClient()
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        await agent.async_process(FakeConversationInput("one", conversation_id="conv-1", device_id="device-123"))
        await agent.async_process(FakeConversationInput("two", conversation_id="conv-2", device_id="device-123"))

        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        self.assertEqual(stream_calls[0]["session_id"], None)
        self.assertEqual(stream_calls[1]["session_id"], None)
        self.assertEqual(agent.session_map, {})

    async def test_legacy_conversation_result_ignores_continue_conversation(self):
        class LegacyConversationResult:
            def __init__(self, response, conversation_id):
                self.response = response
                self.conversation_id = conversation_id

        original_result = conversation_module.ConversationResult
        conversation_module.ConversationResult = LegacyConversationResult
        try:
            entry = FakeConfigEntry(
                options={
                    CONF_ENABLE_CONTINUED_CONVERSATION: True,
                    CONF_ENABLE_SESSION_REUSE: False,
                    CONF_PROMPT: "",
                }
            )
            client = FakeClient()
            agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

            result = await agent.async_process(
                FakeConversationInput("hello", conversation_id="conv-legacy")
            )
        finally:
            conversation_module.ConversationResult = original_result

        self.assertEqual(result.conversation_id, "conv-legacy")
        self.assertFalse(hasattr(result, "continue_conversation"))

    async def test_follow_up_mode_off_is_default_even_for_questions(self):
        entry = FakeConfigEntry(
            options={
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        client.next_text = "Would you like anything else?"
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-off")
        )

        self.assertFalse(result.continue_conversation)
        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        messages = stream_calls[0]["messages"]
        self.assertEqual(messages[-1], {"role": "user", "content": "hello"})
        self.assertNotIn(
            "voice auto follow-up is active",
            "\n".join(
                msg["content"] for msg in messages if msg["role"] == "system"
            ),
        )

    async def test_follow_up_mode_always_keeps_listening_without_prompt_guidance(self):
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: FOLLOW_UP_MODE_ALWAYS,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        client.next_text = "Done."
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-always")
        )

        self.assertTrue(result.continue_conversation)
        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        messages = stream_calls[0]["messages"]
        self.assertEqual(messages[-1], {"role": "user", "content": "hello"})
        self.assertNotIn(
            "voice auto follow-up is active",
            "\n".join(
                msg["content"] for msg in messages if msg["role"] == "system"
            ),
        )

    async def test_follow_up_mode_auto_keeps_listening_for_questions_only(self):
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: FOLLOW_UP_MODE_AUTO,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        client.next_text = "Would you like anything else?"
        question_result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-auto-1")
        )
        client.next_text = "Done."
        statement_result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-auto-2")
        )

        self.assertTrue(question_result.continue_conversation)
        self.assertFalse(statement_result.continue_conversation)
        stream_calls = [call for call in client.calls if call["method"] == "stream"]
        system_message = stream_calls[0]["messages"][0]
        self.assertEqual(system_message["role"], "system")
        self.assertIn("voice auto follow-up is active", system_message["content"])

    async def test_follow_up_mode_auto_allows_trailing_quote_after_question(self):
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: FOLLOW_UP_MODE_AUTO,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        client.next_text = '"Do you want the hallway lights too?"'
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-auto-quote")
        )

        self.assertTrue(result.continue_conversation)

    async def test_follow_up_mode_auto_ignores_embedded_questions(self):
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: FOLLOW_UP_MODE_AUTO,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        client.next_text = 'The phrase means "How are you?".'
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-auto-embedded")
        )

        self.assertFalse(result.continue_conversation)

    async def test_legacy_continued_conversation_bool_maps_to_always(self):
        entry = FakeConfigEntry(
            options={
                CONF_ENABLE_CONTINUED_CONVERSATION: True,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_PROMPT: "",
            }
        )
        client = FakeClient()
        client.next_text = "Done."
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hello", conversation_id="conv-legacy-bool")
        )

        self.assertTrue(result.continue_conversation)

    def test_invalid_follow_up_mode_falls_back_to_off(self):
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: "bogus",
                CONF_ENABLE_CONTINUED_CONVERSATION: False,
            }
        )
        agent = HermesConversationAgent(FakeHass(), entry, FakeClient(), session_map={})

        self.assertEqual(agent._continued_conversation_mode(), FOLLOW_UP_MODE_OFF)

    def test_legacy_instructions_feed_system_prompt(self):
        entry = FakeConfigEntry(data={LEGACY_CONF_INSTRUCTIONS: "Legacy system prompt"}, options={})
        agent = HermesConversationAgent(FakeHass(), entry, FakeClient(), session_map={})
        rendered = agent._render_system_prompt("Chalkers")
        self.assertIn("Legacy system prompt", rendered)

    def test_exposed_entities_include_alias_domain_and_device_area(self):
        state = SimpleNamespace(
            entity_id="light.kitchen_ceiling",
            state="on",
            attributes={"friendly_name": "Ceiling Light"},
        )
        hass = FakeHass(states=[state])
        hass._entity_registry = SimpleNamespace(
            async_get=lambda _entity_id: SimpleNamespace(
                aliases={"Kitchen Main", "Ceiling"},
                area_id=None,
                device_id="device-1",
            )
        )
        hass._device_registry = SimpleNamespace(
            async_get=lambda _device_id: SimpleNamespace(area_id="kitchen")
        )
        hass._area_registry = SimpleNamespace(
            async_get_area=lambda _area_id: SimpleNamespace(name="Kitchen")
        )
        entry = FakeConfigEntry(options={CONF_INCLUDE_EXPOSED_ENTITIES: True})
        agent = HermesConversationAgent(hass, entry, FakeClient(), session_map={})

        entities = agent._get_exposed_entities()

        self.assertEqual(
            entities,
            [
                {
                    "entity_id": "light.kitchen_ceiling",
                    "name": "Ceiling Light",
                    "state": "on",
                    "domain": "light",
                    "aliases": ["Ceiling", "Kitchen Main"],
                    "area": "Kitchen",
                }
            ],
        )

    def test_exposed_entity_area_overrides_device_area(self):
        state = SimpleNamespace(
            entity_id="light.kitchen_ceiling",
            state="on",
            attributes={"friendly_name": "Ceiling Light"},
        )
        hass = FakeHass(states=[state])
        hass._entity_registry = SimpleNamespace(
            async_get=lambda _entity_id: SimpleNamespace(
                aliases=[],
                area_id="kitchen",
                device_id="device-1",
            )
        )
        hass._device_registry = SimpleNamespace(
            async_get=lambda _device_id: self.fail(
                "device area must not replace an entity area"
            )
        )
        hass._area_registry = SimpleNamespace(
            async_get_area=lambda _area_id: SimpleNamespace(name="Kitchen")
        )
        entry = FakeConfigEntry(options={CONF_INCLUDE_EXPOSED_ENTITIES: True})
        agent = HermesConversationAgent(hass, entry, FakeClient(), session_map={})

        self.assertEqual(agent._get_exposed_entities()[0]["area"], "Kitchen")

    def test_exposed_entity_survives_missing_registry_metadata(self):
        state = SimpleNamespace(
            entity_id="light.kitchen_ceiling",
            state="on",
            attributes={"friendly_name": "Ceiling Light"},
        )
        hass = FakeHass(states=[state])
        entry = FakeConfigEntry(options={CONF_INCLUDE_EXPOSED_ENTITIES: True})
        agent = HermesConversationAgent(hass, entry, FakeClient(), session_map={})

        self.assertEqual(
            agent._get_exposed_entities(),
            [
                {
                    "entity_id": "light.kitchen_ceiling",
                    "name": "Ceiling Light",
                    "state": "on",
                    "domain": "light",
                    "aliases": [],
                    "area": "",
                }
            ],
        )

    def test_exposed_entity_budget_counts_alias_domain_and_area(self):
        state = SimpleNamespace(
            entity_id="light.kitchen_ceiling",
            state="on",
            attributes={"friendly_name": "Ceiling Light"},
        )
        hass = FakeHass(states=[state])
        hass._entity_registry = SimpleNamespace(
            async_get=lambda _entity_id: SimpleNamespace(
                aliases={"Kitchen Main", "Ceiling"},
                area_id="kitchen",
                device_id=None,
            )
        )
        hass._area_registry = SimpleNamespace(
            async_get_area=lambda _area_id: SimpleNamespace(name="Kitchen")
        )
        entry = FakeConfigEntry(
            options={
                CONF_INCLUDE_EXPOSED_ENTITIES: True,
                CONF_CONTEXT_MAX_CHARS: 70,
            }
        )
        agent = HermesConversationAgent(hass, entry, FakeClient(), session_map={})

        self.assertEqual(agent._get_exposed_entities(), [])

    def test_exposed_entities_resolve_modern_computed_aliases(self):
        computed_name = object()
        registry_entry = SimpleNamespace(
            aliases=[computed_name, "Kitchen Main"],
            area_id=None,
            device_id=None,
        )
        state = SimpleNamespace(
            entity_id="light.kitchen_ceiling",
            state="on",
            attributes={"friendly_name": "Ceiling Light"},
        )
        hass = FakeHass(states=[state])
        hass._entity_registry = SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        entry = FakeConfigEntry(options={CONF_INCLUDE_EXPOSED_ENTITIES: True})
        agent = HermesConversationAgent(hass, entry, FakeClient(), session_map={})

        with mock.patch.object(
            conversation_module.er,
            "async_get_entity_aliases",
            create=True,
            return_value=["Kitchen Main", "Ceiling Light"],
        ) as get_aliases:
            entities = agent._get_exposed_entities()

        get_aliases.assert_called_once_with(hass, registry_entry)
        self.assertEqual(
            entities[0]["aliases"], ["Ceiling Light", "Kitchen Main"]
        )

    def test_default_prompt_renders_extended_entity_context(self):
        self.assertIn("entity.domain", DEFAULT_PROMPT)
        self.assertIn("entity.aliases", DEFAULT_PROMPT)
        self.assertIn("entity.area", DEFAULT_PROMPT)

    async def test_entity_streams_safe_deltas_to_chat_log(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={CONF_ENABLE_SESSION_REUSE: True, CONF_PROMPT: ""},
        )
        client = FakeClient(stream_chunks=["Hello", " there"])
        hass = FakeHass()
        agent = HermesConversationAgent(hass, entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hi", conversation_id="conv-stream")
        )

        self.assertTrue(agent.supports_streaming)
        self.assertEqual(result.response.speech["plain"]["speech"], "Hello there")
        self.assertEqual(
            hass.data["last_chat_log"].deltas,
            [{"role": "assistant"}, {"content": "Hello"}, {"content": " there"}],
        )

    async def test_streaming_filters_reasoning_and_tool_markup(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={CONF_ENABLE_SESSION_REUSE: True, CONF_PROMPT: ""},
        )
        client = FakeClient(
            stream_chunks=[
                "Visible ",
                "<think>secret",
                " still secret</think>",
                " answer <tool_call>{\"name\":\"terminal\"}</tool_call> done",
            ]
        )
        hass = FakeHass()
        agent = HermesConversationAgent(hass, entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hi", conversation_id="conv-safe")
        )

        speech = result.response.speech["plain"]["speech"]
        chat_text = hass.data["last_chat_log"].content[-1].content
        self.assertEqual(speech, "Visible answer done")
        self.assertNotIn("secret", chat_text)
        self.assertNotIn("tool_call", chat_text)
        self.assertNotIn("terminal", chat_text)

    async def test_stream_setup_error_falls_back_to_non_streaming(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={CONF_ENABLE_SESSION_REUSE: True, CONF_PROMPT: ""},
        )
        client = FakeClient(
            stream_error=HermesStreamSetupError("stream rejected"),
            send_text="fallback response",
        )
        agent = HermesConversationAgent(FakeHass(), entry, client, session_map={})

        result = await agent.async_process(
            FakeConversationInput("hi", conversation_id="conv-fallback")
        )

        self.assertEqual(result.response.speech["plain"]["speech"], "fallback response")
        self.assertEqual([call["method"] for call in client.calls], ["stream", "send"])

    async def test_partial_chat_log_stream_error_keeps_partial_text(self):
        entry = FakeConfigEntry(
            data={CONF_API_KEY: "secret"},
            options={CONF_ENABLE_SESSION_REUSE: True, CONF_PROMPT: ""},
        )
        client = FakeClient(
            stream_chunks=["partial"],
            stream_error_after_chunks=HermesStreamSetupError("connection dropped"),
            send_text="should not retry",
        )
        hass = FakeHass()
        agent = HermesConversationAgent(hass, entry, client, session_map={})

        with self.assertLogs(conversation_module._LOGGER, level="WARNING"):
            result = await agent.async_process(
                FakeConversationInput("hi", conversation_id="conv-partial")
            )

        self.assertEqual(result.response.speech["plain"]["speech"], "partial")
        self.assertEqual([call["method"] for call in client.calls], ["stream"])
        self.assertEqual(hass.data["last_chat_log"].content[-1].content, "partial")

    async def test_missing_chat_log_api_uses_final_legacy_response(self):
        original_get_chat_log = conversation_module.async_get_chat_log
        original_get_chat_session = conversation_module.async_get_chat_session
        conversation_module.async_get_chat_log = None
        conversation_module.async_get_chat_session = None
        try:
            entry = FakeConfigEntry(
                data={CONF_API_KEY: "secret"},
                options={CONF_ENABLE_SESSION_REUSE: True, CONF_PROMPT: ""},
            )
            client = FakeClient(stream_chunks=["legacy response"])
            hass = FakeHass()
            agent = HermesConversationAgent(hass, entry, client, session_map={})

            result = await agent.async_process(
                FakeConversationInput("hi", conversation_id="conv-legacy-stream")
            )
        finally:
            conversation_module.async_get_chat_log = original_get_chat_log
            conversation_module.async_get_chat_session = original_get_chat_session

        self.assertEqual(result.response.speech["plain"]["speech"], "legacy response")
        self.assertNotIn("last_chat_log", hass.data)


if __name__ == "__main__":
    unittest.main()
