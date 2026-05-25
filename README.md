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
- **Multi-turn** — supports both local HA-side history and Hermes-backed session reuse
- **Username resolution** — passes the user's name to the agent
- **Configurable** — connection settings and prompt options can be changed anytime via Configure
- **Multiple instances** — connect to both the local add-on and an external Hermes Agent

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
4. Enter the **Host**, **Port** (default: 8443), and the **API Key** (the Access Password from the add-on configuration)
5. **Use HTTPS** is on by default (the add-on uses a self-signed certificate)
6. **Verify SSL certificate** is off by default (for self-signed certs)
7. Click **Submit**

### Using as Voice Assistant

1. Go to **Settings → Voice Assistants**
2. Create a new assistant or edit an existing one
3. Select **Hermes Agent** as the **Conversation agent**
4. Disable **Prefer handling commands locally** (Hermes Agent handles everything)

### Options

After setup, all settings can be changed via **Settings → Devices & Services → Hermes Agent → Configure**:

| Option                                   | Default             | Description                                                                            |
| ---------------------------------------- | ------------------- | -------------------------------------------------------------------------------------- |
| Host                                     | homeassistant.local | Hermes Agent hostname or IP                                                            |
| Port                                     | 8443                | API port                                                                               |
| API Key                                  | (empty)             | API key (the Access Password from the add-on configuration)                            |
| Use HTTPS                                | Yes                 | Connect via HTTPS                                                                      |
| Verify SSL certificate                   | No                  | Verify the SSL certificate (disable for self-signed)                                   |
| System Prompt                            | (built-in)          | Jinja2 template — leave empty to use Hermes Agent's own prompt                         |
| Include exposed entities                 | No                  | Include smart home device states in the system prompt                                  |
| Max context characters                   | 12000               | Character limit for the entity context block                                           |
| Keep HA listening for follow-ups         | No                  | Home Assistant continued-conversation mode                                             |
| Reuse Hermes server sessions             | Yes                 | Preserve short-term context across fresh wake-word turns via `X-Hermes-Session-Id`     |
| Voice session reuse timeout (seconds)    | 900                 | Idle timeout before a remembered voice session expires                                 |
| Include device/satellite context         | Yes                 | Append voice-origin device/area/satellite metadata to the prompt                       |
| Always speak replies through fallback    | No                  | Also send voice replies through a fallback `tts.speak` target for device-origin turns  |
| Fallback media player entity_id          | (empty)             | Media player target for fallback speech                                                |
| Fallback TTS entity_id                   | (empty)             | TTS entity used for fallback speech                                                    |

The default system prompt includes the current date/time, timezone, the user's name, the home name, and exposed device states (if enabled). Entity exposure is off by default since Hermes Agent can access Home Assistant entities directly when a Home Assistant token is configured in the Hermes Agent add-on.

### Voice continuity modes

Recommended mode if you want to say the wake word each turn **without** losing short-term context:

- **Keep HA listening for follow-ups:** Off
- **Reuse Hermes server sessions:** On

This separates Home Assistant's continued-conversation UX from Hermes's backend memory continuity.

## How It Works

This integration communicates with Hermes Agent's OpenAI-compatible API (`/v1/chat/completions`) using only Home Assistant's built-in HTTP client — **no external Python dependencies**.

Hermes Agent handles tool execution (controlling lights, checking sensors, etc.) server-side through its own Home Assistant tools. This means the conversation integration stays simple: it sends your message, gets back the response (which may include results from tool actions the agent performed), and displays it.

## License

[MIT](LICENSE)
