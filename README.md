# Hermes Agent

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Home Assistant](https://home-assistant.io/) custom integration that connects [Hermes Agent](https://hermes-agent.nousresearch.com/) by [Nous Research](https://nousresearch.com/) as a **conversation agent** for voice assistants and the conversation panel.

## Features

- **Conversation agent** — use Hermes Agent as your voice assistant in Home Assistant
- **Streaming** — low latency for voice pipelines (first token arrives fast)
- **Hermes session continuity** — reuses Hermes `X-Hermes-Session-Id` sessions across short voice turns
- **Voice-origin awareness** — can key continuity from `device_id` / `satellite_id` and pass room context into the prompt
- **Entity exposure** - includes exposed entity names, aliases, domains, areas, and states in the system prompt
- **Follow-up listening modes** — keep Assist closed, always listening, or listening only when Hermes asks a question
- **Multi-turn** — supports both local HA-side history and Hermes-backed session reuse
- **Username resolution** — passes the user's name to the agent
- **Configurable** — connection settings and prompt options can be changed anytime via Configure
- **Multiple instances and profiles** — connect Assist agents to the root API, Home Assistant add-on profile routes, or native Hermes multiplexer routes

## Requirements

- Home Assistant 2024.12 or newer
- A running [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance with the API server enabled:
  - **Easiest:** Install the [Hermes Agent add-on](https://github.com/WolframRavenwolf/hermes-ha-addon) and enable the API in the add-on configuration
  - **Alternative:** Run Hermes Agent standalone and point the integration at its API endpoint

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots in the top right → **Custom repositories**
3. Add `https://github.com/WolframRavenwolf/hermes-ha-integration` as an **Integration**
4. Search for "Hermes Agent" and install it
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/hermes_conversation` folder to your Home Assistant `custom_components` directory
2. Restart Home Assistant

## Configuration

### Setup

1. Make sure the Hermes Agent API is running. For the Home Assistant add-on, turn on **Enable API**; for native profile routes, enable the Hermes profile multiplexer
2. Go to **Settings → Devices & Services → Add Integration**
3. Search for "Hermes Agent"
4. Enter the **Host** as a DNS name or IP address only (no scheme, path, user info, query, or fragment) and the **Port** (default: 8443)
5. Select the **Profile route type**: **Home Assistant add-on** for `/profile/<name>` or **Native Hermes multiplexer** for `/p/<name>`
6. Leave **Profile** blank for the root API, or enter a route name such as `worker`
7. Enter the **API Key**. The add-on uses its Access Password; native multiplexer routes require each profile's own API key
8. **Use HTTPS** is on by default (the add-on uses a self-signed certificate)
9. **Verify SSL certificate** is off by default (for self-signed certs)
10. Click **Submit**

### Using as Voice Assistant

1. Go to **Settings → Voice Assistants**
2. Create a new assistant or edit an existing one
3. Select **Hermes Agent** as the **Conversation agent**
4. Disable **Prefer handling commands locally** (Hermes Agent handles everything)

### Options

After setup, all settings can be changed via **Settings → Devices & Services → Hermes Agent → Configure**:

| Option                                   | Default             | Description                                                                            |
| ---------------------------------------- | ------------------- | -------------------------------------------------------------------------------------- |
| Host                                     | homeassistant.local | Hermes Agent DNS name or IP only; do not include scheme, path, user info, query, or fragment     |
| Port                                     | 8443                | API port                                                                               |
| Profile                                  | (empty)             | Route-specific profile name; blank selects the root API                                |
| Profile route type                       | Home Assistant add-on | Add-on `/profile/<name>` or native multiplexer `/p/<name>`                           |
| API Key                                  | (empty)             | Add-on Access Password, or the selected native profile's own API key                   |
| Use HTTPS                                | Yes                 | Connect via HTTPS                                                                      |
| Verify SSL certificate                   | No                  | Verify the SSL certificate (disable for self-signed)                                   |
| System Prompt                            | (built-in)          | Jinja2 template; empty omits custom context, but speech-format guidance still applies                         |
| Include exposed entities                 | No                  | Include smart home device states in the system prompt                                  |
| Max context characters                   | 12000               | Character limit for the entity context block                                           |
| Follow-up listening                      | Off                 | Off, always on, or automatic only when Hermes asks a follow-up question                 |
| Reuse Hermes server sessions             | Yes                 | Preserve short-term context across fresh wake-word turns via `X-Hermes-Session-Id`     |
| Voice session reuse timeout (seconds)    | 900                 | Idle timeout before a remembered voice session expires                                 |
| Include device/satellite context         | Yes                 | Append voice-origin device/area/satellite metadata to the prompt                       |
| Always speak replies through fallback    | No                  | Also send voice replies through a fallback `tts.speak` target for device-origin turns  |
| Fallback media player entity_id          | (empty)             | Media player target for fallback speech                                                |
| Fallback TTS entity_id                   | (empty)             | TTS entity used for fallback speech                                                    |

### Profile routes

Create one Hermes Agent integration entry for each Assist agent that should use a different profile. Leave **Profile** blank for the root API; a blank profile stays at the root regardless of the selected route type.

- **Home Assistant add-on** uses `/profile/<name>`. Enter the sanitized route name shown by the add-on, for example `worker` or `assistant_2`. Use letters, numbers, and single underscores only; the value cannot end with an underscore.
- **Native Hermes multiplexer** uses `/p/<name>`. Enable profile multiplexing in Hermes and enter a Hermes profile ID: up to 64 lowercase letters, numbers, hyphens, or underscores, starting with a letter or number. Mixed-case input is stored lowercase. `default` is supported; `hermes`, `test`, `tmp`, `root`, and `sudo` are reserved. Native routes require each profile's own API key.

The value is not a filesystem path: do not enter a directory, `/profile/worker`, `/p/worker`, or any slash-containing value. The integration probes only the selected route type; it does not try both prefixes. Before every credentialed request to a named native route, the integration sends credential-free health requests: the reserved native profile `/p/hermes` must return `404`, and the selected native route must return `200` with a Hermes Agent health identity. Any other response blocks the request before the API key is sent because the server may be ignoring the profile prefix, may not support an unambiguous health check, or may be serving the root profile instead. This verification runs both while saving connection settings and again before runtime models or chat requests, so a later server configuration change also fails closed. Setup and connection-changing Configure submissions then use the selected route's authenticated `/v1/models` endpoint to verify the Bearer key before saving. The legacy health fallback applies only to root and add-on routes: for older Hermes versions that return `404` for `/v1/health`, the authenticated models response must contain at least one entry owned by `hermes`. Configure revalidates both the entry snapshot and endpoint/profile-route uniqueness under a shared lock immediately before committing, so concurrent changes cannot create duplicates or silently overwrite a validated update.

If your installed integration version does not show the **Profile** field yet, the immediate workaround is to make the desired profile primary in the add-on, then leave the integration's profile blank. After updating to a version with profile routing, select each profile through its own integration entry and assign that entry to the desired Home Assistant Assist agent.

The default system prompt includes the current date/time, timezone, the user's name, the home name, and exposed entity names, aliases, domains, areas, and states (if enabled). Entity exposure is off by default since Hermes Agent can access Home Assistant entities directly when a Home Assistant token is configured in the Hermes Agent add-on.

### Voice continuity modes

Recommended mode if you want to say the wake word each turn **without** losing short-term context:

- **Follow-up listening:** Off
- **Reuse Hermes server sessions:** On

This separates Home Assistant's continued-conversation UX from Hermes's backend memory continuity.

Use **Follow-up listening: Auto when Hermes asks a question** if you want Assist
to reopen only when Hermes ends with a direct question.

Replies are requested as plain, speakable text in every mode, including with an
empty custom system prompt. Emoji are removed before streamed or final speech
reaches TTS and before Auto follow-up is evaluated. Meaningful units, percentages,
currencies, and mathematical symbols are preserved; dual-use symbols such as
arrows are removed only when explicitly presented as emoji.

Auto asks Hermes to put one direct question last, with nothing after the question
mark. It still checks the final sentence, not earlier quoted questions or
language-specific phrases such as "Tell me what time."

## How It Works

This integration communicates with Hermes Agent's OpenAI-compatible API (`/v1/chat/completions`) using only Home Assistant's built-in HTTP client — **no external Python dependencies**.

Hermes Agent handles tool execution (controlling lights, checking sensors, etc.) server-side through its own Home Assistant tools. This means the conversation integration stays simple: it sends your message, gets back the response (which may include results from tool actions the agent performed), and displays it.

## License

[MIT](LICENSE)
