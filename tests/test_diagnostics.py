from __future__ import annotations

import asyncio
import json
import logging
import re
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_api import FakeResponse, FakeSession
from tests.test_support import FakeConfigEntry, FakeHass
from custom_components.hermes_conversation.api import (
    HermesApiClient,
    HermesConnectionError,
)
from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_USE_SSL,
    DOMAIN,
)
from custom_components.hermes_conversation.conversation import HermesConversationAgent

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = "sk-secret-SHOULD-NOT-LEAK"
RAW_BODY = "raw-error-body token=sk-secret-SHOULD-NOT-LEAK pid=99999"
FORBIDDEN_FRAGMENTS = (
    SECRET_KEY,
    RAW_BODY,
    "token=sk-secret",
    "Authorization",
    "Bearer ",
)


def _health_ok() -> FakeResponse:
    return FakeResponse(
        json_data={"status": "ok", "platform": "hermes-agent", "version": "test"}
    )


def _models_ok() -> FakeResponse:
    return FakeResponse(json_data={"data": [{"id": "hermes-agent", "owned_by": "hermes"}]})


def _detailed_ok(**overrides: Any) -> FakeResponse:
    payload = {
        "status": "ok",
        "platform": "hermes-agent",
        "version": "0.99.0-secret-build",
        "readiness": {
            "status": "ok",
            "checks": {"config": {"status": "ok", "detail": RAW_BODY}},
        },
        "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected"}},
        "active_agents": 4,
        "gateway_busy": True,
        "gateway_drainable": True,
        "exit_reason": None,
        "pid": 4242,
        "updated_at": "2026-04-14T00:00:00Z",
    }
    payload.update(overrides)
    return FakeResponse(json_data=payload)


def _entry(**overrides: Any) -> FakeConfigEntry:
    data = {
        CONF_HOST: "agent.local",
        CONF_PORT: 8443,
        CONF_API_KEY: SECRET_KEY,
        CONF_USE_SSL: True,
    }
    data.update(overrides.pop("data", {}))
    return FakeConfigEntry(
        data=data,
        options=overrides.pop("options", {}),
        entry_id=overrides.pop("entry_id", "entry-1"),
        title=overrides.pop("title", "Hermes Agent"),
        **overrides,
    )


def _client(session, **kwargs: Any) -> HermesApiClient:
    params = {
        "session": session,
        "host": "agent.local",
        "port": 8443,
        "api_key": SECRET_KEY,
    }
    params.update(kwargs)
    return HermesApiClient(**params)


def _serialized(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        if record.exc_text:
            self.messages.append(record.exc_text)

    def combined(self) -> str:
        return "\n".join(self.messages)


def _attach_log_capture() -> tuple[LogCapture, list[logging.Logger]]:
    capture = LogCapture()
    loggers = [
        logging.getLogger("custom_components.hermes_conversation"),
        logging.getLogger("custom_components.hermes_conversation.api"),
        logging.getLogger("custom_components.hermes_conversation.coordinator"),
        logging.getLogger("custom_components.hermes_conversation.entity"),
        logging.getLogger("custom_components.hermes_conversation.sensor"),
        logging.getLogger("custom_components.hermes_conversation.binary_sensor"),
    ]
    for logger in loggers:
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
    return capture, loggers


def _detach_log_capture(capture: LogCapture, loggers: list[logging.Logger]) -> None:
    for logger in loggers:
        logger.removeHandler(capture)


class RaisingSession(FakeSession):
    def get(self, url, *, headers=None, timeout=None, ssl=None, allow_redirects=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "timeout": timeout,
                "ssl": ssl,
                "allow_redirects": allow_redirects,
            }
        )
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class BrokenJsonResponse(FakeResponse):
    async def json(self):
        raise ValueError("not json: " + RAW_BODY)


async def _refresh(hass, entry, client):
    from custom_components.hermes_conversation.coordinator import (
        HermesDiagnosticsCoordinator,
    )

    coordinator = HermesDiagnosticsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    return coordinator


def _shutdown_hook_count(entry, coordinator) -> int:
    return sum(
        1
        for callback in getattr(entry, "_on_unload", [])
        if getattr(callback, "__self__", None) is coordinator
        and getattr(callback, "__name__", None) == "async_shutdown"
    )


class DiagnosticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_probe_uses_same_base_url_for_detailed_health(self):
        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/v1/health",
                "https://agent.local:8443/v1/models",
                "https://agent.local:8443/health/detailed",
            ],
        )
        self.assertTrue(coordinator.data.connected)
        self.assertTrue(coordinator.data.health_available)
        self.assertEqual(coordinator.data.health_status, "ok")
        self.assertFalse(session.calls[-1]["allow_redirects"])
        self.assertEqual(session.calls[-1]["timeout"].total, 10)
        self.assertEqual(
            session.calls[-1]["headers"],
            {"Authorization": f"Bearer {SECRET_KEY}"},
        )

    async def test_addon_profile_keeps_detailed_health_on_selected_route(self):
        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        client = _client(session, profile="worker")
        coordinator = await _refresh(FakeHass(), _entry(), client)

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/profile/worker/v1/health",
                "https://agent.local:8443/profile/worker/v1/models",
                "https://agent.local:8443/profile/worker/health/detailed",
            ],
        )
        self.assertTrue(coordinator.data.connected)
        self.assertNotIn("https://agent.local:8443/health/detailed", [c["url"] for c in session.calls])
        self.assertNotIn(
            "https://agent.local:8443/v1/health",
            [c["url"] for c in session.calls],
        )

    async def test_native_profile_reuses_fail_closed_canaries_then_same_base_detailed(self):
        session = FakeSession(
            [
                FakeResponse(status=404),
                _health_ok(),
                _models_ok(),
                FakeResponse(status=404),
                _health_ok(),
                _detailed_ok(),
            ]
        )
        client = _client(session, profile="Worker-Bot", profile_route="native")
        coordinator = await _refresh(FakeHass(), _entry(), client)

        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/p/hermes/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/models",
                "https://agent.local:8443/p/hermes/v1/health",
                "https://agent.local:8443/p/worker-bot/v1/health",
                "https://agent.local:8443/p/worker-bot/health/detailed",
            ],
        )
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertEqual(session.calls[1]["headers"], {})
        self.assertEqual(
            session.calls[2]["headers"],
            {"Authorization": f"Bearer {SECRET_KEY}"},
        )
        self.assertEqual(session.calls[3]["headers"], {})
        self.assertEqual(session.calls[4]["headers"], {})
        self.assertEqual(
            session.calls[5]["headers"],
            {"Authorization": f"Bearer {SECRET_KEY}"},
        )
        self.assertTrue(all(call["allow_redirects"] is False for call in session.calls))
        self.assertTrue(coordinator.data.connected)
        self.assertEqual(coordinator.data.health_status, "ok")

    async def test_native_canary_failure_never_sends_credentials_or_detailed_request(self):
        session = FakeSession([_health_ok()])
        client = _client(session, profile="worker", profile_route="native")
        coordinator = await _refresh(FakeHass(), _entry(), client)

        self.assertFalse(coordinator.data.connected)
        self.assertEqual(
            [call["url"] for call in session.calls],
            ["https://agent.local:8443/p/hermes/v1/health"],
        )
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertFalse(coordinator.data.health_available)
        self.assertIsNone(coordinator.data.health_status)

    async def test_direct_native_detailed_failing_canary_never_sends_key_or_selected_request(
        self,
    ):
        session = FakeSession([_health_ok(), _detailed_ok()])
        client = _client(session, profile="worker", profile_route="native")

        with self.assertRaises(HermesConnectionError):
            await client.async_get_detailed_health()

        self.assertEqual(
            [call["url"] for call in session.calls],
            ["https://agent.local:8443/p/hermes/v1/health"],
        )
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertEqual(len(session.responses), 1)

    async def test_direct_native_detailed_successful_canary_uses_selected_route(self):
        session = FakeSession(
            [FakeResponse(status=404), _health_ok(), _detailed_ok()]
        )
        client = _client(session, profile="worker", profile_route="native")

        result = await client.async_get_detailed_health()

        self.assertTrue(result.available)
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/p/hermes/v1/health",
                "https://agent.local:8443/p/worker/v1/health",
                "https://agent.local:8443/p/worker/health/detailed",
            ],
        )
        self.assertEqual(session.calls[0]["headers"], {})
        self.assertEqual(session.calls[1]["headers"], {})
        self.assertEqual(
            session.calls[2]["headers"],
            {"Authorization": f"Bearer {SECRET_KEY}"},
        )
        self.assertTrue(all(call["allow_redirects"] is False for call in session.calls))

    async def test_native_route_flip_after_models_fail_closes_without_sending_detailed(
        self,
    ):
        session = FakeSession(
            [
                FakeResponse(status=404),
                _health_ok(),
                _models_ok(),
                _health_ok(),
                _detailed_ok(),
            ]
        )
        client = _client(session, profile="worker", profile_route="native")
        coordinator = await _refresh(FakeHass(), _entry(), client)

        self.assertFalse(coordinator.data.connected)
        self.assertFalse(coordinator.data.health_available)
        self.assertIsNone(coordinator.data.health_status)
        self.assertIsNone(coordinator.data.latency_ms)
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/p/hermes/v1/health",
                "https://agent.local:8443/p/worker/v1/health",
                "https://agent.local:8443/p/worker/v1/models",
                "https://agent.local:8443/p/hermes/v1/health",
            ],
        )
        self.assertEqual(session.calls[3]["headers"], {})
        self.assertEqual(len(session.responses), 1)
        self.assertTrue(coordinator.last_update_success)

    async def test_native_detailed_optional_failures_after_valid_preflight_keep_basics(
        self,
    ):
        cases = (
            FakeResponse(status=404, text_data=RAW_BODY),
            BrokenJsonResponse(status=200, json_data={"status": "ok"}),
            asyncio.TimeoutError(),
        )
        for detailed in cases:
            with self.subTest(detailed=type(detailed).__name__):
                session = RaisingSession(
                    [
                        FakeResponse(status=404),
                        _health_ok(),
                        _models_ok(),
                        FakeResponse(status=404),
                        _health_ok(),
                        detailed,
                    ]
                )
                coordinator = await _refresh(
                    FakeHass(),
                    _entry(),
                    _client(session, profile="worker", profile_route="native"),
                )
                self.assertTrue(coordinator.data.connected)
                self.assertFalse(coordinator.data.health_available)
                self.assertIsNone(coordinator.data.health_status)
                self.assertIsNotNone(coordinator.data.latency_ms)
                self.assertEqual(
                    [call["url"] for call in session.calls],
                    [
                        "https://agent.local:8443/p/hermes/v1/health",
                        "https://agent.local:8443/p/worker/v1/health",
                        "https://agent.local:8443/p/worker/v1/models",
                        "https://agent.local:8443/p/hermes/v1/health",
                        "https://agent.local:8443/p/worker/v1/health",
                        "https://agent.local:8443/p/worker/health/detailed",
                    ],
                )
                self.assertEqual(session.calls[3]["headers"], {})
                self.assertEqual(session.calls[4]["headers"], {})
                self.assertEqual(
                    session.calls[5]["headers"],
                    {"Authorization": f"Bearer {SECRET_KEY}"},
                )
                self.assertIn(
                    coordinator.data.error_category,
                    {"unsupported", "malformed", "timeout"},
                )

    async def test_legacy_missing_public_health_still_authenticates_via_models(self):
        session = FakeSession(
            [FakeResponse(status=404), _models_ok(), _detailed_ok()]
        )
        coordinator = await _refresh(
            FakeHass(), _entry(), _client(session, profile="worker")
        )

        self.assertTrue(coordinator.data.connected)
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/profile/worker/v1/health",
                "https://agent.local:8443/profile/worker/v1/models",
                "https://agent.local:8443/profile/worker/health/detailed",
            ],
        )

    async def test_models_unauthorized_is_offline_not_healthy(self):
        session = FakeSession([_health_ok(), FakeResponse(status=401, text_data=RAW_BODY)])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))

        self.assertFalse(coordinator.data.connected)
        self.assertFalse(coordinator.data.health_available)
        self.assertEqual(coordinator.data.error_category, "auth")
        self.assertIsNone(coordinator.data.latency_ms)
        self.assertEqual(len(session.calls), 2)

    async def test_models_forbidden_is_offline(self):
        session = FakeSession([_health_ok(), FakeResponse(status=403, text_data=RAW_BODY)])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))

        self.assertFalse(coordinator.data.connected)
        self.assertEqual(coordinator.data.error_category, "auth")

    async def test_public_health_success_is_not_authentication(self):
        session = FakeSession([_health_ok(), FakeResponse(status=401)])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertFalse(coordinator.data.connected)
        self.assertNotEqual(coordinator.data.health_status, "ok")

    async def test_baseline_redirect_is_offline(self):
        session = FakeSession([FakeResponse(status=302)])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertFalse(coordinator.data.connected)
        self.assertEqual(coordinator.data.error_category, "redirect")
        self.assertFalse(coordinator.data.health_available)

    async def test_detailed_redirect_keeps_basic_connectivity_and_hides_health(self):
        session = FakeSession(
            [_health_ok(), _models_ok(), FakeResponse(status=302, text_data=RAW_BODY)]
        )
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertTrue(coordinator.data.connected)
        self.assertFalse(coordinator.data.health_available)
        self.assertIsNone(coordinator.data.health_status)
        self.assertEqual(coordinator.data.error_category, "unsupported")

    async def test_detailed_missing_or_unsupported_methods_hide_health_only(self):
        for status in (404, 405, 501):
            with self.subTest(status=status):
                session = FakeSession(
                    [_health_ok(), _models_ok(), FakeResponse(status=status, text_data=RAW_BODY)]
                )
                coordinator = await _refresh(FakeHass(), _entry(), _client(session))
                self.assertTrue(coordinator.data.connected)
                self.assertFalse(coordinator.data.health_available)
                self.assertIsNone(coordinator.data.health_status)
                self.assertIsNotNone(coordinator.data.latency_ms)
                self.assertEqual(coordinator.data.error_category, "unsupported")

    async def test_detailed_server_json_and_timeout_errors_hide_health_only(self):
        cases = (
            FakeResponse(status=500, text_data=RAW_BODY),
            BrokenJsonResponse(status=200, json_data={"status": "ok"}),
            asyncio.TimeoutError(),
        )
        for detailed in cases:
            with self.subTest(detailed=type(detailed).__name__):
                session = RaisingSession([_health_ok(), _models_ok(), detailed])
                coordinator = await _refresh(FakeHass(), _entry(), _client(session))
                self.assertTrue(coordinator.data.connected)
                self.assertFalse(coordinator.data.health_available)
                self.assertIsNone(coordinator.data.health_status)
                self.assertIn(coordinator.data.error_category, {"server", "malformed", "timeout"})

    async def test_detailed_unauthorized_is_conservative_offline(self):
        for status in (401, 403):
            with self.subTest(status=status):
                session = FakeSession(
                    [_health_ok(), _models_ok(), FakeResponse(status=status, text_data=RAW_BODY)]
                )
                coordinator = await _refresh(FakeHass(), _entry(), _client(session))
                self.assertFalse(coordinator.data.connected)
                self.assertFalse(coordinator.data.health_available)
                self.assertNotEqual(coordinator.data.health_status, "ok")
                self.assertEqual(coordinator.data.error_category, "auth")
                self.assertIsNone(coordinator.data.latency_ms)

    async def test_http_200_degraded_is_connected_and_degraded(self):
        session = FakeSession(
            [_health_ok(), _models_ok(), _detailed_ok(status="degraded")]
        )
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertTrue(coordinator.data.connected)
        self.assertTrue(coordinator.data.health_available)
        self.assertEqual(coordinator.data.health_status, "degraded")
        self.assertIsNone(coordinator.data.error_category)

    async def test_http_200_does_not_imply_healthy_without_whitelisted_status(self):
        session = FakeSession(
            [_health_ok(), _models_ok(), _detailed_ok(status="ready")]
        )
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertTrue(coordinator.data.connected)
        self.assertFalse(coordinator.data.health_available)
        self.assertIsNone(coordinator.data.health_status)

    async def test_malformed_nonhashable_status_does_not_crash(self):
        for status in (["ok"], {"code": "ok"}, 200, None):
            with self.subTest(status=status):
                session = FakeSession(
                    [_health_ok(), _models_ok(), _detailed_ok(status=status)]
                )
                coordinator = await _refresh(FakeHass(), _entry(), _client(session))
                self.assertTrue(coordinator.data.connected)
                self.assertFalse(coordinator.data.health_available)
                self.assertIsNone(coordinator.data.health_status)
                self.assertEqual(coordinator.data.error_category, "malformed")

    async def test_latency_measures_baseline_probe_only(self):
        times = iter([10.0, 10.25, 11.0])
        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        with mock.patch("time.monotonic", side_effect=lambda: next(times)):
            coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertEqual(coordinator.data.latency_ms, 250)
        self.assertTrue(coordinator.data.connected)

    async def test_offline_latency_is_unavailable(self):
        session = RaisingSession([asyncio.TimeoutError()])
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        self.assertFalse(coordinator.data.connected)
        self.assertIsNone(coordinator.data.latency_ms)
        self.assertEqual(coordinator.data.error_category, "timeout")

    async def test_probe_does_not_leak_secrets_or_raw_payloads(self):
        capture, loggers = _attach_log_capture()
        try:
            session = FakeSession(
                [_health_ok(), _models_ok(), FakeResponse(status=500, text_data=RAW_BODY)]
            )
            coordinator = await _refresh(FakeHass(), _entry(), _client(session))
            blob = "\n".join(
                [
                    _serialized(coordinator.data),
                    capture.combined(),
                    str(getattr(coordinator.data, "__dict__", {})),
                ]
            )
            for fragment in FORBIDDEN_FRAGMENTS:
                self.assertNotIn(fragment, blob)
            self.assertNotIn("4242", blob)
            self.assertNotIn("active_agents", blob)
            self.assertNotIn("gateway_busy", blob)
            self.assertNotIn("0.99.0-secret-build", blob)
        finally:
            _detach_log_capture(capture, loggers)


class DiagnosticsCoordinatorEntityTests(unittest.IsolatedAsyncioTestCase):
    def _entities(self, coordinator, entry):
        from custom_components.hermes_conversation.binary_sensor import (
            HermesApiConnectivityBinarySensor,
        )
        from custom_components.hermes_conversation.sensor import (
            HermesApiLatencySensor,
            HermesHealthSensor,
            HermesLastSuccessfulConnectionSensor,
        )

        return (
            HermesApiConnectivityBinarySensor(coordinator, entry),
            HermesHealthSensor(coordinator, entry),
            HermesApiLatencySensor(coordinator, entry),
            HermesLastSuccessfulConnectionSensor(coordinator, entry),
        )

    async def test_poll_interval_is_sixty_seconds_and_refresh_does_not_raise(self):
        from custom_components.hermes_conversation.coordinator import (
            HermesDiagnosticsCoordinator,
        )

        session = RaisingSession([asyncio.TimeoutError()])
        coordinator = HermesDiagnosticsCoordinator(
            FakeHass(), _entry(), _client(session)
        )
        await coordinator.async_refresh()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=60))
        self.assertFalse(coordinator.data.connected)
        self.assertTrue(coordinator.last_update_success)

    async def test_successful_then_offline_preserves_utc_timestamp(self):
        session = FakeSession(
            [
                _health_ok(),
                _models_ok(),
                _detailed_ok(),
                FakeResponse(status=500),
            ]
        )
        coordinator = await _refresh(FakeHass(), _entry(), _client(session))
        stamp = coordinator.data.last_successful_connection
        self.assertIsInstance(stamp, datetime)
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), timedelta(0))

        await coordinator.async_refresh()
        self.assertFalse(coordinator.data.connected)
        self.assertIsNone(coordinator.data.latency_ms)
        self.assertFalse(coordinator.data.health_available)
        self.assertEqual(coordinator.data.last_successful_connection, stamp)

    async def test_entities_expose_diagnostic_category_and_shared_device(self):
        from homeassistant.helpers.entity import EntityCategory

        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok(status="degraded")])
        entry = _entry()
        coordinator = await _refresh(FakeHass(), entry, _client(session))
        connectivity, health, latency, last_ok = self._entities(coordinator, entry)
        agent = HermesConversationAgent(FakeHass(), entry, _client(FakeSession([])), {})

        identifiers = {(DOMAIN, entry.entry_id)}
        self.assertEqual(agent.unique_id, entry.entry_id)
        self.assertEqual(agent.device_info["identifiers"], identifiers)
        for entity in (connectivity, health, latency, last_ok):
            self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)
            self.assertEqual(entity.device_info["identifiers"], identifiers)
            self.assertTrue(entity.has_entity_name)
            self.assertIsNotNone(entity.translation_key)

        self.assertEqual(connectivity.unique_id, f"{entry.entry_id}_api_connectivity")
        self.assertEqual(health.unique_id, f"{entry.entry_id}_health")
        self.assertEqual(latency.unique_id, f"{entry.entry_id}_api_latency")
        self.assertEqual(
            last_ok.unique_id, f"{entry.entry_id}_last_successful_connection"
        )
        self.assertTrue(connectivity.is_on)
        self.assertTrue(connectivity.available)
        self.assertEqual(health.native_value, "degraded")
        self.assertEqual(set(health.options), {"ok", "degraded"})
        self.assertTrue(health.available)
        self.assertEqual(latency.native_unit_of_measurement, "ms")
        self.assertTrue(latency.available)
        self.assertTrue(last_ok.available)

    async def test_offline_states_do_not_treat_unavailable_as_idle(self):
        session = FakeSession([FakeResponse(status=500, text_data=RAW_BODY)])
        entry = _entry()
        coordinator = await _refresh(FakeHass(), entry, _client(session))
        connectivity, health, latency, last_ok = self._entities(coordinator, entry)

        self.assertTrue(connectivity.available)
        self.assertFalse(connectivity.is_on)
        self.assertFalse(health.available)
        self.assertIsNone(health.native_value)
        self.assertFalse(latency.available)
        self.assertIsNone(latency.native_value)
        self.assertFalse(last_ok.available)
        self.assertIsNone(last_ok.native_value)

    async def test_optional_health_unavailable_while_basic_works(self):
        session = FakeSession(
            [_health_ok(), _models_ok(), FakeResponse(status=404)]
        )
        entry = _entry()
        coordinator = await _refresh(FakeHass(), entry, _client(session))
        connectivity, health, latency, last_ok = self._entities(coordinator, entry)
        self.assertTrue(connectivity.is_on)
        self.assertFalse(health.available)
        self.assertTrue(latency.available)
        self.assertTrue(last_ok.available)

    async def test_entity_attributes_and_logs_stay_sanitized(self):
        capture, loggers = _attach_log_capture()
        try:
            session = FakeSession(
                [_health_ok(), _models_ok(), FakeResponse(status=500, text_data=RAW_BODY)]
            )
            entry = _entry()
            coordinator = await _refresh(FakeHass(), entry, _client(session))
            entities = self._entities(coordinator, entry)
            blob = capture.combined()
            for entity in entities:
                blob += _serialized(entity.extra_state_attributes)
                blob += _serialized(entity.native_value if hasattr(entity, "native_value") else None)
                blob += str(entity.is_on if hasattr(entity, "is_on") else "")
            for fragment in FORBIDDEN_FRAGMENTS:
                self.assertNotIn(fragment, blob)
            self.assertNotIn("pid", blob)
            self.assertNotIn("readiness", blob)
            self.assertNotIn("active_agents", blob)
            if entities[0].extra_state_attributes:
                self.assertIn(
                    entities[0].extra_state_attributes.get("error_category"),
                    {None, "server", "unreachable"},
                )
        finally:
            _detach_log_capture(capture, loggers)

    async def test_multiple_entries_get_unique_devices(self):
        first = _entry(entry_id="one", title="Agent One")
        second = _entry(entry_id="two", title="Agent Two")
        session_one = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        session_two = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        coord_one = await _refresh(FakeHass(), first, _client(session_one))
        coord_two = await _refresh(FakeHass(), second, _client(session_two))
        entities_one = self._entities(coord_one, first)
        entities_two = self._entities(coord_two, second)
        self.assertEqual(entities_one[0].device_info["identifiers"], {(DOMAIN, "one")})
        self.assertEqual(entities_two[0].device_info["identifiers"], {(DOMAIN, "two")})
        self.assertNotEqual(entities_one[0].unique_id, entities_two[0].unique_id)
        agent_one = HermesConversationAgent(FakeHass(), first, _client(FakeSession([])), {})
        agent_two = HermesConversationAgent(FakeHass(), second, _client(FakeSession([])), {})
        self.assertEqual(agent_one.device_info["identifiers"], {(DOMAIN, "one")})
        self.assertEqual(agent_two.device_info["identifiers"], {(DOMAIN, "two")})

    async def test_coordinator_constructor_registers_core_unload_hook_once(self):
        from custom_components.hermes_conversation.coordinator import (
            HermesDiagnosticsCoordinator,
        )

        entry = _entry()
        coordinator = HermesDiagnosticsCoordinator(
            FakeHass(), entry, _client(FakeSession([]))
        )
        self.assertEqual(_shutdown_hook_count(entry, coordinator), 1)
        self.assertFalse(coordinator.shutdown)
        self.assertFalse(hasattr(coordinator, "_unloaded"))

    async def test_unloaded_coordinator_stops_polling_stale_client(self):
        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        hass = FakeHass(session=session)
        entry = _entry()
        import custom_components.hermes_conversation as integration

        await integration.async_setup_entry(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        calls_after_setup = len(session.calls)
        self.assertEqual(_shutdown_hook_count(entry, coordinator), 1)
        self.assertFalse(getattr(coordinator, "used_first_refresh", False))
        await integration.async_unload_entry(hass, entry)
        await entry.async_execute_unload()
        self.assertTrue(coordinator.shutdown)
        self.assertEqual(_shutdown_hook_count(entry, coordinator), 0)
        self.assertEqual(len(session.calls), calls_after_setup)
        await coordinator.async_refresh()
        self.assertEqual(len(session.calls), calls_after_setup)

    async def test_setup_forwards_conversation_when_api_is_down(self):
        session = RaisingSession([asyncio.TimeoutError()])
        hass = FakeHass(session=session)
        entry = _entry()
        import custom_components.hermes_conversation as integration

        result = await integration.async_setup_entry(hass, entry)
        self.assertTrue(result)
        forwarded = hass.config_entries.forwarded[0][1]
        self.assertIn("conversation", forwarded)
        self.assertIn("sensor", forwarded)
        self.assertIn("binary_sensor", forwarded)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        self.assertFalse(coordinator.data.connected)
        self.assertFalse(getattr(coordinator, "used_first_refresh", False))
        self.assertTrue(coordinator.last_update_success)

    async def test_sensor_and_binary_sensor_platforms_register_four_entities(self):
        session = FakeSession([_health_ok(), _models_ok(), _detailed_ok()])
        hass = FakeHass(session=session)
        entry = _entry()
        import custom_components.hermes_conversation as integration
        from custom_components.hermes_conversation import binary_sensor as binary_module
        from custom_components.hermes_conversation import sensor as sensor_module

        await integration.async_setup_entry(hass, entry)
        added: list[Any] = []
        await sensor_module.async_setup_entry(
            hass, entry, lambda entities: added.extend(entities)
        )
        await binary_module.async_setup_entry(
            hass, entry, lambda entities: added.extend(entities)
        )
        self.assertEqual(len(added), 4)
        keys = {entity.translation_key for entity in added}
        self.assertEqual(
            keys,
            {
                "api_connectivity",
                "health",
                "api_latency",
                "last_successful_connection",
            },
        )


class DiagnosticsContractTests(unittest.TestCase):
    def test_translation_keys_are_synchronized(self):
        strings = json.loads(
            (REPO_ROOT / "custom_components/hermes_conversation/strings.json").read_text()
        )
        english = json.loads(
            (
                REPO_ROOT
                / "custom_components/hermes_conversation/translations/en.json"
            ).read_text()
        )
        expected = {
            "entity": {
                "binary_sensor": {"api_connectivity": {"name": "API connectivity"}},
                "sensor": {
                    "health": {
                        "name": "Health",
                        "state": {"ok": "OK", "degraded": "Degraded"},
                    },
                    "api_latency": {"name": "API latency"},
                    "last_successful_connection": {
                        "name": "Last successful connection"
                    },
                },
            }
        }
        self.assertEqual(strings["entity"], expected["entity"])
        self.assertEqual(english["entity"], expected["entity"])

    def test_manifest_uses_polling_iot_class_and_next_feature_version(self):
        manifest = json.loads(
            (REPO_ROOT / "custom_components/hermes_conversation/manifest.json").read_text()
        )
        self.assertEqual(manifest["iot_class"], "local_polling")
        self.assertEqual(manifest["version"], "1.3.0")

    def test_hacs_metadata_matches_diagnostic_platforms(self):
        hacs = json.loads((REPO_ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["iot_class"], "local_polling")
        self.assertEqual(set(hacs["domains"]), {"conversation", "sensor", "binary_sensor"})

    def test_readme_documents_sensors_interval_legacy_and_alert_example(self):
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertIn("60", readme)
        self.assertIn("binary_sensor.hermes_agent_api_connectivity", readme)
        self.assertIn("sensor.hermes_agent_health", readme)
        self.assertIn("sensor.hermes_agent_api_latency", readme)
        self.assertIn("sensor.hermes_agent_last_successful_connection", readme)
        self.assertIn("persistent_notification", readme)
        self.assertIn("00:02:00", readme)
        self.assertIn("degraded", readme.lower())
        self.assertIn("legacy", readme.lower())
        self.assertIsNone(re.search(r"unavailable.{0,40}idle", readme, re.I))


if __name__ == "__main__":
    unittest.main()
