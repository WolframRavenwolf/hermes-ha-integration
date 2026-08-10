from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest import mock

from tests.test_api import FakeResponse, FakeSession
from tests.test_support import FakeConfigEntry, FakeHass
from homeassistant.data_entry_flow import AbortFlow

import custom_components.hermes_conversation as integration
from custom_components.hermes_conversation.config_flow import (
    HermesConversationConfigFlow,
    HermesConversationOptionsFlow,
)
from custom_components.hermes_conversation.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_PROFILE,
    CONF_PROMPT,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    LEGACY_CONF_API_BASE_URL,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = REPO_ROOT / "custom_components" / "hermes_conversation"


def connection_input(**overrides):
    values = {
        CONF_HOST: "agent.local",
        CONF_PORT: 8443,
        CONF_PROFILE: "",
        CONF_API_KEY: "secret",
        CONF_USE_SSL: True,
        CONF_VERIFY_SSL: False,
    }
    values.update(overrides)
    return values


def successful_probe_responses():
    return [
        FakeResponse(
            json_data={
                "status": "ok",
                "platform": "hermes-agent",
                "version": "test",
            }
        ),
        FakeResponse(json_data={"data": [{"id": "hermes-agent"}]}),
    ]


class NoRequestSession:
    def get(self, *args, **kwargs):
        raise AssertionError("invalid input must be rejected before probing the API")


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, session, entries=()):
        flow = HermesConversationConfigFlow()
        flow.hass = FakeHass(session=session)
        flow._current_entries = list(entries)
        return flow

    async def test_user_step_normalizes_profile_and_sets_profile_title(self):
        session = FakeSession(successful_probe_responses())
        flow = self.make_flow(session)

        result = await flow.async_step_user(
            connection_input(**{CONF_PROFILE: " assistant_2 "})
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Hermes Agent (assistant_2)")
        self.assertEqual(result["data"][CONF_PROFILE], "assistant_2")
        self.assertEqual(
            session.calls[0]["url"],
            "https://agent.local:8443/profile/assistant_2/v1/health",
        )

    async def test_user_step_blank_profile_keeps_primary_title(self):
        flow = self.make_flow(FakeSession(successful_probe_responses()))

        result = await flow.async_step_user(connection_input())

        self.assertEqual(result["title"], "Hermes Agent")
        self.assertEqual(result["data"][CONF_PROFILE], "")

    async def test_user_step_rejects_blank_host_before_connection_probe(self):
        flow = self.make_flow(NoRequestSession())

        result = await flow.async_step_user(
            connection_input(**{CONF_HOST: "   "})
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {CONF_HOST: "invalid_host"})

    async def test_user_step_rejects_non_host_input_before_connection_probe(self):
        for host in (
            "agent.local/path",
            "user@agent.local",
            "agent.local?query=1",
            "agent.local#fragment",
        ):
            with self.subTest(host=host):
                flow = self.make_flow(NoRequestSession())
                result = await flow.async_step_user(
                    connection_input(**{CONF_HOST: host})
                )
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {CONF_HOST: "invalid_host"})

    async def test_public_health_then_models_unauthorized_maps_to_invalid_auth(self):
        session = FakeSession(
            [
                successful_probe_responses()[0],
                FakeResponse(status=401),
            ]
        )
        flow = self.make_flow(session)

        result = await flow.async_step_user(connection_input())

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertEqual(
            [call["url"] for call in session.calls],
            [
                "https://agent.local:8443/v1/health",
                "https://agent.local:8443/v1/models",
            ],
        )

    async def test_user_step_rejects_unsafe_profile_before_connection_probe(self):
        invalid_profiles = (
            "worker/other",
            ".",
            "..",
            "worker name",
            "worker_",
            "worker__two",
            "%2fworker",
            "worker?query=1",
            "worker#fragment",
            "worker-name",
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                flow = self.make_flow(NoRequestSession())

                result = await flow.async_step_user(
                    connection_input(**{CONF_PROFILE: profile})
                )

                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {CONF_PROFILE: "invalid_profile"})

    async def test_exact_duplicate_is_rejected_ignoring_api_key_and_verify_ssl(self):
        existing = FakeConfigEntry(
            data=connection_input(
                **{
                    CONF_HOST: " AGENT.LOCAL. ",
                    CONF_PROFILE: " worker ",
                    CONF_API_KEY: "old-secret",
                    CONF_VERIFY_SSL: True,
                }
            )
        )
        flow = self.make_flow(
            FakeSession(successful_probe_responses()),
            [existing],
        )

        with self.assertRaises(AbortFlow) as context:
            await flow.async_step_user(
                connection_input(
                    **{
                        CONF_HOST: "agent.local",
                        CONF_PROFILE: "worker",
                        CONF_API_KEY: "new-secret",
                        CONF_VERIFY_SSL: False,
                    }
                )
            )

        self.assertEqual(context.exception.reason, "already_configured")

    async def test_same_endpoint_with_different_profile_is_allowed(self):
        existing = FakeConfigEntry(
            data=connection_input(**{CONF_PROFILE: "worker"})
        )
        flow = self.make_flow(
            FakeSession(successful_probe_responses()),
            [existing],
        )

        result = await flow.async_step_user(
            connection_input(**{CONF_PROFILE: "assistant"})
        )

        self.assertEqual(result["type"], "create_entry")

    async def test_same_host_port_and_profile_with_different_transport_is_allowed(self):
        existing = FakeConfigEntry(data=connection_input(**{CONF_USE_SSL: True}))
        flow = self.make_flow(
            FakeSession(successful_probe_responses()),
            [existing],
        )

        result = await flow.async_step_user(
            connection_input(**{CONF_USE_SSL: False})
        )

        self.assertEqual(result["type"], "create_entry")


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, entry, session=None, hass=None):
        flow = HermesConversationOptionsFlow()
        flow.hass = hass or FakeHass(session=session)
        if not any(
            current.entry_id == entry.entry_id
            for current in flow.hass.config_entries.entries
        ):
            flow.hass.config_entries.entries.append(entry)
        flow.config_entry = entry
        return flow

    async def test_legacy_entry_form_defaults_profile_to_blank(self):
        entry = FakeConfigEntry(
            data={LEGACY_CONF_API_BASE_URL: "https://agent.local:8443"},
            options={},
        )
        flow = self.make_flow(entry)

        result = await flow.async_step_init()

        self.assertEqual(result["type"], "form")
        self.assertIn(CONF_PROFILE, result["data_schema"])

    async def test_options_update_profile_preserves_existing_data_and_updates_title(self):
        entry = FakeConfigEntry(
            data={
                CONF_HOST: "agent.local",
                CONF_PORT: 8443,
                CONF_API_KEY: "secret",
                CONF_USE_SSL: True,
                CONF_VERIFY_SSL: False,
                "preserve_me": "yes",
            },
            options={CONF_PROMPT: "old prompt", "preserve_option": "yes"},
        )
        flow = self.make_flow(
            entry,
            session=FakeSession(successful_probe_responses()),
        )

        result = await flow.async_step_init(
            {CONF_PROFILE: " assistant ", CONF_PROMPT: "new prompt"}
        )

        self.assertEqual(entry.data["preserve_me"], "yes")
        self.assertEqual(entry.data[CONF_PROFILE], "assistant")
        self.assertEqual(entry.title, "Hermes Agent (assistant)")
        expected_options = {
            CONF_PROMPT: "new prompt",
            "preserve_option": "yes",
        }
        self.assertIsNone(result["data"])
        self.assertEqual(entry.options, expected_options)
        self.assertEqual(len(flow.hass.config_entries.updated), 1)
        self.assertEqual(flow.hass.config_entries.updated[0][3], expected_options)

    async def test_combined_options_update_triggers_exactly_one_reload(self):
        entry = FakeConfigEntry(
            entry_id="profile-entry",
            data=connection_input(),
            options={CONF_PROMPT: "old prompt"},
        )
        hass = FakeHass(session=FakeSession(successful_probe_responses()))
        hass.config_entries.entries = [entry]
        await integration.async_setup_entry(hass, entry)
        flow = self.make_flow(entry, hass=hass)

        result = await flow.async_step_init(
            {CONF_PROFILE: "worker", CONF_PROMPT: "new prompt"}
        )
        self.assertIsNone(result["data"])
        await hass.config_entries.async_process_pending_updates()
        self.assertEqual(len(hass.config_entries.updated), 1)
        self.assertEqual(hass.config_entries.reloaded, [entry.entry_id])

    async def test_delayed_options_manager_cannot_reapply_stale_options(self):
        entry = FakeConfigEntry(
            entry_id="profile-entry",
            data=connection_input(),
            options={CONF_PROMPT: "original"},
        )
        hass = FakeHass()
        first_flow = self.make_flow(entry, hass=hass)
        first_result = await first_flow.async_step_init({CONF_PROMPT: "first"})

        second_flow = self.make_flow(entry, hass=hass)
        second_result = await second_flow.async_step_init({CONF_PROMPT: "second"})

        self.assertIsNone(first_result["data"])
        self.assertIsNone(second_result["data"])
        self.assertEqual(entry.options[CONF_PROMPT], "second")
        self.assertEqual(len(hass.config_entries.updated), 2)

        # Home Assistant skips its options write when flow-result data is None.
        if first_result["data"] is not None:
            hass.config_entries.async_update_entry(
                entry,
                options=first_result["data"],
            )
        self.assertEqual(entry.options[CONF_PROMPT], "second")
        self.assertEqual(len(hass.config_entries.updated), 2)

    async def test_options_preserves_custom_entry_title_when_profile_changes(self):
        entry = FakeConfigEntry(
            title="Kitchen Agent",
            data=connection_input(**{CONF_PROFILE: "worker"}),
            options={},
        )
        flow = self.make_flow(
            entry,
            session=FakeSession(successful_probe_responses()),
        )

        result = await flow.async_step_init({CONF_PROFILE: "assistant"})

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(entry.data[CONF_PROFILE], "assistant")
        self.assertEqual(entry.title, "Kitchen Agent")

    async def test_options_reject_invalid_profile_without_updating_entry(self):
        entry = FakeConfigEntry(
            data={CONF_HOST: "agent.local", CONF_PORT: 8443},
            options={},
        )
        flow = self.make_flow(entry)

        result = await flow.async_step_init({CONF_PROFILE: "../worker"})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {CONF_PROFILE: "invalid_profile"})
        self.assertNotIn(CONF_PROFILE, entry.data)

    async def test_options_rejects_blank_host_before_fallback_or_mutation(self):
        entry = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "assistant"}),
            options={CONF_PROMPT: "old prompt"},
        )
        flow = self.make_flow(entry, session=NoRequestSession())

        result = await flow.async_step_init(
            {
                CONF_HOST: "   ",
                CONF_PROFILE: "worker",
                CONF_PROMPT: "new prompt",
            }
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {CONF_HOST: "invalid_host"})
        self.assertEqual(flow.hass.config_entries.updated, [])
        self.assertEqual(entry.data[CONF_HOST], "agent.local")
        self.assertEqual(entry.data[CONF_PROFILE], "assistant")
        self.assertEqual(entry.options, {CONF_PROMPT: "old prompt"})

    async def test_options_rejects_invalid_key_before_any_mutation(self):
        entry = FakeConfigEntry(
            entry_id="current",
            title="Kitchen Agent",
            data=connection_input(**{CONF_PROFILE: "worker"}),
            options={CONF_PROMPT: "keep me"},
        )
        original_data = dict(entry.data)
        session = FakeSession(
            [successful_probe_responses()[0], FakeResponse(status=401)]
        )
        flow = self.make_flow(entry, session=session)

        result = await flow.async_step_init({CONF_API_KEY: "wrong-key"})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertEqual(entry.data, original_data)
        self.assertEqual(entry.options, {CONF_PROMPT: "keep me"})
        self.assertEqual(entry.title, "Kitchen Agent")

    async def test_options_rejects_unreachable_profile_before_any_mutation(self):
        entry = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "worker"}),
            options={CONF_PROMPT: "keep me"},
        )
        original_data = dict(entry.data)
        session = FakeSession(
            [FakeResponse(status=404), FakeResponse(status=404)]
        )
        flow = self.make_flow(entry, session=session)

        result = await flow.async_step_init({CONF_PROFILE: "missing"})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertEqual(entry.data, original_data)
        self.assertEqual(entry.options, {CONF_PROMPT: "keep me"})
        self.assertEqual(entry.title, "Hermes Agent")

    async def test_options_allows_current_entry_identity_when_self_is_enumerated(self):
        entry = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "worker"}),
            options={CONF_PROMPT: "old prompt"},
        )
        flow = self.make_flow(
            entry,
            session=FakeSession(successful_probe_responses()),
        )
        flow.hass.config_entries.entries = [entry]

        result = await flow.async_step_init(
            {CONF_API_KEY: "rotated-secret", CONF_PROMPT: "new prompt"}
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(entry.data[CONF_API_KEY], "rotated-secret")
        self.assertIsNone(result["data"])
        self.assertEqual(entry.options[CONF_PROMPT], "new prompt")

    async def test_options_reject_duplicate_connection_without_mutating_entry(self):
        entry = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "assistant"}),
            options={CONF_PROMPT: "keep me"},
        )
        duplicate = FakeConfigEntry(
            entry_id="other",
            data=connection_input(
                **{
                    CONF_HOST: " AGENT.LOCAL ",
                    CONF_PROFILE: " worker ",
                    CONF_API_KEY: "different-secret",
                    CONF_VERIFY_SSL: True,
                }
            ),
        )
        original_data = dict(entry.data)
        original_title = entry.title
        flow = self.make_flow(entry)
        flow.hass.config_entries.entries = [entry, duplicate]

        result = await flow.async_step_init(
            {CONF_PROFILE: "worker", CONF_PROMPT: "must not persist"}
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "already_configured"})
        self.assertEqual(entry.data, original_data)
        self.assertEqual(entry.title, original_title)
        self.assertEqual(entry.options, {CONF_PROMPT: "keep me"})

    async def test_concurrent_entries_cannot_commit_the_same_connection_identity(self):
        first = FakeConfigEntry(
            entry_id="first",
            data=connection_input(**{CONF_PROFILE: "first"}),
        )
        second = FakeConfigEntry(
            entry_id="second",
            data=connection_input(**{CONF_PROFILE: "second"}),
        )
        hass = FakeHass()
        hass.config_entries.entries = [first, second]
        first_flow = self.make_flow(first, hass=hass)
        second_flow = self.make_flow(second, hass=hass)
        both_probing = asyncio.Event()
        release_probes = asyncio.Event()
        probe_count = 0

        async def synchronized_probe(_client):
            nonlocal probe_count
            probe_count += 1
            if probe_count == 2:
                both_probing.set()
            await release_probes.wait()
            return True

        with mock.patch(
            "custom_components.hermes_conversation.config_flow."
            "HermesApiClient.async_check_connection",
            new=synchronized_probe,
        ):
            tasks = [
                asyncio.create_task(
                    first_flow.async_step_init({CONF_PROFILE: "shared"})
                ),
                asyncio.create_task(
                    second_flow.async_step_init({CONF_PROFILE: "shared"})
                ),
            ]
            await asyncio.wait_for(both_probing.wait(), timeout=1)
            release_probes.set()
            results = await asyncio.gather(*tasks)

        self.assertEqual(
            sorted(result["type"] for result in results),
            ["create_entry", "form"],
        )
        rejected = next(result for result in results if result["type"] == "form")
        self.assertEqual(rejected["errors"], {"base": "already_configured"})
        self.assertEqual(
            sum(entry.data[CONF_PROFILE] == "shared" for entry in (first, second)),
            1,
        )

    async def test_concurrent_updates_cannot_overwrite_a_validated_entry_change(self):
        entry = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "original"}),
        )
        hass = FakeHass()
        hass.config_entries.entries = [entry]
        first_flow = self.make_flow(entry, hass=hass)
        second_flow = self.make_flow(entry, hass=hass)
        both_probing = asyncio.Event()
        release_probes = asyncio.Event()
        probe_count = 0

        async def synchronized_probe(_client):
            nonlocal probe_count
            probe_count += 1
            if probe_count == 2:
                both_probing.set()
            await release_probes.wait()
            return True

        with mock.patch(
            "custom_components.hermes_conversation.config_flow."
            "HermesApiClient.async_check_connection",
            new=synchronized_probe,
        ):
            tasks = [
                asyncio.create_task(
                    first_flow.async_step_init({CONF_PROFILE: "first"})
                ),
                asyncio.create_task(
                    second_flow.async_step_init({CONF_PROFILE: "second"})
                ),
            ]
            await asyncio.wait_for(both_probing.wait(), timeout=1)
            release_probes.set()
            results = await asyncio.gather(*tasks)

        self.assertEqual(
            sorted(result["type"] for result in results),
            ["create_entry", "form"],
        )
        rejected = next(result for result in results if result["type"] == "form")
        self.assertEqual(rejected["errors"], {"base": "entry_changed"})
        self.assertIn(entry.data[CONF_PROFILE], {"first", "second"})
        self.assertEqual(len(hass.config_entries.updated), 1)

    async def test_entry_replaced_during_probe_is_rejected_without_mutation(self):
        original = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "original"}),
        )
        replacement = FakeConfigEntry(
            entry_id="current",
            data=connection_input(**{CONF_PROFILE: "replacement"}),
        )
        hass = FakeHass()
        flow = self.make_flow(original, hass=hass)
        probing = asyncio.Event()
        release_probe = asyncio.Event()

        async def blocked_probe(_client):
            probing.set()
            await release_probe.wait()
            return True

        with mock.patch(
            "custom_components.hermes_conversation.config_flow."
            "HermesApiClient.async_check_connection",
            new=blocked_probe,
        ):
            task = asyncio.create_task(
                flow.async_step_init({CONF_PROFILE: "stale"})
            )
            await asyncio.wait_for(probing.wait(), timeout=1)
            hass.config_entries.entries = [replacement]
            release_probe.set()
            result = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "entry_changed"})
        self.assertEqual(replacement.data[CONF_PROFILE], "replacement")
        self.assertEqual(hass.config_entries.updated, [])


class PublicationMetadataTests(unittest.TestCase):
    def test_manifest_is_version_1_1_0(self):
        manifest = json.loads((COMPONENT_ROOT / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "1.1.0")

    def test_strings_and_english_translation_are_equivalent(self):
        strings = json.loads((COMPONENT_ROOT / "strings.json").read_text())
        translation = json.loads((COMPONENT_ROOT / "translations" / "en.json").read_text())

        self.assertEqual(strings, translation)
        self.assertEqual(strings["config"]["step"]["user"]["data"][CONF_PROFILE], "Profile")
        self.assertIn("invalid_profile", strings["config"]["error"])
        self.assertEqual(
            strings["options"]["error"]["already_configured"],
            "This Hermes Agent endpoint and profile are already configured.",
        )
        for error_key in (
            "cannot_connect",
            "entry_changed",
            "invalid_auth",
            "invalid_profile",
            "unknown",
        ):
            self.assertIn(error_key, strings["options"]["error"])
        self.assertEqual(strings["options"]["step"]["init"]["data"][CONF_PROFILE], "Profile")

    def test_readme_documents_profile_route_and_primary_workaround_neutrally(self):
        readme = (REPO_ROOT / "README.md").read_text()
        lowered = readme.lower()

        self.assertIn("make the desired profile primary", lowered)
        self.assertIn("sanitized route name", lowered)
        self.assertIn("not a filesystem path", lowered)
        self.assertIn("worker", lowered)
        self.assertIn("assistant_2", lowered)


if __name__ == "__main__":
    unittest.main()
