# Changelog

## [1.4.5] - 2026-11-25

### Fixed

- Show streamed replies only once in the Assist text UI. The final response now reuses the streamed text (trimmed), so Home Assistant no longer appends a second copy when the integration's extra speech cleanup had changed punctuation spacing or formatting. History, follow-up detection, fallback TTS and non-streaming replies keep the existing cleanup. Thanks to @Smokyr for #29.

### Added

- Optional **Speak only the final answer (drop pre-tool text)** in **Configure**. When enabled, text Hermes writes before a tool call is discarded and only the answer after the last `hermes.tool.progress` event is spoken and shown, once the reply completes. This disables incremental streaming and needs Hermes' tool-progress events (on by default). Off by default. Thanks to @ds-sebastian for #27.

### Verification

- 237 unit tests passed with Home Assistant stubs, including exact final/streamed text agreement, partial failures, intentional silence and the new option's buffering and failure handling. No live Home Assistant runtime test was performed.

## [1.4.4] - 2026-11-18

### Added

- Make the Hermes request timeout and the streamed-reply time limit configurable from **Configure** (previously fixed at 120 s and 300 s, so slow reasoning models or long tool runs failed with a timeout and no way to raise it). An existing legacy `timeout` entry value is still honoured as the request timeout until the new option is set.

### Verification

- 227 unit tests passed with Home Assistant stubs, including configured/default timeout propagation, legacy values and streaming-limit clamping. No live Home Assistant runtime test was performed.

## [1.4.3] - 2026-11-09

### Fixed

- Pass an explicit chat-session context on Home Assistant versions that support it, removing the 2026.11 deprecation warning and supporting the announced 2027.5 requirement. Preserve the previous `None` behavior and compatibility with older Home Assistant signatures. Thanks to @karmateam5555 for #25.

## [1.4.2] - 2026-11-01

### Fixed

- Validate native profile routing with an impossible profile ID instead of `hermes`, which newer gateways may use as the default profile's handle. Correct native connections now pass validation while credential-free checks still block gateways that ignore the profile prefix. Thanks to @dtk1985 for #23.

## [1.4.1] - 2026-10-24

### Fixed

- Consume a complete `NO_REPLY` control response before Home Assistant speech, including markers split across streaming chunks. Intentional silence produces no fallback speech and no follow-up listening, including Always mode. Formatted mentions and ordinary prose remain speech. Thanks to @rmaffeo for #22.
- Refresh the release-version assertions left at 1.3.1 after the 1.4.0 release.

## [1.4.0] - 2026-10-12

### Added

- Track conversation requests originating through each Home Assistant integration entry (Assist or `conversation.process`) with four push-only entities named **Processing conversation**, **Active conversation requests**, **Last conversation duration**, and **Last conversation result**, plus a safe lifecycle event. Counts handle overlap; results distinguish completed, partial, failed, and cancelled requests without exposing conversation content. This does not measure global Hermes activity.
- Detect incomplete streaming responses rather than reporting a truncated SSE response as complete; reject explicit Hermes error/incomplete terminal metadata even when followed by `[DONE]` and sanitize server error details.

## [1.3.1] - 2026-10-04

### Fixed

- Update the last successful connection timestamp on the first successful contact and after connectivity recovers. Successful polls keep the timestamp stable, avoiding repeated Activity/Logbook state changes while other diagnostic sensors continue to update.
- Cover repeated successful polls and recovery after returned offline data or a failed coordinator refresh.

### Documentation

- Clarify that the connection timestamp records connection transitions rather than each health poll.
