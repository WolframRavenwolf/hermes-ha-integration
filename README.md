# Hermes Agent

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Home Assistant](https://home-assistant.io/) custom integration that connects [Hermes Agent](https://hermes-agent.nousresearch.com/) by [Nous Research](https://nousresearch.com/) as a **conversation agent** for voice assistants and the conversation panel.

## Features

- **Conversation agent** — use Hermes Agent as your voice assistant in Home Assistant
- **Streaming** — low latency for voice pipelines (first token arrives fast)
- **Hermes session continuity** — reuses Hermes `X-Hermes-Session-Id` sessions across short voice turns
- **Voice-origin awareness** — can key continuity from `device_id` / `satellite_id` and pass room context into the prompt
- **Entity exposure** — includes your smart home device states in the system prompt
- **Follow-up listening modes** — keep Assist closed, always listening, or listening only when Hermes asks a question
- **Multi-turn** — supports both local HA-side history and Hermes-backed session reuse
- **Username resolution** — passes the user's name to the agent
- **Configurable** — connection settings and prompt options can be changed anytime via Configure
- **Multiple instances and profiles** — connect Assist agents to the primary API or separate add-on profile routes

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

1. Make sure the Hermes Agent add-on is running with **Enable API** turned on
2. Go to **Settings → Devices & Services → Add Integration**
3. Search for "Hermes Agent"
4. Enter the **Host** as a DNS name or IP address only (no scheme, path, user info, query, or fragment), the **Port** (default: 8443), and the **API Key** (the Access Password from the add-on configuration)
5. Leave **Profile** blank for the primary/root API, or enter a sanitized add-on profile route name such as `worker`
6. **Use HTTPS** is on by default (the add-on uses a self-signed certificate)
7. **Verify SSL certificate** is off by default (for self-signed certs)
8. Click **Submit**

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
| Profile                                  | (empty)             | Sanitized add-on profile route name; blank selects the primary/root API                |
| API Key                                  | (empty)             | API key (the Access Password from the add-on configuration)                            |
| Use HTTPS                                | Yes                 | Connect via HTTPS                                                                      |
| Verify SSL certificate                   | No                  | Verify the SSL certificate (disable for self-signed)                                   |
| System Prompt                            | (built-in)          | Jinja2 template — leave empty to use Hermes Agent's own prompt                         |
| Include exposed entities                 | No                  | Include smart home device states in the system prompt                                  |
| Max context characters                   | 12000               | Character limit for the entity context block                                           |
| Follow-up listening                      | Off                 | Off, always on, or automatic only when Hermes asks a follow-up question                 |
| Reuse Hermes server sessions             | Yes                 | Preserve short-term context across fresh wake-word turns via `X-Hermes-Session-Id`     |
| Voice session reuse timeout (seconds)    | 900                 | Idle timeout before a remembered voice session expires                                 |
| Include device/satellite context         | Yes                 | Append voice-origin device/area/satellite metadata to the prompt                       |
| Always speak replies through fallback    | No                  | Also send voice replies through a fallback `tts.speak` target for device-origin turns  |
| Fallback media player entity_id          | (empty)             | Media player target for fallback speech                                                |
| Fallback TTS entity_id                   | (empty)             | TTS entity used for fallback speech                                                    |

### Add-on profiles

Create one Hermes Agent integration entry for each Assist agent that should use a different add-on profile. Leave **Profile** blank for the primary profile. For another profile, enter the sanitized route name shown by the add-on, for example `worker` or `assistant_2`. Use letters, numbers, and single underscores only; the value cannot end with an underscore. The value is not a filesystem path: do not enter a directory, `/profile/worker`, or any slash-containing value. Setup and connection-changing Configure submissions first query public `/v1/health` without sending the API key, then use authenticated `/v1/models` to verify the Bearer key before saving. For older Hermes versions that do not provide `/v1/health` and return `404`, the authenticated models response must contain at least one entry owned by `hermes`; it remains the required identity, reachability, and authentication check. Configure revalidates both the entry snapshot and endpoint/profile uniqueness under a shared lock immediately before committing, so concurrent changes cannot create duplicates or silently overwrite a validated update.

If your installed integration version does not show the **Profile** field yet, the immediate workaround is to make the desired profile primary in the add-on, then leave the integration's profile blank. After updating to a version with profile routing, select each profile through its own integration entry and assign that entry to the desired Home Assistant Assist agent.

The default system prompt includes the current date/time, timezone, the user's name, the home name, and exposed device states (if enabled). Entity exposure is off by default since Hermes Agent can access Home Assistant entities directly when a Home Assistant token is configured in the Hermes Agent add-on.

### Voice continuity modes

Recommended mode if you want to say the wake word each turn **without** losing short-term context:

- **Follow-up listening:** Off
- **Reuse Hermes server sessions:** On

This separates Home Assistant's continued-conversation UX from Hermes's backend memory continuity.

Use **Follow-up listening: Auto when Hermes asks a question** if you want Assist
to reopen only when Hermes ends with a direct question.

## How It Works

This integration communicates with Hermes Agent's OpenAI-compatible API (`/v1/chat/completions`) using only Home Assistant's built-in HTTP client — **no external Python dependencies**.

Hermes Agent handles tool execution (controlling lights, checking sensors, etc.) server-side through its own Home Assistant tools. This means the conversation integration stays simple: it sends your message, gets back the response (which may include results from tool actions the agent performed), and displays it.

## License

[MIT](LICENSE)
