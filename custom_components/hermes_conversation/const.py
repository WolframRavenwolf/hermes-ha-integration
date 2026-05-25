"""Constants for the Hermes Conversation integration."""

DOMAIN = "hermes_conversation"

# ---------------------------------------------------------------------------
# Config entry keys (stored at setup time)
# ---------------------------------------------------------------------------
CONF_HOST = "host"
CONF_PORT = "port"
CONF_API_KEY = "api_key"
CONF_USE_SSL = "use_ssl"
CONF_VERIFY_SSL = "verify_ssl"

# ---------------------------------------------------------------------------
# Options keys (user-changeable after setup)
# ---------------------------------------------------------------------------
CONF_PROMPT = "prompt"
CONF_INCLUDE_EXPOSED_ENTITIES = "include_exposed_entities"
CONF_CONTEXT_MAX_CHARS = "context_max_chars"
CONF_ENABLE_CONTINUED_CONVERSATION = "enable_continued_conversation"
CONF_ENABLE_SESSION_REUSE = "enable_session_reuse"
CONF_SESSION_TIMEOUT_SECONDS = "session_timeout_seconds"
CONF_EXPOSE_DEVICE_CONTEXT = "expose_device_context"
CONF_ALWAYS_SPEAK_FALLBACK = "always_speak_fallback"
CONF_FALLBACK_MEDIA_PLAYER = "fallback_media_player"
CONF_FALLBACK_TTS_ENGINE = "fallback_tts_engine"

# Legacy config keys still found in existing local installs.
LEGACY_CONF_API_BASE_URL = "api_base_url"
LEGACY_CONF_MODEL = "model"
LEGACY_CONF_TIMEOUT = "timeout"
LEGACY_CONF_INSTRUCTIONS = "instructions"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_HOST = "homeassistant.local"
DEFAULT_PORT = 8443
DEFAULT_CONTEXT_MAX_CHARS = 12000
DEFAULT_INCLUDE_EXPOSED_ENTITIES = False
DEFAULT_MODEL = "hermes-agent"
DEFAULT_TIMEOUT = 120
DEFAULT_STREAM_TIMEOUT = 300
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_ENABLE_CONTINUED_CONVERSATION = False
DEFAULT_ENABLE_SESSION_REUSE = True
DEFAULT_SESSION_TIMEOUT_SECONDS = 900
DEFAULT_EXPOSE_DEVICE_CONTEXT = True
DEFAULT_ALWAYS_SPEAK_FALLBACK = False
DEFAULT_FALLBACK_MEDIA_PLAYER = ""
DEFAULT_FALLBACK_TTS_ENGINE = ""

DEFAULT_PROMPT = (
    "You are in a voice chat with {{ user_name }} via the Home Assistant app.\n"
    "Current date and time: {{ now().strftime('%Y-%m-%d %H:%M %Z') }}.\n"
    "{% if ha_name %}The home is called {{ ha_name }}.{% endif %}\n"
    "{% if exposed_entities %}\n"
    "Available devices:\n"
    "{% for entity in exposed_entities %}"
    "- {{ entity.entity_id }} ({{ entity.name }}): {{ entity.state }}\n"
    "{% endfor %}"
    "{% endif %}\n"
    "Answer in the user's language. Be concise for voice responses."
)

# ---------------------------------------------------------------------------
# API paths
# ---------------------------------------------------------------------------
API_CHAT_COMPLETIONS = "/v1/chat/completions"
API_MODELS = "/v1/models"
API_HEALTH = "/health"
