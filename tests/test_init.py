from __future__ import annotations

import unittest

from tests.test_support import FakeConfigEntry, FakeHass
import custom_components.hermes_conversation as integration
from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_PROFILE,
    CONF_USE_SSL,
    DOMAIN,
)


class InitTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_forwards_conversation_platform(self):
        hass = FakeHass()
        entry = FakeConfigEntry(
            data={
                CONF_HOST: "agent.local",
                CONF_PORT: 8443,
                CONF_API_KEY: "secret",
                CONF_USE_SSL: True,
            },
            options={},
        )

        result = await integration.async_setup_entry(hass, entry)

        self.assertTrue(result)
        self.assertIn(entry.entry_id, hass.data[DOMAIN])
        self.assertIn("client", hass.data[DOMAIN][entry.entry_id])
        self.assertIn("sessions", hass.data[DOMAIN][entry.entry_id])
        self.assertEqual(
            hass.config_entries.forwarded,
            [(entry.entry_id, ("conversation",))],
        )

    async def test_setup_passes_profile_to_api_client(self):
        hass = FakeHass()
        entry = FakeConfigEntry(
            data={
                CONF_HOST: "agent.local",
                CONF_PORT: 8443,
                CONF_API_KEY: "secret",
                CONF_USE_SSL: True,
                CONF_PROFILE: "assistant",
            },
            options={},
        )

        await integration.async_setup_entry(hass, entry)

        client = hass.data[DOMAIN][entry.entry_id]["client"]
        self.assertEqual(
            client.base_url,
            "https://agent.local:8443/profile/assistant",
        )

    async def test_setup_legacy_entry_uses_root_api(self):
        hass = FakeHass()
        entry = FakeConfigEntry(
            data={CONF_HOST: "agent.local", CONF_PORT: 8443},
            options={},
        )

        await integration.async_setup_entry(hass, entry)

        client = hass.data[DOMAIN][entry.entry_id]["client"]
        self.assertEqual(client.base_url, "https://agent.local:8443")

    async def test_update_listener_reloads_entry(self):
        hass = FakeHass()
        entry = FakeConfigEntry(entry_id="profile-entry")

        await integration._async_update_listener(hass, entry)

        self.assertEqual(hass.config_entries.reloaded, ["profile-entry"])

    async def test_setup_recreates_session_state_after_reload(self):
        hass = FakeHass()
        entry = FakeConfigEntry(
            data={CONF_HOST: "agent.local", CONF_PORT: 8443},
            options={},
        )
        await integration.async_setup_entry(hass, entry)
        old_sessions = hass.data[DOMAIN][entry.entry_id]["sessions"]
        old_sessions["voice"] = {"session_id": "old"}

        await integration.async_setup_entry(hass, entry)

        new_sessions = hass.data[DOMAIN][entry.entry_id]["sessions"]
        self.assertIsNot(new_sessions, old_sessions)
        self.assertEqual(new_sessions, {})

    async def test_unload_unloads_conversation_platform(self):
        hass = FakeHass()
        entry = FakeConfigEntry()
        hass.data[DOMAIN] = {entry.entry_id: {"client": object(), "sessions": {}}}

        result = await integration.async_unload_entry(hass, entry)

        self.assertTrue(result)
        self.assertNotIn(DOMAIN, hass.data)
        self.assertEqual(
            hass.config_entries.unloaded,
            [(entry.entry_id, ("conversation",))],
        )


if __name__ == "__main__":
    unittest.main()
