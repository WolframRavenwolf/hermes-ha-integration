"""Intentional silence is a complete response, never streamed as speech."""
import unittest
from unittest.mock import ANY, AsyncMock

from tests.test_support import FakeConfigEntry, FakeConversationInput, FakeHass, FakeChatLog
from tests.test_conversation import FakeClient
from custom_components.hermes_conversation.conversation import (
    HermesConversationAgent, _sanitize_text_for_speech,
)
from custom_components.hermes_conversation.const import (
    CONF_CONTINUED_CONVERSATION_MODE, FOLLOW_UP_MODE_ALWAYS,
)


class SilenceTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, chunks):
        entry = FakeConfigEntry(options={CONF_CONTINUED_CONVERSATION_MODE:FOLLOW_UP_MODE_ALWAYS})
        return HermesConversationAgent(FakeHass(), entry, FakeClient(stream_chunks=chunks), session_map={})

    async def test_split_marker_never_reaches_speech_stream(self):
        marker = 'NO_REPLY'
        for split in range(len(marker) + 1):
            with self.subTest(split=split):
                agent = self.make_agent([marker[:split],marker[split:]])
                chunks = [chunk async for chunk in agent._iter_voice_safe_response([])]
                self.assertEqual(chunks, [])

    async def test_silent_result_has_no_speech_or_follow_up(self):
        agent = self.make_agent(['NO_', 'REPLY'])
        agent._async_speak_fallback = AsyncMock()
        chat_log = FakeChatLog('quiet', 'Play music')
        result, failed = await agent._async_process_inner(FakeConversationInput('Play music',device_id='speaker'), chat_log=chat_log)
        self.assertFalse(failed)
        self.assertIsNone(result.response.speech)
        self.assertFalse(result.continue_conversation)
        self.assertEqual(chat_log.deltas, [])
        agent._async_speak_fallback.assert_not_called()

    async def test_optional_ascii_whitespace_is_silent(self):
        agent = self.make_agent([' \nNO', '_REPLY', '\t '])
        self.assertEqual([chunk async for chunk in agent._iter_voice_safe_response([])], [])
        self.assertEqual(_sanitize_text_for_speech(' \nNO_REPLY\t '), 'NO_REPLY')

    async def test_mentions_and_later_prose_are_not_silenced(self):
        for chunks in (['NO_', 'REPLY means no speech.'], ['The token ', 'NO_REPLY'], ['NO_REPLY', ' but explain this.'], ['No_reply']):
            with self.subTest(chunks=chunks):
                agent = self.make_agent(chunks)
                result = ''.join([chunk async for chunk in agent._iter_voice_safe_response([])])
                self.assertTrue(result.strip())

    async def test_empty_text_is_not_an_intentional_silence_signal(self):
        agent = self.make_agent([''])
        agent._async_speak_fallback = AsyncMock()
        result, failed = await agent._async_process_inner(FakeConversationInput('Empty response'))
        self.assertFalse(failed)
        self.assertIsNotNone(result.response.speech)
        self.assertTrue(result.continue_conversation)

    async def test_nonstream_setup_fallback_consumes_marker(self):
        from custom_components.hermes_conversation.api import HermesStreamSetupError
        agent = self.make_agent([])
        agent.client = FakeClient(stream_error=HermesStreamSetupError('setup'),send_text='NO_REPLY')
        result, failed = await agent._async_process_inner(FakeConversationInput('Quiet response'))
        self.assertFalse(failed)
        self.assertIsNone(result.response.speech)
        self.assertFalse(result.continue_conversation)

    def test_prefix_divergence_and_whitespace_cap_release_text(self):
        from custom_components.hermes_conversation.silence import SpeechSilenceFilter
        stream = SpeechSilenceFilter()
        self.assertEqual(stream.feed('NO'), '')
        self.assertEqual(stream.feed(' thanks'), 'NO thanks')
        self.assertEqual(stream.flush(), '')
        stream = SpeechSilenceFilter()
        self.assertEqual(stream.feed(' ' * 65), ' ' * 65)
        self.assertEqual(stream.feed('NO_REPLY'), 'NO_REPLY')
        self.assertFalse(stream.silenced)

    def test_sanitizer_does_not_reclassify_cleaned_text_as_a_marker(self):
        self.assertEqual(_sanitize_text_for_speech('NO_REPLY'), 'NO_REPLY')
        self.assertTrue(_sanitize_text_for_speech('NO_REPLY means no speech.'))
        self.assertTrue(_sanitize_text_for_speech(' ' * 65 + 'NO_REPLY'))

    async def test_formatted_mentions_remain_spoken_in_stream_and_fallback(self):
        from custom_components.hermes_conversation.api import HermesStreamSetupError
        for text in ('**NO_REPLY**', '`NO_REPLY`'):
            for fallback in (False, True):
                with self.subTest(text=text, fallback=fallback):
                    agent = self.make_agent(list(text))
                    if fallback:
                        agent.client = FakeClient(
                            stream_error=HermesStreamSetupError('setup'), send_text=text
                        )
                    agent._async_speak_fallback = AsyncMock()
                    state = {}
                    chat_log = FakeChatLog('mention', 'Explain the marker')
                    result, failed = await agent._async_process_inner(
                        FakeConversationInput('Explain the marker'),
                        chat_log=chat_log,
                        request_state=state,
                    )
                    self.assertFalse(failed)
                    # Streamed speech equals the chat-log text; fallback TTS stays clean.
                    self.assertEqual(
                        result.response.speech['plain']['speech'],
                        chat_log.content[-1].content.strip(),
                    )
                    self.assertIn('NO_REPLY', result.response.speech['plain']['speech'])
                    agent._async_speak_fallback.assert_awaited_once_with(
                        'NO_REPLY', ANY
                    )
                    self.assertTrue(result.continue_conversation)
                    self.assertIsNot(state.get('intentional_silence'), True)
