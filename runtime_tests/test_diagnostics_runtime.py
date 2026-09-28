"""Black-box diagnostics contract against a real Home Assistant runtime.

Asserts entity registry, device registry, and the state machine — not module
imports. Fakes only the Hermes HTTP boundary via runtime_tests.loopback.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.components import conversation
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_PROFILE,
    CONF_PROFILE_ROUTE,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from runtime_tests.loopback import (
    API_KEY,
    DETAILED_404,
    DETAILED_DEGRADED,
    DETAILED_MALFORMED,
    DETAILED_UNSUPPORTED,
    HEALTH_LEGACY_404,
    HermesLoopbackApi,
    RouteBundle,
)

PLATFORM = DOMAIN
DIAGNOSTIC_KEYS = (
    ("binary_sensor", "api_connectivity"),
    ("sensor", "health"),
    ("sensor", "api_latency"),
    ("sensor", "last_successful_connection"),
)


def _entry(
    api: HermesLoopbackApi,
    *,
    entry_id: str,
    title: str = "Hermes Agent",
    profile: str = "",
    profile_route: str = "addon",
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        entry_id=entry_id,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: api.port,
            CONF_API_KEY: API_KEY,
            CONF_PROFILE: profile,
            CONF_PROFILE_ROUTE: profile_route,
            CONF_USE_SSL: False,
            CONF_VERIFY_SSL: False,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> ConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    live = hass.config_entries.async_get_entry(entry.entry_id)
    assert live is not None
    assert live.state is ConfigEntryState.LOADED
    return live


def _eid(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, PLATFORM, unique_id)
    assert entity_id is not None, f"missing {domain}/{PLATFORM}/{unique_id}"
    return entity_id


def _ids(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    ids = {
        "conversation": _eid(hass, "conversation", entry_id),
        "api_connectivity": _eid(hass, "binary_sensor", f"{entry_id}_api_connectivity"),
        "health": _eid(hass, "sensor", f"{entry_id}_health"),
        "api_latency": _eid(hass, "sensor", f"{entry_id}_api_latency"),
        "last_successful_connection": _eid(
            hass, "sensor", f"{entry_id}_last_successful_connection"
        ),
    }
    return ids


def _state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, f"no state for {entity_id}"
    return state


async def _refresh_poll(hass: HomeAssistant, entity_id: str) -> None:
    """Force a coordinator refresh via the public update_entity service."""
    # DataUpdateCoordinator's request-refresh Debouncer defaults to 10s cooldown.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=11), fire_all=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def _advance_interval(hass: HomeAssistant) -> None:
    """Let scheduled coordinator timers run (background tasks included)."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=70), fire_all=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _assert_one_device(hass: HomeAssistant, entry_id: str, ids: dict[str, str]) -> str:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert ("hermes_conversation", entry_id) in device.identifiers
    registry = er.async_get(hass)
    for entity_id in ids.values():
        item = registry.async_get(entity_id)
        assert item is not None
        assert item.device_id == device.id
        assert item.config_entry_id == entry_id
    return device.id


async def test_setup_registers_conversation_and_diagnostics_on_one_device(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    entry = await _setup(hass, _entry(hermes_api, entry_id="root-entry"))
    ids = _ids(hass, entry.entry_id)
    _assert_one_device(hass, entry.entry_id, ids)

    registry = er.async_get(hass)
    for _domain, key in DIAGNOSTIC_KEYS:
        item = registry.async_get(ids[key])
        assert item is not None
        assert item.entity_category is EntityCategory.DIAGNOSTIC

    connectivity_item = registry.async_get(ids["api_connectivity"])
    assert connectivity_item is not None
    assert connectivity_item.original_device_class == "connectivity"

    health_item = registry.async_get(ids["health"])
    assert health_item is not None
    assert health_item.original_device_class == "enum"
    assert (health_item.capabilities or {}).get("options") == ["ok", "degraded"]

    latency_item = registry.async_get(ids["api_latency"])
    assert latency_item is not None
    assert latency_item.unit_of_measurement == "ms"

    last_item = registry.async_get(ids["last_successful_connection"])
    assert last_item is not None
    assert last_item.original_device_class == "timestamp"

    conversation_item = registry.async_get(ids["conversation"])
    assert conversation_item is not None
    assert conversation_item.entity_category is None

    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == "ok"
    latency = _state(hass, ids["api_latency"])
    assert latency.state not in {STATE_UNAVAILABLE, STATE_OFF}
    int(latency.state)
    last = _state(hass, ids["last_successful_connection"])
    stamp = dt_util.parse_datetime(last.state)
    assert stamp is not None
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == timedelta(0)


async def test_legacy_health_404_keeps_basics_and_hides_health(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    hermes_api.root = RouteBundle(health=HEALTH_LEGACY_404, detailed=DETAILED_404)
    entry = await _setup(hass, _entry(hermes_api, entry_id="legacy-entry"))
    ids = _ids(hass, entry.entry_id)
    assert hass.states.get(ids["conversation"]) is not None
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["api_latency"]).state != STATE_UNAVAILABLE
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE
    last = _state(hass, ids["last_successful_connection"])
    assert dt_util.parse_datetime(last.state) is not None


async def test_offline_setup_does_not_block_conversation(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    await hermes_api.go_offline()
    entry = await _setup(hass, _entry(hermes_api, entry_id="offline-entry"))
    ids = _ids(hass, entry.entry_id)
    assert hass.states.get(ids["conversation"]) is not None
    assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE
    assert _state(hass, ids["api_latency"]).state == STATE_UNAVAILABLE
    assert _state(hass, ids["last_successful_connection"]).state == STATE_UNAVAILABLE


async def test_blocked_diagnostics_request_does_not_delay_conversation_setup(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    release = asyncio.Event()
    hermes_api.block_requests = release
    entry = _entry(hermes_api, entry_id="blocked-entry")
    entry.add_to_hass(hass)
    try:
        # Wait for the real TCP request to reach the server and remain unanswered.
        setup = hass.async_create_task(
            hass.config_entries.async_setup(entry.entry_id), "setup blocked Hermes entry"
        )
        try:
            await asyncio.wait_for(hermes_api.request_started.wait(), timeout=5)
            assert not release.is_set()
            assert await asyncio.wait_for(setup, timeout=2)
            live = hass.config_entries.async_get_entry(entry.entry_id)
            assert live is not None
            assert live.state is ConfigEntryState.LOADED
            ids = _ids(hass, entry.entry_id)
            assert conversation.async_get_agent(hass, ids["conversation"]) is not None
            assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
            assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE
        finally:
            release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert _state(hass, ids["api_connectivity"]).state == STATE_ON
        assert _state(hass, ids["health"]).state == "ok"
    finally:
        release.set()


async def test_unload_cancels_blocked_initial_refresh(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    release = asyncio.Event()
    hermes_api.block_requests = release
    entry = _entry(hermes_api, entry_id="unload-blocked-entry")
    entry.add_to_hass(hass)
    try:
        assert await asyncio.wait_for(hass.config_entries.async_setup(entry.entry_id), 2)
        await asyncio.wait_for(hermes_api.request_started.wait(), 5)
        assert not release.is_set()
        # The request was already recorded by the server; verify the actual
        # background task is cancelled, not just that no new requests appear.
        refresh_tasks = [
            task
            for task in entry._background_tasks
            if task.get_name().startswith("Hermes diagnostics initial refresh")
        ]
        assert len(refresh_tasks) == 1
        refresh_task = refresh_tasks[0]
        assert not refresh_task.done()
        assert await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 2)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert refresh_task.cancelled()
        before = len(hermes_api.requests)
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(hermes_api.requests) == before
    finally:
        release.set()


async def test_loss_recovery_retains_utc_timestamp(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    entry = await _setup(hass, _entry(hermes_api, entry_id="retain-entry"))
    ids = _ids(hass, entry.entry_id)
    first = dt_util.parse_datetime(_state(hass, ids["last_successful_connection"]).state)
    assert first is not None
    assert first.utcoffset() == timedelta(0)

    await hermes_api.go_offline()
    await _refresh_poll(hass, ids["api_connectivity"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE
    assert _state(hass, ids["api_latency"]).state == STATE_UNAVAILABLE
    retained = dt_util.parse_datetime(
        _state(hass, ids["last_successful_connection"]).state
    )
    assert retained == first

    await hermes_api.go_online()
    await _refresh_poll(hass, ids["api_connectivity"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == "ok"
    recovered = dt_util.parse_datetime(
        _state(hass, ids["last_successful_connection"]).state
    )
    assert recovered is not None
    assert recovered >= first


async def test_degraded_and_malformed_and_unsupported_detailed(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    entry = await _setup(hass, _entry(hermes_api, entry_id="payload-entry"))
    ids = _ids(hass, entry.entry_id)
    assert _state(hass, ids["health"]).state == "ok"

    hermes_api.root.detailed = DETAILED_DEGRADED
    await _refresh_poll(hass, ids["health"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == "degraded"

    hermes_api.root.detailed = DETAILED_MALFORMED
    await _refresh_poll(hass, ids["health"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE
    assert _state(hass, ids["api_latency"]).state != STATE_UNAVAILABLE

    hermes_api.root.detailed = DETAILED_UNSUPPORTED
    await _refresh_poll(hass, ids["health"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE

    hermes_api.root.detailed = DETAILED_404
    await _refresh_poll(hass, ids["health"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE


async def test_two_entries_are_distinct_devices(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    hermes_api.addon["worker"] = RouteBundle()
    first = await _setup(hass, _entry(hermes_api, entry_id="device-a", title="Hermes A"))
    second = await _setup(
        hass,
        _entry(
            hermes_api,
            entry_id="device-b",
            title="Hermes B",
            profile="worker",
            profile_route="addon",
        ),
    )
    ids_a = _ids(hass, first.entry_id)
    ids_b = _ids(hass, second.entry_id)
    device_a = _assert_one_device(hass, first.entry_id, ids_a)
    device_b = _assert_one_device(hass, second.entry_id, ids_b)
    assert device_a != device_b
    assert set(ids_a.values()).isdisjoint(ids_b.values())
    assert hermes_api.count_paths("/profile/worker/") >= 1
    assert hermes_api.count_paths("/v1/health") >= 1


async def test_native_profile_uses_canary_and_registers(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    hermes_api.native["worker-bot"] = RouteBundle()
    hermes_api.native_canary_status = 404
    entry = await _setup(
        hass,
        _entry(
            hermes_api,
            entry_id="native-entry",
            title="Hermes Agent (worker-bot)",
            profile="worker-bot",
            profile_route="native",
        ),
    )
    ids = _ids(hass, entry.entry_id)
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    assert _state(hass, ids["health"]).state == "ok"
    assert "/p/hermes/v1/health" in hermes_api.request_paths()
    assert "/p/worker-bot/v1/health" in hermes_api.request_paths()
    assert "/p/worker-bot/v1/models" in hermes_api.request_paths()
    assert "/p/worker-bot/health/detailed" in hermes_api.request_paths()


async def test_automatic_poll_is_shared_and_observes_outage(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    entry = await _setup(hass, _entry(hermes_api, entry_id="scheduled-entry"))
    ids = _ids(hass, entry.entry_id)
    before = len(hermes_api.requests)
    await _advance_interval(hass)
    assert hermes_api.request_paths()[before:] == [
        "/v1/health", "/v1/models", "/health/detailed"
    ]
    await hermes_api.go_offline()
    await _advance_interval(hass)
    assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
    assert _state(hass, ids["health"]).state == STATE_UNAVAILABLE


async def test_rejected_credentials_and_profile_guard_are_fail_closed(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    hermes_api.native["worker"] = RouteBundle(api_key="different-test-key")
    entry = await _setup(
        hass,
        _entry(hermes_api, entry_id="auth-entry", profile="worker", profile_route="native"),
    )
    ids = _ids(hass, entry.entry_id)
    assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
    assert all("detailed" not in request.path for request in hermes_api.requests)
    for request in hermes_api.requests:
        if request.path.endswith("/v1/health"):
            assert request.authorization is None
    hermes_api.native["worker"].api_key = API_KEY
    await _refresh_poll(hass, ids["api_connectivity"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_ON
    before = len(hermes_api.requests)
    hermes_api.native_canary_status = 200
    await _refresh_poll(hass, ids["api_connectivity"])
    assert _state(hass, ids["api_connectivity"]).state == STATE_OFF
    # The time jump can fire a scheduled poll as well as update_entity.
    blocked = hermes_api.requests[before:]
    assert blocked
    assert all(request.path == "/p/hermes/v1/health" for request in blocked)
    assert all(request.authorization is None for request in blocked)


async def test_unload_and_reload_stop_stale_polling(
    hass_ready: HomeAssistant, hermes_api: HermesLoopbackApi
) -> None:
    hass = hass_ready
    entry = await _setup(hass, _entry(hermes_api, entry_id="poll-entry"))
    ids = _ids(hass, entry.entry_id)
    await _refresh_poll(hass, ids["api_connectivity"])
    after_poll = hermes_api.count_paths("/v1/models", "/health/detailed")
    assert after_poll >= 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    live = hass.config_entries.async_get_entry(entry.entry_id)
    assert live is not None
    assert live.state is ConfigEntryState.NOT_LOADED
    unloaded_count = hermes_api.count_paths("/v1/models", "/health/detailed")
    await _advance_interval(hass)
    await _advance_interval(hass)
    assert hermes_api.count_paths("/v1/models", "/health/detailed") == unloaded_count
    connectivity = hass.states.get(ids["api_connectivity"])
    assert connectivity is None or connectivity.state == STATE_UNAVAILABLE

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    reloaded = _ids(hass, entry.entry_id)
    assert _state(hass, reloaded["api_connectivity"]).state == STATE_ON
    before = hermes_api.count_paths("/v1/models", "/health/detailed")
    await _refresh_poll(hass, reloaded["api_connectivity"])
    assert hermes_api.count_paths("/v1/models", "/health/detailed") > before
