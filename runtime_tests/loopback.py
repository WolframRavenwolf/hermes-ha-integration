"""Loopback aiohttp Hermes Agent API for runtime tests.

Fakes only the external HTTP boundary. Responses are labeled fixtures, not
Home Assistant stubs. Bind is 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

API_KEY = "runtime-test-key"

HEALTH_OK = ("health.ok", 200, {"status": "ok", "platform": "hermes-agent"})
HEALTH_LEGACY_404 = ("health.legacy_404", 404, {"detail": "Not Found"})
HEALTH_MALFORMED = ("health.malformed", 200, "{not-json", "text/plain")
HEALTH_UNSUPPORTED = (
    "health.unsupported",
    200,
    {"status": "ok", "platform": "not-hermes"},
)

MODELS_OK = (
    "models.ok",
    200,
    {"data": [{"id": "hermes-agent", "owned_by": "hermes"}]},
)
MODELS_UNAUTH = ("models.unauth", 401, {"error": "invalid_api_key"})

_DETAILED_OK_BODY = {
    "status": "ok",
    "platform": "hermes-agent",
    "readiness": {
        "status": "ok",
        "checks": {
            "state_db": {"status": "ok"},
            "config": {"status": "ok"},
            "model": {"status": "ok"},
            "disk": {"status": "ok", "used_percent": 10.0},
            "gateway": {"status": "ok", "state": "running"},
            "background_queues": {"status": "ok", "active_api_runs": 0},
        },
    },
}
_DETAILED_DEGRADED_BODY = {
    "status": "degraded",
    "platform": "hermes-agent",
    "readiness": {
        "status": "degraded",
        "checks": {
            "state_db": {"status": "ok"},
            "config": {"status": "ok"},
            "model": {"status": "ok"},
            "disk": {"status": "degraded", "used_percent": 95.0},
            "gateway": {"status": "ok", "state": "running"},
            "background_queues": {"status": "ok", "active_api_runs": 0},
        },
    },
}

DETAILED_OK = ("detailed.ok", 200, _DETAILED_OK_BODY)
DETAILED_DEGRADED = ("detailed.degraded", 200, _DETAILED_DEGRADED_BODY)
DETAILED_MALFORMED = ("detailed.malformed", 200, "{", "text/plain")
DETAILED_UNSUPPORTED = (
    "detailed.unsupported",
    200,
    {"status": "yellow", "readiness": "not-an-object"},
)
DETAILED_404 = ("detailed.404", 404, {"detail": "Not Found"})


def _unpack(fixture: tuple) -> tuple[str, int, Any, str | None]:
    if len(fixture) == 4:
        label, status, body, content_type = fixture
        return label, status, body, content_type
    label, status, body = fixture
    return label, status, body, None


@dataclass
class RouteBundle:
    """Labeled fixtures for one API root (host root, /profile/x, or /p/x)."""

    health: tuple = HEALTH_OK
    models: tuple = MODELS_OK
    detailed: tuple = DETAILED_OK
    api_key: str | None = API_KEY


@dataclass
class RecordedRequest:
    method: str
    path: str
    authorization: str | None
    fixture_label: str | None


@dataclass
class HermesLoopbackApi:
    """Real TCP aiohttp server implementing the Hermes public health/models API."""

    host: str = "127.0.0.1"
    port: int = 0
    requests: list[RecordedRequest] = field(default_factory=list)
    root: RouteBundle = field(default_factory=RouteBundle)
    addon: dict[str, RouteBundle] = field(default_factory=dict)
    native: dict[str, RouteBundle] = field(default_factory=dict)
    native_canary_status: int = 404
    block_requests: asyncio.Event | None = None
    request_started: asyncio.Event = field(default_factory=asyncio.Event)
    _runner: web.AppRunner | None = field(default=None, init=False, repr=False)
    _site: web.TCPSite | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def request_paths(self) -> list[str]:
        return [item.path for item in self.requests]

    def count_paths(self, *substrings: str) -> int:
        return sum(
            1
            for path in self.request_paths()
            if any(token in path for token in substrings)
        )

    async def start(self) -> None:
        app = web.Application(middlewares=[self._capture])
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if not sockets:
            raise RuntimeError("Hermes loopback API failed to bind 127.0.0.1")
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def go_offline(self) -> None:
        """Drop the TCP listener (connection refused) while remembering the port."""
        port = self.port
        await self.stop()
        self.port = port

    async def go_online(self) -> None:
        if self._site is not None:
            return
        await self.start()

    @web.middleware
    async def _capture(self, request: web.Request, handler):
        return await handler(request)

    def _bundle_for(self, path: str) -> tuple[RouteBundle | None, str, bool]:
        """Return (bundle, remainder, is_native_canary)."""
        if path == "/p/hermes/v1/health" or path.startswith("/p/hermes/"):
            return None, path[len("/p/hermes") :], True
        if path.startswith("/p/"):
            rest = path[3:]
            name, _, remainder = rest.partition("/")
            return self.native.get(name), "/" + remainder, False
        if path.startswith("/profile/"):
            rest = path[len("/profile/") :]
            name, _, remainder = rest.partition("/")
            return self.addon.get(name), "/" + remainder, False
        return self.root, path, False

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        bundle, remainder, is_canary = self._bundle_for(path)
        fixture = None
        if is_canary and remainder in {"/v1/health", "v1/health", "/v1/health/"}:
            fixture = ("native.canary", self.native_canary_status, {"detail": "Not Found"})
        elif bundle is not None:
            if remainder in {"/v1/health", "/v1/health/"}:
                fixture = bundle.health
            elif remainder in {"/v1/models", "/v1/models/"}:
                fixture = bundle.models
            elif remainder in {"/health/detailed", "/health/detailed/"}:
                fixture = bundle.detailed

        label = _unpack(fixture)[0] if fixture is not None else None
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=path,
                authorization=request.headers.get("Authorization"),
                fixture_label=label,
            )
        )

        self.request_started.set()
        if self.block_requests is not None:
            await self.block_requests.wait()

        if fixture is None:
            return web.json_response({"detail": "Not Found"}, status=404)

        label, status, body, content_type = _unpack(fixture)
        if remainder in {"/v1/models", "/v1/models/", "/health/detailed", "/health/detailed/"}:
            expected = bundle.api_key if bundle is not None else None
            header = request.headers.get("Authorization")
            if expected and header != f"Bearer {expected}":
                return web.json_response({"error": "unauthorized"}, status=401)

        if isinstance(body, (dict, list)):
            return web.json_response(body, status=status)
        return web.Response(
            text=str(body),
            status=status,
            content_type=content_type or "text/plain",
        )
