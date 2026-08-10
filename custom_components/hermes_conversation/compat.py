"""Compatibility helpers for newer and legacy Hermes config entries."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_API_KEY,
    CONF_CONTINUED_CONVERSATION_MODE,
    CONF_ENABLE_CONTINUED_CONVERSATION,
    CONF_HOST,
    CONF_PORT,
    CONF_PROFILE,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_CONTINUED_CONVERSATION_MODE,
    DEFAULT_HOST,
    DEFAULT_ENABLE_CONTINUED_CONVERSATION,
    DEFAULT_PORT,
    FOLLOW_UP_MODE_ALWAYS,
    FOLLOW_UP_MODES,
    LEGACY_CONF_API_BASE_URL,
)


_PROFILE_PATTERN = re.compile(r"^_?[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*$")
_HOST_LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$"
)


@dataclass(slots=True, frozen=True)
class HermesConnectionConfig:
    """Resolved connection config for a Hermes API entry."""

    host: str
    port: int
    api_key: str | None
    profile: str
    use_ssl: bool
    verify_ssl: bool


@dataclass(slots=True, frozen=True)
class ParsedApiBaseUrl:
    """Normalized legacy api_base_url details."""

    host: str
    port: int
    use_ssl: bool


def entry_value(
    entry: ConfigEntry,
    key: str,
    default: Any = None,
    *,
    legacy_keys: tuple[str, ...] = (),
    prefer_options: bool = True,
) -> Any:
    """Read a value from options/data with optional legacy-key fallback."""
    sources = (entry.options, entry.data) if prefer_options else (entry.data, entry.options)

    for source in sources:
        if key in source and source[key] is not None:
            return source[key]
        for legacy_key in legacy_keys:
            if legacy_key in source and source[legacy_key] is not None:
                return source[legacy_key]

    return default


def normalize_profile(value: Any) -> str:
    """Return a safe add-on profile route segment, or fail closed."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Profile must be a string")

    profile = value.strip()
    if not profile:
        return ""
    if _PROFILE_PATTERN.fullmatch(profile) is None:
        raise ValueError("Profile must contain only ASCII letters, digits, and underscores")
    return profile


def normalize_host(value: Any) -> str:
    """Return a canonical host-only DNS name or IP address."""
    if not isinstance(value, str):
        raise ValueError("Host must be a string")

    host = value.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ValueError("Host cannot be blank")
    if "%" in host:
        raise ValueError("Scoped IPv6 addresses are not supported")

    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass

    host = host.rstrip(".").casefold()
    if not host or len(host) > 253:
        raise ValueError("Host is invalid")
    labels = host.split(".")
    if any(_HOST_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise ValueError("Host must be a DNS name or IP address")
    return host


def resolve_connection_config(entry: ConfigEntry) -> HermesConnectionConfig:
    """Resolve connection details from current or legacy config layout."""
    host = entry_value(entry, CONF_HOST, prefer_options=False)
    port = entry_value(entry, CONF_PORT, prefer_options=False)
    api_key = entry_value(entry, CONF_API_KEY, prefer_options=False) or None
    profile = normalize_profile(
        entry_value(entry, CONF_PROFILE, "", prefer_options=False)
    )
    use_ssl = entry_value(entry, CONF_USE_SSL, prefer_options=False)
    verify_ssl = entry_value(entry, CONF_VERIFY_SSL, prefer_options=False)

    if host and port is not None:
        return HermesConnectionConfig(
            host=normalize_host(host),
            port=_coerce_int(port, DEFAULT_PORT),
            api_key=api_key,
            profile=profile,
            use_ssl=True if use_ssl is None else bool(use_ssl),
            verify_ssl=False if verify_ssl is None else bool(verify_ssl),
        )

    api_base_url = entry_value(
        entry,
        LEGACY_CONF_API_BASE_URL,
        prefer_options=False,
    )
    parsed = parse_api_base_url(api_base_url)
    if parsed:
        return HermesConnectionConfig(
            host=parsed.host,
            port=parsed.port,
            api_key=api_key,
            profile=profile,
            use_ssl=parsed.use_ssl if use_ssl is None else bool(use_ssl),
            verify_ssl=False if verify_ssl is None else bool(verify_ssl),
        )

    return HermesConnectionConfig(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        api_key=api_key,
        profile=profile,
        use_ssl=True if use_ssl is None else bool(use_ssl),
        verify_ssl=False if verify_ssl is None else bool(verify_ssl),
    )


def resolve_continued_conversation_mode(entry: ConfigEntry) -> str:
    """Resolve follow-up listening mode with legacy boolean compatibility."""
    mode = entry_value(entry, CONF_CONTINUED_CONVERSATION_MODE)
    if mode in FOLLOW_UP_MODES:
        return str(mode)

    legacy_enabled = entry_value(
        entry,
        CONF_ENABLE_CONTINUED_CONVERSATION,
        DEFAULT_ENABLE_CONTINUED_CONVERSATION,
    )
    if bool(legacy_enabled):
        return FOLLOW_UP_MODE_ALWAYS

    return DEFAULT_CONTINUED_CONVERSATION_MODE


def parse_api_base_url(value: Any) -> ParsedApiBaseUrl | None:
    """Parse an old-style api_base_url into host/port/use_ssl."""
    if not isinstance(value, str):
        return None

    base_url = value.strip()
    if not base_url:
        return None

    if "://" not in base_url:
        base_url = f"https://{base_url}"

    parsed = urlparse(base_url)
    if not parsed.hostname:
        return None

    use_ssl = parsed.scheme != "http"
    try:
        port = parsed.port or (443 if use_ssl else 80)
        host = normalize_host(parsed.hostname)
    except ValueError:
        return None
    return ParsedApiBaseUrl(host=host, port=port, use_ssl=use_ssl)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
