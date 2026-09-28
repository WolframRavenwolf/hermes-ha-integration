"""Real HA fixtures for diagnostics runtime tests.

Do not import tests.test_support. Network is faked only by the 127.0.0.1
aiohttp loopback in loopback.py — not by Home Assistant aiohttp mocks.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_socket

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from runtime_tests.loopback import HermesLoopbackApi

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# pytest-homeassistant-custom-component registers via the pytest11 entry point.


@pytest.fixture(autouse=True)
def _allow_loopback_sockets() -> None:
    """Undo HA's disable_socket so the loopback TCP server can bind/connect."""
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


@pytest.fixture(autouse=True)
def _no_stub_leak() -> Generator[None]:
    leaked = [
        name
        for name in sys.modules
        if name == "tests.test_support" or name.startswith("tests.test_support.")
    ]
    assert not leaked, f"stub suite leaked into runtime tests: {leaked}"
    yield
    leaked = [
        name
        for name in sys.modules
        if name == "tests.test_support" or name.startswith("tests.test_support.")
    ]
    assert not leaked, f"stub suite leaked into runtime tests: {leaked}"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/hermes_conversation from this worktree."""


@pytest.fixture
async def hass_ready(hass: HomeAssistant) -> HomeAssistant:
    # conversation.default_agent calls async_should_expose at started,
    # which needs homeassistant.exposed_entities (and its persistent_notification import).
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    await hass.async_block_till_done()
    return hass


@pytest.fixture
async def hermes_api() -> AsyncGenerator[HermesLoopbackApi]:
    api = HermesLoopbackApi()
    await api.start()
    try:
        yield api
    finally:
        await api.stop()
