from __future__ import annotations

import json
import unittest

from tests.test_support import FakeClientTimeout
from custom_components.hermes_conversation.api import HermesApiClient


class FakeResponse:
    def __init__(self, *, status=200, headers=None, json_data=None, text_data="", chunks=None):
        self.status = status
        self.headers = headers or {}
        self._json_data = json_data or {}
        self._text_data = text_data
        self.content = self
        self._chunks = [chunk.encode("utf-8") for chunk in (chunks or [])]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers=None, json=None, timeout=None, ssl=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
                "ssl": ssl,
            }
        )
        return self.responses.pop(0)

    def get(self, url, *, headers=None, timeout=None, ssl=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": timeout,
                "ssl": ssl,
            }
        )
        return self.responses.pop(0)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_streaming_preserves_session_header_and_model_timeout(self):
        session = FakeSession(
            [
                FakeResponse(
                    headers={"X-Hermes-Session-Id": "sess-2"},
                    json_data={"choices": [{"message": {"content": "hello"}}]},
                )
            ]
        )
        client = HermesApiClient(
            session=session,
            host="agent.local",
            port=8443,
            api_key="secret",
            model="custom-model",
            request_timeout=42,
        )

        result = await client.async_send_message(
            [{"role": "user", "content": "hi"}], session_id="sess-1"
        )

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.session_id, "sess-2")
        self.assertEqual(client.last_session_id, "sess-2")
        self.assertEqual(session.calls[0]["headers"]["X-Hermes-Session-Id"], "sess-1")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(session.calls[0]["json"]["model"], "custom-model")
        self.assertIsInstance(session.calls[0]["timeout"], FakeClientTimeout)
        self.assertEqual(session.calls[0]["timeout"].total, 42)

    async def test_streaming_preserves_returned_session_id(self):
        chunks = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Blue"}}]}) + "\n",
            "data: " + json.dumps({"choices": [{"delta": {"content": " sky"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        session = FakeSession(
            [FakeResponse(headers={"X-Hermes-Session-Id": "sess-stream"}, chunks=chunks)]
        )
        client = HermesApiClient(
            session=session,
            host="agent.local",
            port=8443,
            request_timeout=12,
            stream_timeout=30,
        )

        parts = []
        async for part in client.async_stream_message(
            [{"role": "user", "content": "hi"}], session_id="old-session"
        ):
            parts.append(part)

        self.assertEqual("".join(parts), "Blue sky")
        self.assertEqual(client.last_session_id, "sess-stream")
        self.assertEqual(session.calls[0]["headers"]["X-Hermes-Session-Id"], "old-session")
        self.assertEqual(session.calls[0]["timeout"].total, 30)
        self.assertEqual(session.calls[0]["timeout"].sock_read, 12)


if __name__ == "__main__":
    unittest.main()
