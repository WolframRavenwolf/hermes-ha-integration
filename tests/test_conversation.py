from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from tests.test_support import FakeConfigEntry, FakeConversationInput, FakeHass
from custom_components.hermes_conversation import conversation as conversation_module
from custom_components.hermes_conversation.conversation import HermesConversationAgent
from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_ENABLE_CONTINUED_CONVERSATION,
    CONF_ENABLE_SESSION_REUSE,
    CONF_PROMPT,
    CONF_SESSION_TIMEOUT_SECONDS,
    LEGACY_CONF_INSTRUCTIONS,
)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.last_session_id = None
        self.next_session_id = "sess-1"

    async def async_stream_message(self, messages, session_id=None):
        self.calls.append({"method": "stream", "messages": messages, "session_id": session_id})
        if False:
            yield None
        return

    async def async_send_message(self, messages, session_id=None):
        self.calls.append({"method": "send", "messages": messages, "session_id": session_id})
        if session_id is None:
            self.last_session_id = self.next_session_id
            return SimpleNamespace(text="stored", session_id=self.next_session_id)
        self.last_session_id = session_id
        return SimpleNamespace(text="reused", session_id=session_id)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
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
        send_calls = [call for call in client.calls if call["method"] == "send"]
        self.assertEqual(send_calls[0]["session_id"], None)
        self.assertEqual(send_calls[1]["session_id"], "sess-1")
        self.assertEqual(agent.session_map["device:device-123"]["session_id"], "sess-1")

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

        send_calls = [call for call in client.calls if call["method"] == "send"]
        self.assertEqual(send_calls[0]["session_id"], None)
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

        send_calls = [call for call in client.calls if call["method"] == "send"]
        self.assertEqual(send_calls[0]["session_id"], None)
        self.assertEqual(send_calls[1]["session_id"], None)
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

        send_calls = [call for call in client.calls if call["method"] == "send"]
        self.assertEqual(send_calls[0]["session_id"], None)
        self.assertEqual(send_calls[1]["session_id"], None)
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

    def test_legacy_instructions_feed_system_prompt(self):
        entry = FakeConfigEntry(data={LEGACY_CONF_INSTRUCTIONS: "Legacy system prompt"}, options={})
        agent = HermesConversationAgent(FakeHass(), entry, FakeClient(), session_map={})
        rendered = agent._render_system_prompt("Chalkers")
        self.assertIn("Legacy system prompt", rendered)


if __name__ == "__main__":
    unittest.main()
