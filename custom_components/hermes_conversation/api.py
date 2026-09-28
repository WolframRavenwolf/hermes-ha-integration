"""HTTP client for the Hermes Agent API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import aiohttp

from .compat import normalize_host, normalize_profile, normalize_profile_route
from .const import (
    API_CHAT_COMPLETIONS,
    API_HEALTH,
    API_HEALTH_DETAILED,
    API_MODELS,
    DEFAULT_MODEL,
    DEFAULT_STREAM_TIMEOUT,
    DEFAULT_TIMEOUT,
    ERROR_CATEGORY_AUTH,
    ERROR_CATEGORY_MALFORMED,
    ERROR_CATEGORY_REDIRECT,
    ERROR_CATEGORY_SERVER,
    ERROR_CATEGORY_TIMEOUT,
    ERROR_CATEGORY_UNREACHABLE,
    ERROR_CATEGORY_UNSUPPORTED,
    HEALTH_STATUSES,
    REDIRECT_STATUS_CODES,
    UNSUPPORTED_DETAILED_STATUS_CODES,
    ProfileRouteFamily,
)

_LOGGER = logging.getLogger(__name__)
_NATIVE_ROUTE_CANARY_PROFILE = "hermes"


class HermesApiError(Exception):
    """Base exception for Hermes API errors."""


class HermesConnectionError(HermesApiError):
    """Cannot reach the Hermes Agent API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_category: str = ERROR_CATEGORY_UNREACHABLE,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_category = error_category


class HermesAuthError(HermesApiError):
    """Authentication failed."""


class HermesStreamSetupError(HermesApiError):
    """Streaming request was rejected before a stream was established."""


@dataclass(slots=True)
class HermesApiResult:
    """Result wrapper for Hermes API chat-completions calls."""

    text: str
    session_id: str | None


@dataclass(slots=True, frozen=True)
class HermesDetailedHealthResult:
    """Whitelisted optional /health/detailed outcome; never stores raw payload."""

    available: bool
    status: str | None
    error_category: str | None
    auth_failed: bool = False


class HermesApiClient:
    """Client for the Hermes Agent OpenAI-compatible API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        api_key: str | None = None,
        use_ssl: bool = True,
        verify_ssl: bool = False,
        profile: str | None = None,
        model: str | None = None,
        request_timeout: int = DEFAULT_TIMEOUT,
        stream_timeout: int = DEFAULT_STREAM_TIMEOUT,
        profile_route: ProfileRouteFamily | str | None = None,
    ) -> None:
        self._session = session
        scheme = "https" if use_ssl else "http"
        normalized_host = normalize_host(host)
        url_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
        root_url = f"{scheme}://{url_host}:{port}"
        normalized_route = normalize_profile_route(profile_route)
        normalized_profile = normalize_profile(profile, normalized_route)
        self._root_url = root_url
        self._profile = normalized_profile
        self._profile_route = normalized_route
        route_prefix = (
            "profile"
            if normalized_route is ProfileRouteFamily.ADDON
            else "p"
        )
        self._base_url = (
            f"{root_url}/{route_prefix}/{normalized_profile}"
            if normalized_profile
            else root_url
        )
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL
        self._request_timeout = max(1, int(request_timeout))
        self._stream_timeout = max(self._request_timeout, int(stream_timeout))
        # ssl=False disables certificate verification (for self-signed certs)
        self._ssl: bool | None = None if not use_ssl else (None if verify_ssl else False)
        self._last_session_id: str | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def last_session_id(self) -> str | None:
        """Most recent X-Hermes-Session-Id observed from the API."""
        return self._last_session_id

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    def _classify_status_error(self, status: int) -> HermesConnectionError:
        if status in REDIRECT_STATUS_CODES:
            category = ERROR_CATEGORY_REDIRECT
        elif status >= 500:
            category = ERROR_CATEGORY_SERVER
        else:
            category = ERROR_CATEGORY_UNREACHABLE
        return HermesConnectionError(
            "Hermes API request failed",
            status_code=status,
            error_category=category,
        )

    async def _async_verify_native_profile_route(self) -> bool:
        """Verify that a named native route selects and identifies its profile."""
        if not (
            self._profile_route is ProfileRouteFamily.NATIVE
            and self._profile
        ):
            return False

        timeout = aiohttp.ClientTimeout(total=10)
        canary_url = (
            f"{self._root_url}/p/{_NATIVE_ROUTE_CANARY_PROFILE}{API_HEALTH}"
        )
        try:
            async with self._session.get(
                canary_url,
                headers={},
                timeout=timeout,
                ssl=self._ssl,
                allow_redirects=False,
            ) as resp:
                if resp.status != 404:
                    raise self._classify_status_error(resp.status)

            async with self._session.get(
                f"{self._base_url}{API_HEALTH}",
                headers={},
                timeout=timeout,
                ssl=self._ssl,
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    raise self._classify_status_error(resp.status)
                health = await resp.json()
                if not isinstance(health, dict) or (
                    health.get("status") != "ok"
                    or health.get("platform") != "hermes-agent"
                ):
                    raise HermesConnectionError(
                        "Native profile route did not identify a Hermes Agent API",
                        error_category=ERROR_CATEGORY_MALFORMED,
                    )
            return True
        except HermesConnectionError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise HermesConnectionError(
                "Cannot verify native Hermes profile routing",
                error_category=_transport_error_category(err),
            ) from err

    async def async_check_connection(self) -> bool:
        """Check Hermes identity, reachability, and Bearer authentication."""
        timeout = aiohttp.ClientTimeout(total=10)
        legacy_health_missing = False
        try:
            native_route_verified = (
                await self._async_verify_native_profile_route()
            )

            if not native_route_verified:
                async with self._session.get(
                    f"{self._base_url}{API_HEALTH}",
                    headers={},
                    timeout=timeout,
                    ssl=self._ssl,
                    allow_redirects=False,
                ) as resp:
                    if resp.status == 404:
                        # Hermes releases before 2026-03-28 do not expose
                        # /v1/health. The authenticated models probe below remains
                        # authoritative for both API identity and reachability.
                        legacy_health_missing = True
                    elif resp.status != 200:
                        raise self._classify_status_error(resp.status)
                    else:
                        health = await resp.json()
                        if not isinstance(health, dict) or (
                            health.get("status") != "ok"
                            or health.get("platform") != "hermes-agent"
                        ):
                            raise HermesConnectionError(
                                "Health endpoint did not identify a Hermes Agent API",
                                error_category=ERROR_CATEGORY_MALFORMED,
                            )

            async with self._session.get(
                f"{self._base_url}{API_MODELS}",
                headers=self._headers(),
                timeout=timeout,
                ssl=self._ssl,
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise HermesAuthError("Invalid API key")
                if resp.status != 200:
                    raise self._classify_status_error(resp.status)
                models = await resp.json()
                if not isinstance(models, dict) or not isinstance(
                    models.get("data"), list
                ):
                    raise HermesConnectionError(
                        "Models endpoint returned an invalid Hermes API response",
                        error_category=ERROR_CATEGORY_MALFORMED,
                    )
                if legacy_health_missing and not any(
                    isinstance(model, dict) and model.get("owned_by") == "hermes"
                    for model in models["data"]
                ):
                    raise HermesConnectionError(
                        "Legacy models endpoint did not identify a Hermes Agent API"
                    )
            return True
        except (HermesAuthError, HermesConnectionError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise HermesConnectionError(
                "Cannot connect to Hermes Agent",
                error_category=_transport_error_category(err),
            ) from err

    async def async_get_detailed_health(self) -> HermesDetailedHealthResult:
        """Fetch optional authenticated /health/detailed on the selected route."""
        await self._async_verify_native_profile_route()
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.get(
                f"{self._base_url}{API_HEALTH_DETAILED}",
                headers=self._headers(),
                timeout=timeout,
                ssl=self._ssl,
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    return HermesDetailedHealthResult(
                        available=False,
                        status=None,
                        error_category=ERROR_CATEGORY_AUTH,
                        auth_failed=True,
                    )
                if (
                    resp.status in REDIRECT_STATUS_CODES
                    or resp.status in UNSUPPORTED_DETAILED_STATUS_CODES
                ):
                    return HermesDetailedHealthResult(
                        available=False,
                        status=None,
                        error_category=ERROR_CATEGORY_UNSUPPORTED,
                    )
                if resp.status != 200:
                    return HermesDetailedHealthResult(
                        available=False,
                        status=None,
                        error_category=(
                            ERROR_CATEGORY_SERVER
                            if resp.status >= 500
                            else ERROR_CATEGORY_UNREACHABLE
                        ),
                    )
                payload = await resp.json()
                return _parse_detailed_health(payload)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            return HermesDetailedHealthResult(
                available=False,
                status=None,
                error_category=_transport_error_category(err),
            )

    async def async_get_models(self) -> list[dict[str, Any]]:
        """Fetch available models from /v1/models."""
        await self._async_verify_native_profile_route()
        try:
            async with self._session.get(
                f"{self._base_url}{API_MODELS}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=self._ssl,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception:
            return []

    async def async_send_message(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> HermesApiResult:
        """Send a non-streaming chat completion request."""
        await self._async_verify_native_profile_route()
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }

        try:
            async with self._session.post(
                f"{self._base_url}{API_CHAT_COMPLETIONS}",
                headers=self._headers(session_id=session_id),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout),
                ssl=self._ssl,
            ) as resp:
                if resp.status == 401:
                    raise HermesAuthError("Invalid API key")
                if resp.status >= 400:
                    body = await resp.text()
                    raise HermesApiError(
                        f"API error {resp.status}: {body[:500]}"
                    )
                data = await resp.json()
                resolved_session_id = resp.headers.get("X-Hermes-Session-Id") or session_id
                self._last_session_id = resolved_session_id
                return HermesApiResult(
                    text=self._extract_content(data),
                    session_id=resolved_session_id,
                )
        except HermesApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HermesConnectionError(
                f"Connection error: {err}"
            ) from err

    async def async_stream_message(
        self,
        messages: list[dict[str, str]],
        session_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Send a streaming chat completion request. Yields content deltas."""
        await self._async_verify_native_profile_route()
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }

        try:
            async with self._session.post(
                f"{self._base_url}{API_CHAT_COMPLETIONS}",
                headers=self._headers(session_id=session_id),
                json=payload,
                timeout=aiohttp.ClientTimeout(
                    total=self._stream_timeout,
                    sock_read=self._request_timeout,
                ),
                ssl=self._ssl,
            ) as resp:
                if resp.status == 401:
                    raise HermesAuthError("Invalid API key")
                if resp.status >= 400:
                    body = await resp.text()
                    raise HermesStreamSetupError(
                        f"API error {resp.status}: {body[:500]}"
                    )

                self._last_session_id = resp.headers.get("X-Hermes-Session-Id") or session_id

                # Parse SSE stream
                buffer = ""
                event_name = "message"
                async for chunk in resp.content.iter_any():
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")

                        if not line:
                            event_name = "message"
                            continue
                        line = line.strip()
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                            continue
                        if line == "data: [DONE]":
                            return
                        if not line.startswith("data: "):
                            continue
                        if event_name != "message":
                            continue

                        try:
                            data = json.loads(line[6:])
                            delta = (
                                data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content")
                            )
                            if isinstance(delta, str) and delta:
                                yield delta
                        except (json.JSONDecodeError, IndexError):
                            continue

        except HermesApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HermesConnectionError(
                f"Stream connection error: {err}"
            ) from err

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        """Extract the assistant message content from a chat completion response."""
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return data.get("error", {}).get("message", "(No response)")


def _transport_error_category(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return ERROR_CATEGORY_TIMEOUT
    if isinstance(err, ValueError):
        return ERROR_CATEGORY_MALFORMED
    return ERROR_CATEGORY_UNREACHABLE


def _parse_detailed_health(payload: Any) -> HermesDetailedHealthResult:
    if not isinstance(payload, dict):
        return HermesDetailedHealthResult(
            available=False,
            status=None,
            error_category=ERROR_CATEGORY_MALFORMED,
        )
    status = payload.get("status")
    if not isinstance(status, str) or status not in HEALTH_STATUSES:
        return HermesDetailedHealthResult(
            available=False,
            status=None,
            error_category=ERROR_CATEGORY_MALFORMED,
        )
    platform = payload.get("platform")
    if platform is not None and platform != "hermes-agent":
        return HermesDetailedHealthResult(
            available=False,
            status=None,
            error_category=ERROR_CATEGORY_MALFORMED,
        )
    return HermesDetailedHealthResult(
        available=True,
        status=status,
        error_category=None,
    )
