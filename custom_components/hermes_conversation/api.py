"""HTTP client for the Hermes Agent API."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import aiohttp

from .const import (
    API_CHAT_COMPLETIONS,
    API_HEALTH,
    API_MODELS,
    DEFAULT_MODEL,
    DEFAULT_STREAM_TIMEOUT,
    DEFAULT_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class HermesApiError(Exception):
    """Base exception for Hermes API errors."""


class HermesConnectionError(HermesApiError):
    """Cannot reach the Hermes Agent API."""


class HermesAuthError(HermesApiError):
    """Authentication failed."""


@dataclass(slots=True)
class HermesApiResult:
    """Result wrapper for Hermes API chat-completions calls."""

    text: str
    session_id: str | None


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
        model: str | None = None,
        request_timeout: int = DEFAULT_TIMEOUT,
        stream_timeout: int = DEFAULT_STREAM_TIMEOUT,
    ) -> None:
        self._session = session
        scheme = "https" if use_ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"
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

    async def async_check_connection(self) -> bool:
        """Check if the Hermes Agent API is reachable and auth is valid."""
        try:
            async with self._session.get(
                f"{self._base_url}{API_HEALTH}",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=self._ssl,
            ) as resp:
                if resp.status == 401:
                    raise HermesAuthError("Invalid API key")
                if resp.status == 403:
                    raise HermesAuthError("Access denied")
                return resp.status < 400
        except HermesAuthError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HermesConnectionError(
                f"Cannot connect to Hermes Agent at {self._base_url}: {err}"
            ) from err

    async def async_get_models(self) -> list[dict[str, Any]]:
        """Fetch available models from /v1/models."""
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
                    raise HermesApiError(
                        f"API error {resp.status}: {body[:500]}"
                    )

                self._last_session_id = resp.headers.get("X-Hermes-Session-Id") or session_id

                # Parse SSE stream
                buffer = ""
                async for chunk in resp.content.iter_any():
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()

                        if not line:
                            continue
                        if line == "data: [DONE]":
                            return
                        if not line.startswith("data: "):
                            continue

                        try:
                            data = json.loads(line[6:])
                            delta = (
                                data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content")
                            )
                            if delta:
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
