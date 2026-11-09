# Changelog

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
