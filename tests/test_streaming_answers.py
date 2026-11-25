"""Assist streaming regressions for issues #29 and #27."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tests.test_support import FakeConfigEntry, FakeConversationInput, FakeHass
from tests.test_api import FakeResponse, FakeSession
from tests.test_conversation import FakeClient
from custom_components.hermes_conversation import config_flow, conversation
from custom_components.hermes_conversation.api import HermesApiClient, HermesConnectionError
from custom_components.hermes_conversation.const import (
    CONF_CONTINUED_CONVERSATION_MODE,
    CONF_ENABLE_SESSION_REUSE,
    CONF_PROMPT,
    FOLLOW_UP_MODE_AUTO,
)

FINAL_ONLY = "speak_only_final_answer"
DONE = "data: [DONE]\n\n"


def content_frame(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


def tool_frame(status="running"):
    return 'event: hermes.tool.progress\ndata: ' + json.dumps(
        {"tool": "terminal", "toolCallId": "call-1", "status": status}
    ) + "\n\n"


class StreamingAnswerTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, client, options=None):
        entry = FakeConfigEntry(options={
            CONF_PROMPT: "",
            CONF_ENABLE_SESSION_REUSE: False,
            CONF_CONTINUED_CONVERSATION_MODE: FOLLOW_UP_MODE_AUTO,
            **(options or {}),
        })
        return conversation.HermesConversationAgent(FakeHass(), entry, client, {})

    def assert_stream_matches_result(self, agent, result):
        self.assertIsNone(result.response.error)
        chat_log = agent.hass.data["last_chat_log"]
        streamed = "".join(delta.get("content", "") for delta in chat_log.deltas)
        # Assist compares the final speech with the streamed segment after trim().
        self.assertEqual(result.response.speech["plain"]["speech"], streamed.strip())
        self.assertEqual(chat_log.content[-1].content, streamed)
        return streamed

    async def test_final_speech_matches_stream_exactly(self):
        cases = (
            ["Oui. Qu'est-ce que tu souhaites changer dans l'automatisation de la climatisation", " ?"],
            ["  First  item\n", "- Second item\n1. Third item !  "],
            ["Ready? ", "🌞"],
            ["Visible ", "<think>secret</think>", " answer."],
        )
        for chunks in cases:
            with self.subTest(chunks=chunks):
                agent = self.make_agent(FakeClient(stream_chunks=chunks))
                result = await agent.async_process(FakeConversationInput("hello", conversation_id="match"))
                streamed = self.assert_stream_matches_result(agent, result)
                self.assertEqual(
                    agent._history["match"][-1]["content"],
                    conversation._sanitize_text_for_speech(streamed),
                )
                self.assertEqual(result.continue_conversation, streamed.rstrip().endswith("?"))

    async def test_partial_failure_keeps_exact_stream_without_retry(self):
        client = FakeClient(
            stream_chunks=["Keep  this ? "],
            stream_error_after_chunks=HermesConnectionError("dropped"),
        )
        agent = self.make_agent(client)
        with self.assertLogs(conversation._LOGGER, level="WARNING"):
            result = await agent.async_process(FakeConversationInput("hello"))
        self.assertEqual(self.assert_stream_matches_result(agent, result), "Keep  this ? ")
        self.assertTrue(result.continue_conversation)
        self.assertEqual([call["method"] for call in client.calls], ["stream"])

    async def test_legacy_path_keeps_final_speech_cleanup(self):
        agent = self.make_agent(FakeClient(stream_chunks=["  Oui.  Et ensuite ?  "]))
        with mock.patch.object(conversation, "async_get_chat_log", None):
            result = await agent.async_process(FakeConversationInput("hello"))
        self.assertEqual(result.response.speech["plain"]["speech"], "Oui. Et ensuite?")
        self.assertTrue(result.continue_conversation)

    async def test_final_only_drops_every_pre_tool_segment(self):
        frames = [
            content_frame("Checking now. "), tool_frame(), tool_frame("completed"),
            content_frame("One more check. "), tool_frame(), tool_frame("completed"),
            content_frame("Oui. Et ensuite"), content_frame(" ?"), DONE,
        ]
        # Also split event names, payloads, and frame boundaries across network reads.
        wire = "".join(frames)
        session = FakeSession([FakeResponse(chunks=[wire[i:i + 7] for i in range(0, len(wire), 7)])])
        agent = self.make_agent(HermesApiClient(session, "agent.local", 8443), {FINAL_ONLY: True})
        result = await agent.async_process(FakeConversationInput("hello", conversation_id="final"))
        self.assertEqual(result.response.speech["plain"]["speech"], "Oui. Et ensuite ?")
        self.assert_stream_matches_result(agent, result)
        self.assertEqual(agent._history["final"][-1]["content"], "Oui. Et ensuite?")
        self.assertTrue(result.continue_conversation)
        self.assertEqual(len(session.calls), 1)

    async def test_final_only_waits_for_done_even_without_tools(self):
        agent = None
        observed = []

        class ObservedResponse(FakeResponse):
            async def iter_any(self):
                assert agent is not None
                for chunk in self._chunks:
                    observed.append(list(agent.hass.data["last_chat_log"].deltas))
                    yield chunk

        session = FakeSession([ObservedResponse(chunks=[content_frame("Hello"), content_frame(" there"), DONE])])
        agent = self.make_agent(HermesApiClient(session, "agent.local", 8443), {FINAL_ONLY: True})
        result = await agent.async_process(FakeConversationInput("hello"))
        self.assertEqual(observed, [[], [], []])
        self.assertEqual(self.assert_stream_matches_result(agent, result), "Hello there")

    async def test_default_and_explicit_false_keep_incremental_preamble(self):
        for options in ({}, {FINAL_ONLY: False}):
            with self.subTest(options=options):
                agent = None
                observed = []

                class ObservedResponse(FakeResponse):
                    async def iter_any(self):
                        assert agent is not None
                        for chunk in self._chunks:
                            observed.append(list(agent.hass.data["last_chat_log"].deltas))
                            yield chunk

                session = FakeSession([ObservedResponse(chunks=[
                    content_frame("Checking. "), tool_frame(), content_frame("Done."), DONE,
                ])])
                agent = self.make_agent(HermesApiClient(session, "agent.local", 8443), options)
                result = await agent.async_process(FakeConversationInput("hello"))
                self.assertEqual(self.assert_stream_matches_result(agent, result), "Checking. Done.")
                self.assertEqual(observed[1], [{"role": "assistant"}, {"content": "Checking. "}])
                self.assertEqual(session.calls[0]["json"]["stream"], True)

    async def test_final_only_does_not_flush_unconfirmed_text_on_failure(self):
        failures = (
            [],  # EOF without DONE
            ['event: error\ndata: {"error": "failed"}\n\n', DONE],
            ['data: {"choices": [{"delta": {}, "finish_reason": "length"}]}\n\n', DONE],
            ['data: {"error": false}\n\n', DONE],
        )
        for failure in failures:
            for after_tool in (False, True):
                with self.subTest(failure=failure, after_tool=after_tool):
                    frames = [content_frame("Checking now. ")]
                    if after_tool:
                        frames += [tool_frame(), content_frame("Unconfirmed answer.")]
                    session = FakeSession([FakeResponse(chunks=frames + failure)])
                    agent = self.make_agent(HermesApiClient(session, "agent.local", 8443), {FINAL_ONLY: True})
                    with self.assertLogs(conversation._LOGGER, level="WARNING"):
                        result = await agent.async_process(FakeConversationInput("hello"))
                    self.assertIsNotNone(result.response.error)
                    self.assertIsNone(result.response.speech)
                    self.assertEqual(agent.hass.data["last_chat_log"].deltas, [])
                    self.assertFalse(result.continue_conversation)
                    self.assertEqual(len(session.calls), 1)

    async def test_final_only_preserves_intentional_silence_after_tools(self):
        session = FakeSession([FakeResponse(chunks=[
            content_frame("Checking. "), tool_frame(), content_frame("NO_REPLY"), DONE,
        ])])
        agent = self.make_agent(HermesApiClient(session, "agent.local", 8443), {FINAL_ONLY: True})
        result = await agent.async_process(FakeConversationInput("hello"))
        self.assertIsNone(result.response.error)
        self.assertIsNone(result.response.speech)
        self.assertFalse(result.continue_conversation)
        self.assertEqual(agent.hass.data["last_chat_log"].deltas, [])


class FinalAnswerOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_only_option_defaults_off_and_preserves_saved_value(self):
        for options, expected in (({}, False), ({FINAL_ONLY: True}, True)):
            with self.subTest(options=options):
                flow = config_flow.HermesConversationOptionsFlow()
                flow.hass = FakeHass()
                flow.config_entry = FakeConfigEntry(data={"host": "agent.local"}, options=options)
                with mock.patch.object(config_flow.vol, "Optional", wraps=config_flow.vol.Optional) as optional:
                    form = await flow.async_step_init()
                self.assertIn(FINAL_ONLY, form["data_schema"])
                self.assertIs(form["data_schema"][FINAL_ONLY], bool)
                self.assertIn(mock.call(FINAL_ONLY, default=expected), optional.call_args_list)

    async def test_final_only_option_can_be_enabled_preserved_and_disabled(self):
        entry = FakeConfigEntry(data={"host": "agent.local"}, options={"keep": "value"})
        flow = config_flow.HermesConversationOptionsFlow()
        flow.hass = FakeHass()
        flow.hass.config_entries.entries.append(entry)
        flow.config_entry = entry
        for submission, expected in (({FINAL_ONLY: True}, True), ({CONF_PROMPT: "hi"}, True), ({FINAL_ONLY: False}, False)):
            result = await flow.async_step_init(submission)
            self.assertEqual(result["type"], "create_entry")
            self.assertIs(entry.options[FINAL_ONLY], expected)
            self.assertEqual(entry.options["keep"], "value")
            self.assertNotIn(FINAL_ONLY, entry.data)
