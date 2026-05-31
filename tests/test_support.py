from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class FakeConfigEntry:
    def __init__(self, data=None, options=None, entry_id="entry-1"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.update_listeners = []

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return listener

    def async_on_unload(self, value):
        return value


class FakeIntentResponse:
    def __init__(self, language=None):
        self.language = language
        self.speech = None
        self.error = None

    def async_set_error(self, code, message):
        self.error = {"code": code, "message": message}

    def async_set_speech(self, text):
        self.speech = {"plain": {"speech": text}}


@dataclass
class FakeConversationResult:
    response: object
    conversation_id: str
    continue_conversation: bool


@dataclass
class FakeAssistantContent:
    agent_id: str
    content: str
    role: str = "assistant"


class FakeChatLog:
    def __init__(self, conversation_id, user_text):
        self.conversation_id = conversation_id
        self.content = [SimpleNamespace(role="user", content=user_text)]
        self.deltas = []

    async def async_add_delta_content_stream(self, agent_id, stream):
        current_content = ""
        async for delta in stream:
            self.deltas.append(delta)
            if "role" in delta and current_content:
                content = FakeAssistantContent(agent_id=agent_id, content=current_content)
                self.content.append(content)
                yield content
                current_content = ""
            if delta_content := delta.get("content"):
                current_content += delta_content

        if current_content:
            content = FakeAssistantContent(agent_id=agent_id, content=current_content)
            self.content.append(content)
            yield content


@contextmanager
def fake_async_get_chat_session(hass, conversation_id):
    yield SimpleNamespace(
        conversation_id=conversation_id or "default",
        async_on_cleanup=lambda callback: None,
    )


@contextmanager
def fake_async_get_chat_log(hass, session, user_input):
    chat_log = FakeChatLog(
        session.conversation_id,
        user_input.text if user_input is not None else "",
    )
    hass.data["last_chat_log"] = chat_log
    yield chat_log


class FakeTemplate:
    def __init__(self, text, hass):
        self.text = text
        self.hass = hass

    def async_render(self, variables):
        rendered = self.text
        rendered = rendered.replace("{{ user_name }}", str(variables.get("user_name", "")))
        rendered = rendered.replace("{{ ha_name }}", str(variables.get("ha_name", "")))
        return rendered


class FakeTemplateError(Exception):
    pass


class FakeConversationInput:
    def __init__(
        self,
        text,
        *,
        language="en",
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        context=None,
        extra_system_prompt=None,
    ):
        self.text = text
        self.language = language
        self.conversation_id = conversation_id
        self.device_id = device_id
        self.satellite_id = satellite_id
        self.context = context
        self.extra_system_prompt = extra_system_prompt


class FakeClientTimeout:
    def __init__(self, total=None, sock_read=None):
        self.total = total
        self.sock_read = sock_read


class FakeClientError(Exception):
    pass


class FakeAuthStore:
    async def async_get_user(self, user_id):
        return SimpleNamespace(name=f"user-{user_id}")


class FakeConfigEntries:
    def __init__(self):
        self.updated = []
        self.reloaded = []
        self.forwarded = []
        self.unloaded = []

    def async_update_entry(self, entry, *, data=None):
        if data is not None:
            entry.data = data
        self.updated.append((entry, data))

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry.entry_id, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms):
        self.unloaded.append((entry.entry_id, tuple(platforms)))
        return True

    def async_get_entry(self, entry_id):
        return None


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, service_data, blocking=False):
        self.calls.append((domain, service, service_data, blocking))


class FakeStates:
    def __init__(self, states=None):
        self._states = list(states or [])

    def async_all(self):
        return list(self._states)

    def get(self, entity_id):
        for state in self._states:
            if state.entity_id == entity_id:
                return state
        return None


class FakeHass:
    def __init__(self, *, session=None, states=None, location_name="Home"):
        self._session = session
        self.config = SimpleNamespace(location_name=location_name)
        self.auth = FakeAuthStore()
        self.services = FakeServices()
        self.states = FakeStates(states)
        self.data = {}
        self.config_entries = FakeConfigEntries()
        self._device_registry = SimpleNamespace(async_get=lambda device_id: None)
        self._area_registry = SimpleNamespace(async_get_area=lambda area_id: None)


def install_stubs():
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = FakeConfigEntry
    config_entries.ConfigFlow = type("ConfigFlow", (), {})
    config_entries.OptionsFlow = type("OptionsFlow", (), {})

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = FakeHass
    core.callback = lambda fn: fn

    const = types.ModuleType("homeassistant.const")
    const.MATCH_ALL = "*"
    const.Platform = SimpleNamespace(CONVERSATION="conversation")

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.AbortFlow = type("AbortFlow", (Exception,), {})

    conversation = types.ModuleType("homeassistant.components.conversation")
    conversation.AbstractConversationAgent = type("AbstractConversationAgent", (), {})
    conversation.ChatLog = FakeChatLog
    conversation.ConversationEntityFeature = SimpleNamespace(CONTROL=1)
    conversation.ConversationInput = FakeConversationInput
    conversation.ConversationResult = FakeConversationResult
    conversation.MATCH_ALL = "*"
    conversation.async_get_chat_log = fake_async_get_chat_log
    conversation.async_set_agent = lambda hass, entry, agent: hass.data.setdefault("set_agents", []).append((entry.entry_id, agent))
    conversation.async_unset_agent = lambda hass, entry: hass.data.setdefault("unset_agents", []).append(entry.entry_id)

    class FakeConversationEntity:
        _attr_supports_streaming = False

        @property
        def supports_streaming(self):
            return self._attr_supports_streaming

        async def async_added_to_hass(self):
            return None

        async def async_will_remove_from_hass(self):
            return None

        async def async_process(self, user_input):
            chat_log = FakeChatLog(
                user_input.conversation_id or "default",
                user_input.text,
            )
            self._last_chat_log = chat_log
            return await self._async_handle_message(user_input, chat_log)

    conversation.ConversationEntity = FakeConversationEntity

    exposed = types.ModuleType("homeassistant.components.homeassistant.exposed_entities")
    exposed.async_should_expose = lambda hass, platform, entity_id: True

    intent = types.ModuleType("homeassistant.helpers.intent")
    intent.IntentResponse = FakeIntentResponse
    intent.IntentResponseErrorCode = SimpleNamespace(UNKNOWN="unknown")

    template = types.ModuleType("homeassistant.helpers.template")
    template.Template = FakeTemplate
    template.TemplateError = FakeTemplateError

    area_registry = types.ModuleType("homeassistant.helpers.area_registry")
    area_registry.async_get = lambda hass: hass._area_registry

    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda hass: hass._device_registry

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass._session

    chat_session = types.ModuleType("homeassistant.helpers.chat_session")
    chat_session.async_get_chat_session = fake_async_get_chat_session

    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.SelectOptionDict = dict
    selector.SelectSelector = lambda config=None: {"select_selector": config}
    selector.SelectSelectorConfig = lambda **kwargs: kwargs
    selector.TextSelector = lambda config=None: {"selector": config}
    selector.TextSelectorConfig = lambda **kwargs: kwargs

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.intent = intent
    helpers.template = template
    helpers.area_registry = area_registry
    helpers.device_registry = device_registry
    helpers.aiohttp_client = aiohttp_client
    helpers.chat_session = chat_session
    helpers.selector = selector

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = FakeClientTimeout
    aiohttp.ClientError = FakeClientError
    aiohttp.ClientSession = object

    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda x: x
    voluptuous.Required = lambda key, default=None: key
    voluptuous.Optional = lambda key, default=None: key
    voluptuous.All = lambda *args, **kwargs: (args, kwargs)
    voluptuous.Coerce = lambda arg: arg
    voluptuous.Range = lambda **kwargs: kwargs

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow
    sys.modules["homeassistant.components.conversation"] = conversation
    sys.modules["homeassistant.components.homeassistant.exposed_entities"] = exposed
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.intent"] = intent
    sys.modules["homeassistant.helpers.template"] = template
    sys.modules["homeassistant.helpers.area_registry"] = area_registry
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    sys.modules["homeassistant.helpers.chat_session"] = chat_session
    sys.modules["homeassistant.helpers.selector"] = selector
    sys.modules["aiohttp"] = aiohttp
    sys.modules["voluptuous"] = voluptuous


install_stubs()
