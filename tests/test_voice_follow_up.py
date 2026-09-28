"""Regression coverage for speech cleanup and Auto follow-up (issue #16)."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.test_support import FakeConfigEntry, FakeConversationInput, FakeHass  # isort: skip
from tests.test_conversation import FakeClient  # isort: skip
from custom_components.hermes_conversation import conversation as conversation_module
from custom_components.hermes_conversation.api import HermesStreamSetupError
from custom_components.hermes_conversation.const import (
    CONF_ALWAYS_SPEAK_FALLBACK,
    CONF_CONTINUED_CONVERSATION_MODE,
    CONF_ENABLE_SESSION_REUSE,
    CONF_EXPOSE_DEVICE_CONTEXT,
    CONF_FALLBACK_MEDIA_PLAYER,
    CONF_FALLBACK_TTS_ENGINE,
    CONF_PROMPT,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODE_AUTO,
    FOLLOW_UP_MODE_OFF,
)
from custom_components.hermes_conversation.conversation import (
    HermesConversationAgent,
    _sanitize_text_for_speech,
    _UnsafeSpeechStreamFilter,
)

EMOJI = (
    "🌞",
    "☀️",
    "👍🏽",
    "👩🏽‍💻",
    "👨‍👩‍👧‍👦",
    "🏳️‍🌈",
    "🇪🇸",
    "1️⃣",
    "2⃣",
    "#️⃣",
    "*️⃣",
    "❤️",
    "©️",
    "↔️",
    "➕️",
    "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
)
MEANINGFUL_TEXT = (
    "21 °C, 70 °F, 50%, 12 €, £5, $10, ¥200. "
    "2 × 3 = 6; 8 ÷ 2 = 4; 5 − 2 = 3; ±2; x ≤ 5; x ≥ 1; √9; π ≈ 3.14; "
    "a ↔ b; 2 ➕ 3 = 5; 6 ➗ 2 = 3; © 2026; ®; ™; #2; 2 * 3 = 6. "
    "Español, 日本語, العربية, फ़ाइल, ക്\u200dഷ."
)


class SpeechCleanupTests(unittest.TestCase):
    def test_final_speech_removes_emoji_sequences(self):
        for emoji in EMOJI:
            with self.subTest(emoji=emoji):
                self.assertEqual(
                    _sanitize_text_for_speech(f"**¿A qué hora?** {emoji}"),
                    "¿A qué hora?",
                )
                self.assertEqual(_sanitize_text_for_speech(emoji), "")

    def test_emoji_only_link_does_not_leave_a_spoken_url(self):
        for markup in (
            "[🌞](https://example.com/x)",
            "[1️⃣](https://example.com/x)",
            "![🌞](https://example.com/x.png)",
            "[](https://example.com/x)",
        ):
            with self.subTest(markup=markup):
                self.assertEqual(_sanitize_text_for_speech(markup), "")
                self.assertEqual(
                    _sanitize_text_for_speech(f"Ready? {markup}"), "Ready?"
                )
                speech_filter = _UnsafeSpeechStreamFilter()
                streamed = speech_filter.feed(markup) + speech_filter.flush()
                self.assertEqual(streamed, "")

    def test_stream_removes_emoji_only_links_at_every_split(self):
        for markup in (
            "[🌞](https://example.com/x)",
            "[🌞 🌞](https://example.com/x)",
            "![1️⃣](https://example.com/x)",
            "[](https://example.com/x)",
        ):
            text = f"Ready? {markup} Next."
            chunkings = [[text[:split], text[split:]] for split in range(len(text) + 1)]
            chunkings.append(list(text))
            for chunks in chunkings:
                with self.subTest(markup=markup, chunks=chunks):
                    speech_filter = _UnsafeSpeechStreamFilter()
                    parts = [speech_filter.feed(chunk) for chunk in chunks]
                    parts.append(speech_filter.flush())
                    self.assertEqual("".join(parts).split(), ["Ready?", "Next."])

    def test_explicit_text_presentation_survives_complete_and_split_streams(self):
        for symbol in ("➡︎", "✈︎", "ℹ︎", "☀︎"):
            text = f"A {symbol} B"
            with self.subTest(symbol=symbol):
                self.assertEqual(_sanitize_text_for_speech(text), text)
                for split in range(len(text) + 1):
                    speech_filter = _UnsafeSpeechStreamFilter()
                    parts = [speech_filter.feed(text[:split])]
                    parts.append(speech_filter.feed(text[split:]))
                    parts.append(speech_filter.flush())
                    self.assertEqual("".join(parts), text)
                speech_filter = _UnsafeSpeechStreamFilter()
                self.assertEqual(
                    "".join(speech_filter.feed(char) for char in text)
                    + speech_filter.flush(),
                    text,
                )
        self.assertEqual(_sanitize_text_for_speech("A ➡️ B"), "A B")

    def test_nested_and_escaped_link_labels_never_expose_destination(self):
        cases = (
            ("[array[0]](https://example.com/private)", "array[0]"),
            (r"[array\]0](https://example.com/private)", r"array\]0"),
            ("![photo[0]](https://example.com/private)", "photo[0]"),
            ("[nested [inner] label](https://example.com/private_(x))", "nested [inner] label"),
            ("[![img](https://evil.com/token)](https://other.example/x)", "img"),
            ("[a [inner](https://evil.com/token) z](https://other.example/x)", "a inner z"),
            (r"[label\](https://evil.com/token)", "label\\"),
            ("[a]](https://evil.com/token)", "a]"),
            ("[[a](https://evil.com/token)](https://other.example/x)", "a"),
        )
        for markup, label in cases:
            text = f"Read {markup} now."
            expected = f"Read {label} now."
            with self.subTest(markup=markup):
                self.assertEqual(_sanitize_text_for_speech(text), expected)
                chunkings = ([text[:i], text[i:]] for i in range(len(text) + 1))
                for chunks in (*chunkings, list(text)):
                    speech_filter = _UnsafeSpeechStreamFilter()
                    output = "".join(speech_filter.feed(chunk) for chunk in chunks)
                    output += speech_filter.flush()
                    self.assertEqual(output, expected)
                    self.assertNotIn("https://", output)

    def test_link_streaming_preserves_labels_and_plain_brackets(self):
        cases = (
            ("Read [docs](https://example.com/x) now!", "Read docs now!"),
            ("![photo](https://example.com/x) here", "photo here"),
            ("[docs](https://example.com/a_(b))!", "docs!"),
            (r"[docs](https://example.com/a\)b) now", "docs now"),
            ("Values [1, 2], x[0] = 3!", "Values [1, 2], x[0] = 3!"),
            ("[unfinished label", "[unfinished label"),
            ("[docs](https://example.com/unfinished", "docs"),
            ("[" + "x" * 5000, ""),
            ("[" + "x" * 5000 + "](https://example.com/private) safe", " safe"),
        )
        for text, expected in cases:
            with self.subTest(text=text[:80]):
                speech_filter = _UnsafeSpeechStreamFilter()
                parts = [speech_filter.feed(char) for char in text]
                parts.append(speech_filter.flush())
                self.assertEqual("".join(parts), expected)

    def test_stream_removes_emoji_at_every_split(self):
        for emoji in EMOJI:
            text = f"Ready? {emoji} Next."
            for split in range(len(text) + 1):
                with self.subTest(emoji=emoji, split=split):
                    speech_filter = _UnsafeSpeechStreamFilter()
                    parts = [speech_filter.feed(text[:split])]
                    parts.append(speech_filter.feed(text[split:]))
                    parts.append(speech_filter.flush())
                    self.assertEqual("".join(parts), "Ready?  Next.")
                    self.assertEqual(speech_filter.flush(), "")

    def test_meaningful_symbols_and_non_latin_text_survive(self):
        self.assertEqual(_sanitize_text_for_speech(MEANINGFUL_TEXT), MEANINGFUL_TEXT)
        speech_filter = _UnsafeSpeechStreamFilter()
        parts = [speech_filter.feed(char) for char in MEANINGFUL_TEXT]
        parts.append(speech_filter.flush())
        self.assertEqual("".join(parts), MEANINGFUL_TEXT)

    def test_ordinary_text_streams_before_completion(self):
        speech_filter = _UnsafeSpeechStreamFilter()
        self.assertEqual(speech_filter.feed("What time? "), "What time? ")
        self.assertEqual(speech_filter.feed("🌞"), "")
        self.assertEqual(speech_filter.flush(), "")


class VoiceFollowUpTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, client, mode=FOLLOW_UP_MODE_AUTO):
        hass = FakeHass()
        entry = FakeConfigEntry(
            options={
                CONF_CONTINUED_CONVERSATION_MODE: mode,
                CONF_ENABLE_SESSION_REUSE: False,
                CONF_EXPOSE_DEVICE_CONTEXT: False,
                CONF_PROMPT: "",
                CONF_ALWAYS_SPEAK_FALLBACK: True,
                CONF_FALLBACK_MEDIA_PLAYER: "media_player.test",
                CONF_FALLBACK_TTS_ENGINE: "tts.test",
            }
        )
        return HermesConversationAgent(hass, entry, client, session_map={})

    async def test_auto_follow_up_uses_clean_speech_on_every_response_path(self):
        text = "¿A qué hora quieres que lo haga? 🌞👩🏽‍💻1️⃣"
        expected = "¿A qué hora quieres que lo haga?"
        for legacy in (False, True):
            for fallback in (False, True):
                with self.subTest(legacy=legacy, fallback=fallback):
                    client = FakeClient(
                        stream_chunks=list(text),
                        stream_error=HermesStreamSetupError("rejected")
                        if fallback
                        else None,
                        send_text=text,
                    )
                    agent = self.make_agent(client)
                    # Legacy HA has no ChatLog API; keep that path covered too.
                    with (
                        mock.patch.object(
                            conversation_module,
                            "async_get_chat_log",
                            None if legacy else conversation_module.async_get_chat_log,
                        ),
                        mock.patch.object(
                            agent.hass.services,
                            "async_call",
                            new_callable=mock.AsyncMock,
                        ) as speak,
                    ):
                        result = await agent.async_process(
                            FakeConversationInput(
                                "Programa la alarma",
                                language="es",
                                conversation_id="voice-test",
                                device_id="test-device",
                            )
                        )
                    self.assertTrue(result.continue_conversation)
                    self.assertEqual(
                        result.response.speech["plain"]["speech"], expected
                    )
                    speak.assert_awaited_once()
                    assert speak.await_args is not None
                    self.assertEqual(speak.await_args.args[2]["message"], expected)
                    self.assertEqual(
                        agent._history["voice-test"][-1]["content"], expected
                    )
                    self.assertEqual(
                        [call["method"] for call in client.calls],
                        ["stream", "send"] if fallback else ["stream"],
                    )
                    if not legacy:
                        deltas = agent.hass.data["last_chat_log"].deltas
                        self.assertEqual(
                            "".join(d.get("content", "") for d in deltas).strip(),
                            expected,
                        )

    async def test_emoji_link_fallback_cleans_speech_tts_and_history(self):
        client = FakeClient(
            stream_error=HermesStreamSetupError("rejected"),
            send_text="Ready? [🌞](https://example.com/x)",
        )
        agent = self.make_agent(client)
        with mock.patch.object(
            agent.hass.services, "async_call", new_callable=mock.AsyncMock
        ) as speak:
            result = await agent.async_process(
                FakeConversationInput(
                    "hello", conversation_id="link-test", device_id="test-device"
                )
            )
        self.assertEqual(result.response.speech["plain"]["speech"], "Ready?")
        self.assertTrue(result.continue_conversation)
        speak.assert_awaited_once()
        assert speak.await_args is not None
        self.assertEqual(speak.await_args.args[2]["message"], "Ready?")
        self.assertEqual(agent._history["link-test"][-1]["content"], "Ready?")
        deltas = agent.hass.data["last_chat_log"].deltas
        self.assertEqual("".join(d.get("content", "") for d in deltas), "Ready?")
        self.assertEqual([call["method"] for call in client.calls], ["stream", "send"])

    async def test_split_emoji_links_never_reach_chat_log_or_tts(self):
        for fail_after in (False, True):
            with self.subTest(fail_after=fail_after):
                client = FakeClient(
                    stream_chunks=["Ready? ", "[", "🌞", "](https://example.com/x)"],
                    stream_error_after_chunks=conversation_module.HermesApiError(
                        "dropped"
                    )
                    if fail_after
                    else None,
                )
                agent = self.make_agent(client)
                with mock.patch.object(
                    agent.hass.services, "async_call", new_callable=mock.AsyncMock
                ) as speak:
                    if fail_after:
                        with self.assertLogs(
                            conversation_module._LOGGER, level="WARNING"
                        ):
                            result = await agent.async_process(
                                FakeConversationInput(
                                    "hello",
                                    conversation_id="split-link",
                                    device_id="test",
                                )
                            )
                    else:
                        result = await agent.async_process(
                            FakeConversationInput(
                                "hello", conversation_id="split-link", device_id="test"
                            )
                        )
                self.assertEqual(result.response.speech["plain"]["speech"], "Ready?")
                self.assertTrue(result.continue_conversation)
                speak.assert_awaited_once()
                assert speak.await_args is not None
                self.assertEqual(speak.await_args.args[2]["message"], "Ready?")
                deltas = agent.hass.data["last_chat_log"].deltas
                self.assertEqual(
                    "".join(d.get("content", "") for d in deltas).strip(), "Ready?"
                )
                self.assertEqual([call["method"] for call in client.calls], ["stream"])

    async def test_nested_link_destinations_never_reach_chat_log_tts_or_history(self):
        cases = (
            ("[array[0]](https://example.com/private)", "array[0]"),
            ("[![img](https://evil.com/token)](https://other.example/x)", "img"),
            ("[a [inner](https://evil.com/token) z](https://other.example/x)", "a inner z"),
            (r"[label\](https://evil.com/token)", "label\\"),
            ("[a]](https://evil.com/token)", "a]"),
            ("[[a](https://evil.com/token)](https://other.example/x)", "a"),
        )
        for markup, label in cases:
            expected = f"Read {label} now?"
            for legacy in (False, True):
                for fallback in (False, True):
                    for split in (range(len(markup) + 1) if not fallback else (0,)):
                        with self.subTest(legacy=legacy, fallback=fallback, split=split):
                            client = FakeClient(
                                stream_chunks=["Read ", markup[:split], markup[split:], " now?"],
                                stream_error=HermesStreamSetupError("rejected")
                                if fallback
                                else None,
                                send_text=f"Read {markup} now?",
                            )
                            agent = self.make_agent(client)
                            with (
                                mock.patch.object(
                                    conversation_module,
                                    "async_get_chat_log",
                                    None if legacy else conversation_module.async_get_chat_log,
                                ),
                                mock.patch.object(
                                    agent.hass.services, "async_call", new_callable=mock.AsyncMock
                                ) as speak,
                            ):
                                result = await agent.async_process(
                                    FakeConversationInput(
                                        "hello", conversation_id="nested-link", device_id="test-device"
                                    )
                                )
                            self.assertEqual(result.response.speech["plain"]["speech"], expected)
                            self.assertEqual(agent._history["nested-link"][-1]["content"], expected)
                            speak.assert_awaited_once()
                            assert speak.await_args is not None
                            self.assertEqual(speak.await_args.args[2]["message"], expected)
                            if not legacy:
                                deltas = agent.hass.data["last_chat_log"].deltas
                                self.assertEqual(
                                    "".join(d.get("content", "") for d in deltas), expected
                                )

    async def test_partial_stream_error_preserves_pending_plain_symbol(self):
        for suffix in ("22", "©", "#"):
            with self.subTest(suffix=suffix):
                client = FakeClient(
                    stream_chunks=[f"The value is {suffix}"],
                    stream_error_after_chunks=conversation_module.HermesApiError(
                        "dropped"
                    ),
                )
                agent = self.make_agent(client)
                with self.assertLogs(conversation_module._LOGGER, level="WARNING"):
                    result = await agent.async_process(FakeConversationInput("hello"))
                self.assertEqual(
                    result.response.speech["plain"]["speech"], f"The value is {suffix}"
                )
                self.assertEqual([call["method"] for call in client.calls], ["stream"])

    async def test_partial_stream_error_does_not_flush_hidden_markup(self):
        for tail in ("<thi", "<think>secret", "<tool_call>secret"):
            with self.subTest(tail=tail):
                client = FakeClient(
                    stream_chunks=["The value is 22", tail],
                    stream_error_after_chunks=conversation_module.HermesApiError(
                        "dropped"
                    ),
                )
                agent = self.make_agent(client)
                with self.assertLogs(conversation_module._LOGGER, level="WARNING"):
                    result = await agent.async_process(FakeConversationInput("hello"))
                self.assertEqual(
                    result.response.speech["plain"]["speech"], "The value is 22"
                )
                deltas = agent.hass.data["last_chat_log"].deltas
                self.assertEqual(
                    "".join(d.get("content", "") for d in deltas), "The value is 22"
                )
                self.assertEqual([call["method"] for call in client.calls], ["stream"])

    async def test_auto_keeps_final_question_heuristic(self):
        cases = (
            ('The phrase means "How are you?". 🌞', False),
            ("When do you want me to do it? Tell me what time. 🌞", False),
            ("Done. 🌞", False),
            ("🌞👩🏽‍💻", False),
            ('"Do you want the hallway lights too?" 🌞', True),
            ("何時にしますか？ 🌞", True),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                agent = self.make_agent(FakeClient(stream_chunks=list(text)))
                result = await agent.async_process(FakeConversationInput("hello"))
                self.assertEqual(result.continue_conversation, expected)

    async def test_off_and_always_modes_keep_their_semantics(self):
        for mode, expected in (
            (FOLLOW_UP_MODE_OFF, False),
            (FOLLOW_UP_MODE_ALWAYS, True),
        ):
            with self.subTest(mode=mode):
                agent = self.make_agent(FakeClient(stream_chunks=["Ready? 🌞"]), mode)
                result = await agent.async_process(FakeConversationInput("hello"))
                self.assertEqual(result.continue_conversation, expected)
                self.assertEqual(result.response.speech["plain"]["speech"], "Ready?")

    async def test_plain_speech_contract_is_independent_of_follow_up_mode(self):
        for mode in (FOLLOW_UP_MODE_OFF, FOLLOW_UP_MODE_ALWAYS, FOLLOW_UP_MODE_AUTO):
            with self.subTest(mode=mode):
                client = FakeClient()
                agent = self.make_agent(client, mode)
                agent.entry.options[CONF_PROMPT] = "Custom instructions."
                await agent.async_process(FakeConversationInput("hello"))
                prompt = client.calls[0]["messages"][0]["content"]
                self.assertIn("Custom instructions.", prompt)
                self.assertIn("plain, speakable text", prompt)
                self.assertIn("Do not use emoji", prompt)
                self.assertIn("decorative formatting", prompt)
                if mode == FOLLOW_UP_MODE_AUTO:
                    self.assertIn("one short, direct question", prompt)
                    self.assertIn("nothing after the question mark", prompt)
                else:
                    self.assertNotIn("voice auto follow-up is active", prompt)
