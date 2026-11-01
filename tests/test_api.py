from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from urllib.parse import urlsplit

from tests.test_support import FakeClientError, FakeClientTimeout
from custom_components.hermes_conversation.api import (
    HermesApiClient,
    HermesApiError,
    HermesAuthError,
    HermesConnectionError,
    HermesStreamSetupError,
    _NATIVE_ROUTE_CANARY_PROFILE,
)


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

    def get(
        self,
        url,
        *,
        headers=None,
        timeout=None,
        ssl=None,
        allow_redirects=None,
    ):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": timeout,
                "ssl": ssl,
                "allow_redirects": allow_redirects,
            }
        )
        return self.responses.pop(0)


class NativeRoutingSession(FakeSession):
    """Resolve health by profile, including the default handle and a real user ID."""

    def __init__(self, *, default_handle_alias):
        super().__init__([])
        self.default_handle_alias = default_handle_alias

    def get(self, url, **kwargs):
        path = urlsplit(url).path
        profile = path.split("/")[2]
        if path.endswith("/v1/health"):
            exists = profile in {"default", "worker", "hermes-route-canary-nonexistent"}
            exists = exists or (self.default_handle_alias and profile == "hermes")
            response = FakeResponse(
                status=200 if exists else 404,
                json_data={"status": "ok", "platform": "hermes-agent"},
            )
        elif path.endswith("/v1/models"):
            response = FakeResponse(json_data={"data": [{"id": "worker"}]})
        else:
            response = FakeResponse(json_data={"status": "ok"})
        self.responses.append(response)
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        self.responses.append(FakeResponse(
            json_data={"choices": [{"message": {"content": "ok"}}]},
            chunks=[
                'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n',
                "data: [DONE]\n\n",
            ],
        ))
        return super().post(url, **kwargs)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _health_response():
        return FakeResponse(
            json_data={
                "status": "ok",
                "platform": "hermes-agent",
                "version": "test",
            }
        )

    async def test_connection_probe_requires_authenticated_models_after_public_health(self):
        session = FakeSession(
            [self._health_response(), FakeResponse(status=401)]
        )
        client = HermesApiClient(
            session=session,
            host="agent.local",
            port=8443,
            api_key="wrong-key",
            profile="worker",
        )

        with self.assertRaises(HermesAuthError):
            await client.async_check_connection()

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/profile/worker/v1/health",
                "https://agent.local:8443/profile/worker/v1/models",
            ],
        )
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertFalse(session.calls[1]["allow_redirects"])
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertEqual(
            session.calls[1]["headers"],
            {"Authorization": "Bearer wrong-key"},
        )

    def test_native_canary_cannot_be_a_real_profile_id(self):
        self.assertNotRegex(
            _NATIVE_ROUTE_CANARY_PROFILE, r"^[a-z0-9][a-z0-9_-]{0,63}$"
        )

    async def test_native_requests_accept_default_handle_alias(self):
        for alias in (False, True):
            for profile in ("worker", "default"):
                for operation in ("connection", "models", "detailed", "chat", "stream"):
                    with self.subTest(alias=alias, profile=profile, operation=operation):
                        session = NativeRoutingSession(default_handle_alias=alias)
                        client = HermesApiClient(
                            session, "agent.local", 8443, api_key="profile-key",
                            profile=profile, profile_route="native",
                        )
                        if operation == "connection":
                            self.assertTrue(await client.async_check_connection())
                        elif operation == "models":
                            self.assertEqual(await client.async_get_models(), [{"id": "worker"}])
                        elif operation == "detailed":
                            self.assertTrue((await client.async_get_detailed_health()).available)
                        elif operation == "chat":
                            self.assertEqual((await client.async_send_message([])).text, "ok")
                        else:
                            self.assertEqual([p async for p in client.async_stream_message([])], ["ok"])

                        self.assertEqual(len(session.calls), 3)
                        self.assertEqual(session.calls[0]["headers"], {})
                        self.assertEqual(session.calls[1]["headers"], {})
                        self.assertFalse(session.calls[0]["allow_redirects"])
                        self.assertFalse(session.calls[1]["allow_redirects"])
                        self.assertEqual(session.calls[1]["url"], f"{client.base_url}/v1/health")
                        self.assertEqual(session.calls[2]["headers"]["Authorization"], "Bearer profile-key")

    async def test_native_probe_rejects_non_fail_closed_routing_before_credentials(self):
        for profile in ("worker", "default"):
            with self.subTest(profile=profile):
                session = FakeSession(
                    [
                        self._health_response(),
                        FakeResponse(json_data={"data": [{"id": "hermes-agent"}]}),
                    ]
                )
                client = HermesApiClient(
                    session=session,
                    host="agent.local",
                    port=8443,
                    api_key="must-not-leak",
                    profile=profile,
                    profile_route="native",
                )

                with self.assertRaises(HermesConnectionError):
                    await client.async_check_connection()

                self.assertEqual(
                    [call["url"] for call in session.calls],
                    ["https://agent.local:8443/p/_/v1/health"],
                )
                self.assertEqual(session.calls[0]["headers"], {})
                self.assertFalse(session.calls[0]["allow_redirects"])

    async def test_connection_probe_falls_back_to_models_when_legacy_health_is_missing(self):
        session = FakeSession(
            [
                FakeResponse(status=404),
                FakeResponse(
                    json_data={
                        "data": [
                            {"id": "hermes-agent", "owned_by": "hermes"}
                        ]
                    }
                ),
            ]
        )
        client = HermesApiClient(
            session=session,  # type: ignore[arg-type]
            host="agent.local",
            port=8443,
            api_key="legacy-key",
            profile="worker",
        )

        self.assertTrue(await client.async_check_connection())
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertEqual(
            session.calls[1]["headers"],
            {"Authorization": "Bearer legacy-key"},
        )
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/profile/worker/v1/health",
                "https://agent.local:8443/profile/worker/v1/models",
            ],
        )

    async def test_native_probe_rejects_ambiguous_legacy_health_before_credentials(self):
        session = FakeSession(
            [
                FakeResponse(status=404),
                FakeResponse(status=404),
                FakeResponse(
                    json_data={
                        "data": [
                            {"id": "hermes-agent", "owned_by": "hermes"}
                        ]
                    }
                ),
            ]
        )
        client = HermesApiClient(
            session=session,  # type: ignore[arg-type]
            host="agent.local",
            port=8443,
            api_key="must-not-leak",
            profile="worker",
            profile_route="native",
        )

        with self.assertRaises(HermesConnectionError):
            await client.async_check_connection()

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/p/_/v1/health",
                "https://agent.local:8443/p/worker/v1/health",
            ],
        )
        self.assertTrue(all(call["headers"] == {} for call in session.calls))

    async def test_native_runtime_requests_reverify_routing_before_credentials(self):
        for operation in ("models", "non_streaming", "streaming"):
            with self.subTest(operation=operation):
                session = FakeSession([self._health_response()])
                client = HermesApiClient(
                    session=session,  # type: ignore[arg-type]
                    host="agent.local",
                    port=8443,
                    api_key="must-not-leak",
                    profile="worker",
                    profile_route="native",
                )

                with self.assertRaises(HermesConnectionError):
                    if operation == "models":
                        await client.async_get_models()
                    elif operation == "non_streaming":
                        await client.async_send_message(
                            [{"role": "user", "content": "hello"}]
                        )
                    else:
                        async for _ in client.async_stream_message(
                            [{"role": "user", "content": "hello"}]
                        ):
                            pass

                self.assertEqual(
                    [call["url"] for call in session.calls],
                    ["https://agent.local:8443/p/_/v1/health"],
                )
                self.assertEqual(session.calls[0]["headers"], {})
                self.assertFalse(session.calls[0]["allow_redirects"])

    async def test_legacy_fallback_rejects_generic_openai_models_response(self):
        session = FakeSession(
            [
                FakeResponse(status=404),
                FakeResponse(
                    json_data={
                        "data": [{"id": "generic-model", "owned_by": "other"}]
                    }
                ),
            ]
        )
        client = HermesApiClient(
            session,  # type: ignore[arg-type]
            "agent.local",
            8443,
            api_key="key",
        )

        with self.assertRaises(HermesConnectionError):
            await client.async_check_connection()

    async def test_connection_probe_rejects_non_hermes_health_body(self):
        session = FakeSession([FakeResponse(json_data={"status": "ok"})])
        client = HermesApiClient(session, "agent.local", 8443)

        with self.assertRaises(HermesConnectionError):
            await client.async_check_connection()

    async def test_connection_probe_rejects_health_redirect(self):
        session = FakeSession(
            [
                FakeResponse(
                    status=302,
                    json_data={"status": "ok", "platform": "hermes-agent"},
                )
            ]
        )
        client = HermesApiClient(session, "agent.local", 8443)

        with self.assertRaises(HermesConnectionError):
            await client.async_check_connection()

    def test_base_url_uses_root_for_blank_profile(self):
        client = HermesApiClient(
            session=FakeSession([]),
            host="agent.local",
            port=8443,
            profile="",
        )

        self.assertEqual(client.base_url, "https://agent.local:8443")

    def test_base_url_uses_root_for_blank_native_profile(self):
        client = HermesApiClient(
            session=FakeSession([]),
            host="agent.local",
            port=8443,
            profile="",
            profile_route="native",
        )

        self.assertEqual(client.base_url, "https://agent.local:8443")

    def test_base_url_normalizes_valid_profile(self):
        client = HermesApiClient(
            session=FakeSession([]),
            host="agent.local",
            port=8443,
            profile=" assistant_2 ",
        )

        self.assertEqual(
            client.base_url,
            "https://agent.local:8443/profile/assistant_2",
        )

    def test_base_url_uses_native_route_and_canonical_profile(self):
        client = HermesApiClient(
            session=FakeSession([]),
            host="agent.local",
            port=8443,
            profile=" Worker-Bot_2 ",
            profile_route="native",
        )

        self.assertEqual(
            client.base_url,
            "https://agent.local:8443/p/worker-bot_2",
        )

    def test_base_url_rejects_unknown_route_family(self):
        with self.assertRaises(ValueError):
            HermesApiClient(
                session=FakeSession([]),
                host="agent.local",
                port=8443,
                profile="worker",
                profile_route="/tenant",
            )

    def test_base_url_normalizes_dns_host_and_brackets_ipv6(self):
        dns_client = HermesApiClient(FakeSession([]), " AGENT.LOCAL. ", 8443)
        ipv6_client = HermesApiClient(FakeSession([]), "[2001:0db8::1]", 8443)

        self.assertEqual(dns_client.base_url, "https://agent.local:8443")
        self.assertEqual(ipv6_client.base_url, "https://[2001:db8::1]:8443")

    def test_base_url_rejects_non_host_input(self):
        for host in (
            "agent.local/path",
            "user@agent.local",
            "agent.local?query=1",
            "agent.local#fragment",
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                HermesApiClient(FakeSession([]), host, 8443)

    def test_base_url_rejects_invalid_profile(self):
        with self.assertRaises(ValueError):
            HermesApiClient(
                session=FakeSession([]),
                host="agent.local",
                port=8443,
                profile="../assistant",
            )

    def test_base_url_rejects_reserved_native_profile(self):
        with self.assertRaises(ValueError):
            HermesApiClient(
                session=FakeSession([]),
                host="agent.local",
                port=8443,
                profile="hermes",
                profile_route="native",
            )

    async def test_profile_base_url_is_used_by_every_request_family(self):
        chunks = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        session = FakeSession(
            [
                self._health_response(),
                FakeResponse(json_data={"data": [{"id": "hermes-agent"}]}),
                FakeResponse(json_data={"data": []}),
                FakeResponse(json_data={"choices": [{"message": {"content": "ok"}}]}),
                FakeResponse(chunks=chunks),
            ]
        )
        client = HermesApiClient(
            session=session,
            host="agent.local",
            port=8443,
            profile="worker",
        )

        self.assertTrue(await client.async_check_connection())
        await client.async_get_models()
        await client.async_send_message([{"role": "user", "content": "hello"}])
        self.assertEqual(
            [
                part
                async for part in client.async_stream_message(
                    [{"role": "user", "content": "hello"}]
                )
            ],
            ["ok"],
        )

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/profile/worker/v1/health",
                "https://agent.local:8443/profile/worker/v1/models",
                "https://agent.local:8443/profile/worker/v1/models",
                "https://agent.local:8443/profile/worker/v1/chat/completions",
                "https://agent.local:8443/profile/worker/v1/chat/completions",
            ],
        )

    async def test_native_base_url_is_used_by_every_request_family(self):
        chunks = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        session = FakeSession(
            [
                FakeResponse(status=404),
                self._health_response(),
                FakeResponse(json_data={"data": [{"id": "hermes-agent"}]}),
                FakeResponse(status=404),
                self._health_response(),
                FakeResponse(json_data={"data": []}),
                FakeResponse(status=404),
                self._health_response(),
                FakeResponse(json_data={"choices": [{"message": {"content": "ok"}}]}),
                FakeResponse(status=404),
                self._health_response(),
                FakeResponse(chunks=chunks),
            ]
        )
        client = HermesApiClient(
            session=session,
            host="agent.local",
            port=8443,
            api_key="profile-key",
            profile="Worker-Bot",
            profile_route="native",
        )

        self.assertTrue(await client.async_check_connection())
        await client.async_get_models()
        await client.async_send_message([{"role": "user", "content": "hello"}])
        self.assertEqual(
            [
                part
                async for part in client.async_stream_message(
                    [{"role": "user", "content": "hello"}]
                )
            ],
            ["ok"],
        )

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/p/_/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/models",
                "https://agent.local:8443/p/_/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/models",
                "https://agent.local:8443/p/_/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/chat/completions",
                "https://agent.local:8443/p/_/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/chat/completions",
            ],
        )
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertEqual(
            session.calls[2]["headers"],
            {"Authorization": "Bearer profile-key"},
        )
        self.assertFalse(session.calls[2]["allow_redirects"])
        for index in (0, 1, 3, 4, 6, 7, 9, 10):
            self.assertEqual(session.calls[index]["headers"], {})
            self.assertFalse(session.calls[index]["allow_redirects"])
        for index in (5, 8, 11):
            self.assertEqual(
                session.calls[index]["headers"],
                {"Authorization": "Bearer profile-key"},
            )

    async def test_health_non_success_status_raises_connection_error(self):
        client = HermesApiClient(
            session=FakeSession([FakeResponse(status=500)]),
            host="agent.local",
            port=8443,
        )

        with self.assertRaises(HermesConnectionError):
            await client.async_check_connection()

    async def test_health_unauthorized_statuses_are_connection_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client = HermesApiClient(
                    session=FakeSession([FakeResponse(status=status)]),
                    host="agent.local",
                    port=8443,
                )

                with self.assertRaises(HermesConnectionError):
                    await client.async_check_connection()

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

    async def test_streaming_ignores_custom_sse_events(self):
        chunks = [
            "event: hermes.tool.progress\n",
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": "do not speak"}}], "tool": "terminal"}
            )
            + "\n",
            "\n",
            "data: "
            + json.dumps({"choices": [{"delta": {"content": "Safe answer"}}]})
            + "\n",
            "data: [DONE]\n",
        ]
        session = FakeSession([FakeResponse(chunks=chunks)])
        client = HermesApiClient(session=session, host="agent.local", port=8443)

        parts = []
        async for part in client.async_stream_message(
            [{"role": "user", "content": "hi"}]
        ):
            parts.append(part)

        self.assertEqual(parts, ["Safe answer"])

    async def test_streaming_ignores_tool_call_deltas(self):
        chunks = [
            "data: "
            + json.dumps({"choices": [{"delta": {"tool_calls": [{"id": "call-1"}]}}]})
            + "\n",
            "data: " + json.dumps({"choices": [{"delta": {"content": "Done"}}]}) + "\n",
            "data: [DONE]\n",
        ]
        session = FakeSession([FakeResponse(chunks=chunks)])
        client = HermesApiClient(session=session, host="agent.local", port=8443)

        parts = []
        async for part in client.async_stream_message(
            [{"role": "user", "content": "hi"}]
        ):
            parts.append(part)

        self.assertEqual(parts, ["Done"])

    async def test_stream_terminal_failure_and_incomplete_are_sanitized(self):
        terminal_cases = (
            {"choices": [{"finish_reason": "error", "delta": {}}],
             "hermes": {"failed": True, "error": "private-server-detail"},
             "error": "private-server-detail"},
            {"choices": [{"finish_reason": "length", "delta": {}}],
             "hermes": {"completed": False, "partial": True,
                        "error": "private-server-detail"}},
            {"choices": [{"finish_reason": "stop", "delta": {}}],
             "hermes": {"completed": False}},
            {"choices": [{"finish_reason": "stop", "delta": {}}],
             "hermes": {"failed": True}},
            {"choices": [{"finish_reason": "stop", "delta": {}}],
             "error": "private-server-detail"},
            {"choices": [{"finish_reason": "length", "delta": {}}]},
        )
        for terminal in terminal_cases:
            for prior_content in (False, True):
                with self.subTest(terminal=terminal, prior_content=prior_content):
                    chunks = []
                    if prior_content:
                        chunks.append('data: ' + json.dumps({
                            "choices": [{"delta": {"content": "safe reply"}}]
                        }) + '\n\n')
                    chunks += [
                        'data: ' + json.dumps(terminal) + '\n\n',
                        'data: [DONE]\n\n',
                    ]
                    session = FakeSession([FakeResponse(chunks=chunks)])
                    client = HermesApiClient(session, 'agent.local', 8443)
                    delivered = []
                    with self.assertRaises(HermesConnectionError) as caught:
                        async for part in client.async_stream_message([{"role": "user", "content": "hi"}]):
                            delivered.append(part)
                    self.assertEqual(delivered, ["safe reply"] if prior_content else [])
                    self.assertEqual(caught.exception.error_category, "server")
                    self.assertNotIn("private-server-detail", str(caught.exception))
                    self.assertEqual(len(session.calls), 1)

    async def test_stream_error_event_is_not_a_successful_done(self):
        chunks = [
            'event: error\n',
            'data: ' + json.dumps({"error": "private-server-detail"}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        client = HermesApiClient(FakeSession([FakeResponse(chunks=chunks)]), "agent.local", 8443)
        with self.assertRaises(HermesConnectionError) as caught:
            async for _ in client.async_stream_message([{"role": "user", "content": "hi"}]):
                pass
        self.assertEqual(caught.exception.error_category, "server")
        self.assertNotIn("private-server-detail", str(caught.exception))

    async def test_stream_does_not_deliver_content_after_error_terminal(self):
        chunks = [
            'data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": "error"}]}) + '\n\n',
            'data: ' + json.dumps({"choices": [{"delta": {"content": "should not speak"}}]}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        session = FakeSession([FakeResponse(chunks=chunks)])
        client = HermesApiClient(session, "agent.local", 8443)
        delivered = []
        with self.assertRaises(HermesConnectionError):
            async for part in client.async_stream_message([{"role": "user", "content": "hi"}]):
                delivered.append(part)
        self.assertEqual(delivered, [])

    async def test_nonstream_terminal_failure_and_incomplete_are_sanitized(self):
        for reason, hermes, top_error in (
            ("error", {"failed": True, "error": "private-server-detail"}, None),
            ("length", {"partial": True, "completed": False}, None),
            ("stop", {"completed": False}, None),
            ("stop", {}, "private-server-detail"),
            ("length", {}, None),
        ):
            with self.subTest(reason=reason, hermes=hermes, top_error=top_error):
                payload = {"choices": [{"finish_reason": reason,
                                        "message": {"content": "must not succeed"}}],
                           "hermes": hermes}
                if top_error:
                    payload["error"] = top_error
                session = FakeSession([FakeResponse(json_data=payload)])
                client = HermesApiClient(session, 'agent.local', 8443)
                with self.assertRaises(HermesConnectionError) as caught:
                    await client.async_send_message([{"role": "user", "content": "hi"}])
                self.assertEqual(caught.exception.error_category, "server")
                self.assertNotIn("private-server-detail", str(caught.exception))
                self.assertEqual(len(session.calls), 1)

    async def test_nonstream_present_non_null_error_is_failure_even_when_falsey(self):
        for error in ({}, "", [], 0, False):
            with self.subTest(error=error):
                session = FakeSession([FakeResponse(json_data={
                    "choices": [{"finish_reason": "stop", "message": {"content": "safe"}}],
                    "error": error,
                })])
                client = HermesApiClient(session, "agent.local", 8443)
                with self.assertRaises(HermesConnectionError) as caught:
                    await client.async_send_message([{"role": "user", "content": "hi"}])
                self.assertEqual(caught.exception.error_category, "server")
                self.assertEqual(str(caught.exception), "Hermes API reported an incomplete chat response")
                self.assertEqual(len(session.calls), 1)

    async def test_nonstream_null_or_absent_error_keeps_valid_completion(self):
        for error_present in (False, True):
            with self.subTest(error_present=error_present):
                payload: dict[str, Any] = {"choices": [{"finish_reason": "stop", "message": {"content": "safe"}}]}
                if error_present:
                    payload["error"] = None
                session = FakeSession([FakeResponse(json_data=payload)])
                client = HermesApiClient(session, "agent.local", 8443)
                self.assertEqual((await client.async_send_message([{"role": "user", "content": "hi"}])).text, "safe")
                self.assertEqual(len(session.calls), 1)

    async def test_stream_present_non_null_error_is_failure_even_when_falsey(self):
        for error in ({}, "", [], 0, False):
            for prior_content in (False, True):
                with self.subTest(error=error, prior_content=prior_content):
                    chunks = []
                    if prior_content:
                        chunks.append('data: ' + json.dumps({
                            "choices": [{"delta": {"content": "safe"}}]
                        }) + '\n\n')
                    chunks += [
                        'data: ' + json.dumps({
                            "choices": [{"finish_reason": "stop", "delta": {}}],
                            "error": error,
                        }) + '\n\n',
                        'data: [DONE]\n\n',
                    ]
                    session = FakeSession([FakeResponse(chunks=chunks)])
                    client = HermesApiClient(session, "agent.local", 8443)
                    delivered = []
                    with self.assertRaises(HermesConnectionError) as caught:
                        async for part in client.async_stream_message([{"role": "user", "content": "hi"}]):
                            delivered.append(part)
                    self.assertEqual(delivered, ["safe"] if prior_content else [])
                    self.assertEqual(caught.exception.error_category, "server")
                    self.assertEqual(str(caught.exception), "Hermes API reported an incomplete chat response")
                    self.assertEqual(len(session.calls), 1)

    async def test_stream_present_non_null_error_without_done_keeps_server_category(self):
        for prior_content in (False, True):
            with self.subTest(prior_content=prior_content):
                chunks = []
                if prior_content:
                    chunks.append('data: ' + json.dumps({
                        "choices": [{"delta": {"content": "safe"}}]
                    }) + '\n\n')
                chunks.append('data: ' + json.dumps({
                    "choices": [{"finish_reason": "stop", "delta": {}}], "error": {},
                }) + '\n\n')
                session = FakeSession([FakeResponse(chunks=chunks)])
                client = HermesApiClient(session, "agent.local", 8443)
                delivered = []
                with self.assertRaises(HermesConnectionError) as caught:
                    async for part in client.async_stream_message([{"role": "user", "content": "hi"}]):
                        delivered.append(part)
                self.assertEqual(delivered, ["safe"] if prior_content else [])
                self.assertEqual(caught.exception.error_category, "server")
                self.assertEqual(str(caught.exception), "Hermes API reported an incomplete chat response")
                self.assertEqual(len(session.calls), 1)

    async def test_stream_null_or_absent_error_keeps_valid_completion(self):
        for error_present in (False, True):
            with self.subTest(error_present=error_present):
                terminal: dict[str, Any] = {"choices": [{"finish_reason": "stop", "delta": {}}]}
                if error_present:
                    terminal["error"] = None
                session = FakeSession([FakeResponse(chunks=[
                    'data: ' + json.dumps({"choices": [{"delta": {"content": "safe"}}]}) + '\n\n',
                    'data: ' + json.dumps(terminal) + '\n\n',
                    'data: [DONE]\n\n',
                ])])
                client = HermesApiClient(session, "agent.local", 8443)
                self.assertEqual([part async for part in client.async_stream_message(
                    [{"role": "user", "content": "hi"}])], ["safe"])
                self.assertEqual(len(session.calls), 1)

    async def test_nonstream_completed_false_header_is_not_success(self):
        session = FakeSession([FakeResponse(
            headers={"X-Hermes-Completed": "false"},
            json_data={"choices": [{"finish_reason": "stop",
                                    "message": {"content": "not complete"}}]},
        )])
        client = HermesApiClient(session, "agent.local", 8443)
        with self.assertRaises(HermesConnectionError) as caught:
            await client.async_send_message([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.error_category, "server")
        self.assertEqual(len(session.calls), 1)

    async def test_successful_terminal_reason_still_completes(self):
        terminal = {"choices": [{"finish_reason": "stop", "delta": {}}],
                    "hermes": {"completed": True, "partial": False, "failed": False}}
        session = FakeSession([FakeResponse(chunks=[
            'data: ' + json.dumps({"choices": [{"delta": {"content": "safe"}}]}) + '\n\n',
            'data: ' + json.dumps(terminal) + '\n\n',
            'data: [DONE]\n\n',
        ])])
        client = HermesApiClient(session, "agent.local", 8443)
        parts = [part async for part in client.async_stream_message(
            [{"role": "user", "content": "hi"}])]
        self.assertEqual(parts, ["safe"])

    async def test_chat_http_failures_have_typed_categories_without_raw_body(self):
        for streaming in (False, True):
            for status, category in ((401, "auth"), (403, "auth"),
                                     (400, "unreachable"), (429, "unreachable"),
                                     (500, "server"), (503, "server")):
                with self.subTest(streaming=streaming, status=status):
                    session = FakeSession([FakeResponse(
                        status=status, text_data="private-server-detail"
                    )])
                    client = HermesApiClient(session, "agent.local", 8443)
                    with self.assertRaises(HermesApiError) as caught:
                        if streaming:
                            async for _ in client.async_stream_message([{"role": "user", "content": "hi"}]):
                                pass
                        else:
                            await client.async_send_message([{"role": "user", "content": "hi"}])
                    if category == "auth":
                        self.assertIsInstance(caught.exception, HermesAuthError)
                    else:
                        self.assertIsInstance(caught.exception, HermesConnectionError)
                        self.assertEqual(caught.exception.status_code, status)
                        self.assertEqual(caught.exception.error_category, category)
                    self.assertNotIsInstance(caught.exception, HermesStreamSetupError)
                    self.assertNotIn("private-server-detail", str(caught.exception))
                    self.assertEqual(len(session.calls), 1)

    async def test_chat_transport_timeout_is_typed_before_and_after_stream_content(self):
        class TimedOutResponse(FakeResponse):
            async def __aenter__(self):
                raise asyncio.TimeoutError("private-server-detail")

        class TimedOutStream(FakeResponse):
            async def iter_any(self):
                yield b'data: {"choices": [{"delta": {"content": "safe"}}]}\n\n'
                raise asyncio.TimeoutError("private-server-detail")

        class DisconnectedResponse(FakeResponse):
            async def __aenter__(self):
                raise FakeClientError("private-server-detail")

        for streaming, response, expected, category in (
            (False, TimedOutResponse(), [], "timeout"),
            (True, TimedOutResponse(), [], "timeout"),
            (True, TimedOutStream(), ["safe"], "timeout"),
            (False, DisconnectedResponse(), [], "unreachable"),
            (True, DisconnectedResponse(), [], "unreachable"),
        ):
            with self.subTest(streaming=streaming, expected=expected, category=category):
                session = FakeSession([response])
                client = HermesApiClient(session, "agent.local", 8443)
                delivered = []
                with self.assertRaises(HermesConnectionError) as caught:
                    if streaming:
                        async for part in client.async_stream_message([{"role": "user", "content": "hi"}]):
                            delivered.append(part)
                    else:
                        await client.async_send_message([{"role": "user", "content": "hi"}])
                self.assertEqual(delivered, expected)
                self.assertEqual(caught.exception.error_category, category)
                self.assertNotIn("private-server-detail", str(caught.exception))
                self.assertEqual(len(session.calls), 1)

    async def test_known_stream_failure_without_done_remains_server_failure(self):
        for terminal in (
            {"choices": [{"finish_reason": "error", "delta": {}}]},
            {"choices": [{"finish_reason": "length", "delta": {}}]},
            {"hermes": {"failed": True, "error": "private-server-detail"}},
            {"hermes": {"completed": False}},
            {"error": "private-server-detail"},
        ):
            for prior_content in (False, True):
                with self.subTest(terminal=terminal, prior_content=prior_content):
                    chunks = []
                    if prior_content:
                        chunks.append('data: {"choices": [{"delta": {"content": "safe"}}]}\n\n')
                    chunks.append('data: ' + json.dumps(terminal) + '\n\n')
                    session = FakeSession([FakeResponse(chunks=chunks)])
                    delivered = []
                    with self.assertRaises(HermesConnectionError) as caught:
                        async for part in HermesApiClient(session, "agent.local", 8443).async_stream_message(
                            [{"role": "user", "content": "hi"}]
                        ):
                            delivered.append(part)
                    self.assertEqual(delivered, ["safe"] if prior_content else [])
                    self.assertEqual(caught.exception.error_category, "server")
                    self.assertNotIn("private-server-detail", str(caught.exception))
                    self.assertEqual(len(session.calls), 1)

        for prior_content in (False, True):
            chunks = []
            if prior_content:
                chunks.append('data: {"choices": [{"delta": {"content": "safe"}}]}\n\n')
            chunks.append('event: error\ndata: {"error": "private-server-detail"}\n\n')
            session = FakeSession([FakeResponse(chunks=chunks)])
            delivered = []
            with self.assertRaises(HermesConnectionError) as caught:
                async for part in HermesApiClient(session, "agent.local", 8443).async_stream_message(
                    [{"role": "user", "content": "hi"}]
                ):
                    delivered.append(part)
            self.assertEqual(delivered, ["safe"] if prior_content else [])
            self.assertEqual(caught.exception.error_category, "server")
            self.assertNotIn("private-server-detail", str(caught.exception))
            self.assertEqual(len(session.calls), 1)

    async def test_known_failure_precedes_transport_close_category(self):
        class ClosedStream(FakeResponse):
            async def iter_any(self):
                yield b'data: {"choices": [{"delta": {"content": "safe"}}]}\n\n'
                yield b'data: {"choices": [{"finish_reason": "error", "delta": {}}]}\n\n'
                raise FakeClientError("private-server-detail")

        session = FakeSession([ClosedStream()])
        delivered = []
        with self.assertRaises(HermesConnectionError) as caught:
            async for part in HermesApiClient(session, "agent.local", 8443).async_stream_message(
                [{"role": "user", "content": "hi"}]
            ):
                delivered.append(part)
        self.assertEqual(delivered, ["safe"])
        self.assertEqual(caught.exception.error_category, "server")
        self.assertNotIn("private-server-detail", str(caught.exception))
        self.assertEqual(len(session.calls), 1)

    async def test_unknown_stream_eof_remains_unreachable(self):
        session = FakeSession([FakeResponse(chunks=[
            'data: {"choices": [{"delta": {"content": "safe"}}]}\n\n'
        ])])
        delivered = []
        with self.assertRaises(HermesConnectionError) as caught:
            async for part in HermesApiClient(session, "agent.local", 8443).async_stream_message(
                [{"role": "user", "content": "hi"}]
            ):
                delivered.append(part)
        self.assertEqual(delivered, ["safe"])
        self.assertEqual(caught.exception.error_category, "unreachable")

    async def test_streaming_http_status_is_ambiguous_not_setup_error(self):
        for status in (400, 429, 500, 503):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse(status=status, text_data="synthetic failure")])
                client = HermesApiClient(session=session, host="agent.local", port=8443)

                with self.assertRaises(HermesApiError) as caught:
                    async for _part in client.async_stream_message(
                        [{"role": "user", "content": "hi"}]
                    ):
                        pass
                self.assertNotIsInstance(caught.exception, HermesStreamSetupError)
                self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
