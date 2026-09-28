from __future__ import annotations

import asyncio
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
    def __init__(self, data=None, options=None, entry_id="entry-1", title="Hermes Agent"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title
        self.update_listeners = []
        self._on_unload = []
        self._background_tasks = set()

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)

        def _remove():
            if listener in self.update_listeners:
                self.update_listeners.remove(listener)

        return _remove

    def async_on_unload(self, value):
        self._on_unload.append(value)
        return value

    def async_create_background_task(self, hass, target, name, eager_start=True):
        task = hass.async_create_background_task(target, name, eager_start)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_execute_unload(self):
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while self._on_unload:
            callback = self._on_unload.pop()
            result = callback()
            if hasattr(result, "__await__"):
                await result


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


class FailingSession:
    def get(self, *args, **kwargs):
        raise FakeClientError("offline")

    def post(self, *args, **kwargs):
        raise FakeClientError("offline")


class FakeAuthStore:
    async def async_get_user(self, user_id):
        return SimpleNamespace(name=f"user-{user_id}")


class FakeConfigEntries:
    def __init__(self, hass=None):
        self.hass = hass
        self.updated = []
        self.reloaded = []
        self.forwarded = []
        self.unloaded = []
        self.entries = []
        self.pending_updates = []

    def async_entries(self, _domain=None):
        return list(self.entries)

    def async_update_entry(self, entry, *, data=None, title=None, options=None):
        changed = False
        if data is not None and data != entry.data:
            entry.data = data
            changed = True
        if title is not None and title != entry.title:
            entry.title = title
            changed = True
        if options is not None and options != entry.options:
            entry.options = options
            changed = True
        if changed:
            self.updated.append((entry, data, title, options))
            self.pending_updates.append(entry)

    async def async_process_pending_updates(self):
        pending = self.pending_updates
        self.pending_updates = []
        for entry in pending:
            for listener in entry.update_listeners:
                await listener(self.hass, entry)

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry.entry_id, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms):
        self.unloaded.append((entry.entry_id, tuple(platforms)))
        return True

    def async_get_entry(self, entry_id):
        return next(
            (entry for entry in self.entries if entry.entry_id == entry_id),
            None,
        )


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
        self._session = session if session is not None else FailingSession()
        self.config = SimpleNamespace(location_name=location_name)
        self.auth = FakeAuthStore()
        self.services = FakeServices()
        self.states = FakeStates(states)
        self.data = {}
        self.config_entries = FakeConfigEntries(self)
        self._entity_registry = SimpleNamespace(async_get=lambda entity_id: None)
        self._device_registry = SimpleNamespace(async_get=lambda device_id: None)
        self._area_registry = SimpleNamespace(async_get_area=lambda area_id: None)
        self._background_tasks = set()

    def async_create_background_task(self, target, name, eager_start=True):
        task = asyncio.create_task(target, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_block_till_done(self, wait_background_tasks=False):
        if wait_background_tasks and self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks))


def install_stubs():
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    class FakeFlowBase:
        def __init__(self):
            self.hass = None

        def async_show_form(self, *, step_id, data_schema, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
            }

        def async_create_entry(self, *, title, data):
            return {"type": "create_entry", "title": title, "data": data}

    class FakeConfigFlow(FakeFlowBase):
        def __init_subclass__(cls, **kwargs):
            cls.domain = kwargs.pop("domain", None)
            super().__init_subclass__()

        def __init__(self):
            super().__init__()
            self._current_entries = []

        def _async_current_entries(self):
            return list(self._current_entries)

    class FakeOptionsFlow(FakeFlowBase):
        pass

    config_entries.ConfigEntry = FakeConfigEntry
    config_entries.ConfigFlow = FakeConfigFlow
    config_entries.OptionsFlow = FakeOptionsFlow

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = FakeHass
    core.callback = lambda fn: fn

    const = types.ModuleType("homeassistant.const")
    const.MATCH_ALL = "*"
    const.Platform = SimpleNamespace(
        CONVERSATION="conversation",
        SENSOR="sensor",
        BINARY_SENSOR="binary_sensor",
    )

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")

    class FakeAbortFlow(Exception):
        def __init__(self, reason):
            super().__init__(reason)
            self.reason = reason

    data_entry_flow.AbortFlow = FakeAbortFlow

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

    class FakeEntity:
        _attr_should_poll = False
        _attr_unique_id = None
        _attr_name = None
        _attr_device_info = None
        _attr_entity_category = None
        _attr_translation_key = None
        _attr_has_entity_name = False
        _attr_available = True
        _attr_extra_state_attributes = None
        _attr_native_value = None
        _attr_native_unit_of_measurement = None
        _attr_device_class = None
        _attr_state_class = None
        _attr_options = None
        _attr_is_on = None
        hass = None

        def __init__(self):
            self._remove_callbacks = []
            self.written_states = 0

        def async_on_remove(self, func):
            self._remove_callbacks.append(func)
            return func

        def async_write_ha_state(self):
            self.written_states += 1

        @property
        def unique_id(self):
            return self._attr_unique_id

        @property
        def name(self):
            return self._attr_name

        @property
        def device_info(self):
            return self._attr_device_info

        @property
        def entity_category(self):
            return self._attr_entity_category

        @property
        def translation_key(self):
            return self._attr_translation_key

        @property
        def has_entity_name(self):
            return self._attr_has_entity_name

        @property
        def available(self):
            return self._attr_available

        @property
        def extra_state_attributes(self):
            return self._attr_extra_state_attributes

        @property
        def native_value(self):
            return self._attr_native_value

        @property
        def native_unit_of_measurement(self):
            return self._attr_native_unit_of_measurement

        @property
        def device_class(self):
            return self._attr_device_class

        @property
        def state_class(self):
            return self._attr_state_class

        @property
        def options(self):
            return self._attr_options

        @property
        def is_on(self):
            return self._attr_is_on

    class FakeConversationEntity(FakeEntity):
        _attr_supports_streaming = False

        @property
        def supports_streaming(self):
            return self._attr_supports_streaming

        @property
        def unique_id(self):
            return self._attr_unique_id

        @property
        def name(self):
            return self._attr_name

        @property
        def device_info(self):
            return self._attr_device_info

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

    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: hass._entity_registry

    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda hass: hass._device_registry
    device_registry.DeviceEntryType = SimpleNamespace(SERVICE="service")
    device_registry.DeviceInfo = dict

    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.Entity = FakeEntity
    entity.EntityCategory = SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic")

    class UpdateFailed(Exception):
        pass

    class FakeDataUpdateCoordinator:
        def __init__(
            self,
            hass,
            logger,
            *,
            name=None,
            update_interval=None,
            config_entry=None,
            **kwargs,
        ):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.config_entry = config_entry
            self.data = None
            self.last_update_success = True
            self._listeners = []
            self.refresh_count = 0
            self._shutdown_requested = False
            self.shutdown = False
            self.used_first_refresh = False
            if self.config_entry:
                self.config_entry.async_on_unload(self.async_shutdown)

        def async_add_listener(self, update_callback, context=None):
            self._listeners.append(update_callback)

            def _remove():
                if update_callback in self._listeners:
                    self._listeners.remove(update_callback)

            return _remove

        def async_update_listeners(self):
            for listener in list(self._listeners):
                listener()

        async def async_refresh(self):
            if self._shutdown_requested:
                return self.data
            self.refresh_count += 1
            self.data = await self._async_update_data()
            self.last_update_success = True
            self.async_update_listeners()
            return self.data

        async def async_config_entry_first_refresh(self):
            self.used_first_refresh = True
            exceptions = types.ModuleType("homeassistant.exceptions")
            class ConfigEntryNotReady(Exception):
                pass
            try:
                await self.async_refresh()
            except Exception as err:
                raise ConfigEntryNotReady from err

        async def async_shutdown(self):
            self._shutdown_requested = True
            self.shutdown = True
            self._listeners.clear()

        def __class_getitem__(cls, item):
            return cls

    class FakeCoordinatorEntity(FakeEntity):
        def __init__(self, coordinator):
            super().__init__()
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

        async def async_added_to_hass(self):
            self.async_on_remove(
                self.coordinator.async_add_listener(self._handle_coordinator_update)
            )

        def _handle_coordinator_update(self):
            self.async_write_ha_state()

        def __class_getitem__(cls, item):
            return cls

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = FakeDataUpdateCoordinator
    update_coordinator.CoordinatorEntity = FakeCoordinatorEntity
    update_coordinator.UpdateFailed = UpdateFailed

    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor.SensorEntity = FakeEntity
    sensor.SensorDeviceClass = SimpleNamespace(
        ENUM="enum",
        TIMESTAMP="timestamp",
        DURATION="duration",
    )
    sensor.SensorStateClass = SimpleNamespace(MEASUREMENT="measurement")

    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    binary_sensor.BinarySensorEntity = FakeEntity
    binary_sensor.BinarySensorDeviceClass = SimpleNamespace(CONNECTIVITY="connectivity")

    exceptions = types.ModuleType("homeassistant.exceptions")
    class ConfigEntryNotReady(Exception):
        pass
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

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
    helpers.entity_registry = entity_registry
    helpers.device_registry = device_registry
    helpers.aiohttp_client = aiohttp_client
    helpers.chat_session = chat_session
    helpers.selector = selector
    helpers.entity = entity
    helpers.update_coordinator = update_coordinator

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
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.components.conversation"] = conversation
    sys.modules["homeassistant.components.homeassistant.exposed_entities"] = exposed
    sys.modules["homeassistant.components.sensor"] = sensor
    sys.modules["homeassistant.components.binary_sensor"] = binary_sensor
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.intent"] = intent
    sys.modules["homeassistant.helpers.template"] = template
    sys.modules["homeassistant.helpers.area_registry"] = area_registry
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    sys.modules["homeassistant.helpers.chat_session"] = chat_session
    sys.modules["homeassistant.helpers.selector"] = selector
    sys.modules["homeassistant.helpers.entity"] = entity
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["aiohttp"] = aiohttp
    sys.modules["voluptuous"] = voluptuous


install_stubs()
