from __future__ import annotations

import unittest

from tests.test_support import FakeConfigEntry
from custom_components.hermes_conversation.compat import (
    entry_value,
    parse_api_base_url,
    resolve_connection_config,
)
from custom_components.hermes_conversation.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_USE_SSL,
    LEGACY_CONF_API_BASE_URL,
)


class CompatTests(unittest.TestCase):
    def test_parse_api_base_url_with_scheme_and_port(self):
        parsed = parse_api_base_url("https://agent01.local:8443")
        self.assertEqual(parsed.host, "agent01.local")
        self.assertEqual(parsed.port, 8443)
        self.assertTrue(parsed.use_ssl)

    def test_parse_api_base_url_without_scheme_defaults_to_https(self):
        parsed = parse_api_base_url("agent01.local:8123")
        self.assertEqual(parsed.host, "agent01.local")
        self.assertEqual(parsed.port, 8123)
        self.assertTrue(parsed.use_ssl)

    def test_entry_value_prefers_options_then_data_then_legacy(self):
        entry = FakeConfigEntry(
            data={"prompt": "data prompt", "instructions": "legacy prompt"},
            options={"prompt": "options prompt"},
        )
        self.assertEqual(entry_value(entry, "prompt", legacy_keys=("instructions",)), "options prompt")

        entry = FakeConfigEntry(data={"instructions": "legacy prompt"}, options={})
        self.assertEqual(entry_value(entry, "prompt", legacy_keys=("instructions",)), "legacy prompt")

    def test_resolve_connection_config_from_legacy_api_base_url(self):
        entry = FakeConfigEntry(
            data={LEGACY_CONF_API_BASE_URL: "http://ha-box.local:8080"},
            options={},
        )
        connection = resolve_connection_config(entry)
        self.assertEqual(connection.host, "ha-box.local")
        self.assertEqual(connection.port, 8080)
        self.assertFalse(connection.use_ssl)
        self.assertFalse(connection.verify_ssl)

    def test_resolve_connection_config_prefers_explicit_host_port(self):
        entry = FakeConfigEntry(
            data={LEGACY_CONF_API_BASE_URL: "http://old-host.local:8080", CONF_HOST: "new-host.local", CONF_PORT: 9443, CONF_USE_SSL: True},
            options={},
        )
        connection = resolve_connection_config(entry)
        self.assertEqual(connection.host, "new-host.local")
        self.assertEqual(connection.port, 9443)
        self.assertTrue(connection.use_ssl)


if __name__ == "__main__":
    unittest.main()
