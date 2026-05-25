"""Compatibility helpers for newer and legacy Hermes config entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LEGACY_CONF_API_BASE_URL,
)


@dataclass(slots=True, frozen=True)
class HermesConnectionConfig:
    """Resolved connection config for a Hermes API entry."""

    host: str
    port: int
    api_key: str | None
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


def resolve_connection_config(entry: ConfigEntry) -> HermesConnectionConfig:
    """Resolve connection details from current or legacy config layout."""
    host = entry_value(entry, CONF_HOST, prefer_options=False)
    port = entry_value(entry, CONF_PORT, prefer_options=False)
    api_key = entry_value(entry, CONF_API_KEY, prefer_options=False) or None
    use_ssl = entry_value(entry, CONF_USE_SSL, prefer_options=False)
    verify_ssl = entry_value(entry, CONF_VERIFY_SSL, prefer_options=False)

    if host and port is not None:
        return HermesConnectionConfig(
            host=str(host),
            port=_coerce_int(port, DEFAULT_PORT),
            api_key=api_key,
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
            use_ssl=parsed.use_ssl if use_ssl is None else bool(use_ssl),
            verify_ssl=False if verify_ssl is None else bool(verify_ssl),
        )

    return HermesConnectionConfig(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        api_key=api_key,
        use_ssl=True if use_ssl is None else bool(use_ssl),
        verify_ssl=False if verify_ssl is None else bool(verify_ssl),
    )


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
    port = parsed.port or (443 if use_ssl else 80)
    return ParsedApiBaseUrl(host=parsed.hostname, port=port, use_ssl=use_ssl)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
