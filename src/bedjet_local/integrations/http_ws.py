"""HTTP + WebSocket adapter. REST for commands, a socket for live state.

The primary adapter (ADR-0002), shaped like chroxy so that Jarvis integration is a known
quantity rather than a new one. It contains no protocol knowledge whatsoever: it moves
JSON between :mod:`bedjet_local.api` and a socket, and it would keep working if the device
underneath were replaced.

**This server can switch on a mains-powered heater, and that governs its defaults.**

- **Loopback by default.** The listener binds ``127.0.0.1``. Reaching it from another host
  is a deliberate act, and binding anywhere else without a token is refused outright rather
  than warned about — a heater API reachable from the LAN by anyone who can guess a port is
  not a configuration mistake worth being lenient with.
- **The token goes in a header, never a query string.** URLs end up in logs, proxies and
  browser history.
- **Browser-shaped requests are rejected.** Any request carrying an ``Origin`` header is
  turned away, and the ``Host`` must be one we recognise. Together those close DNS
  rebinding, where a page on some other tab resolves an attacker's domain to 127.0.0.1 and
  posts to this port from inside the machine. Our own clients — Jarvis, curl, a script —
  send neither header, so the cost is nil.
- **Commands are REST-only.** The socket is one-way, state out. Two authenticated paths to
  a heater is one more than this needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

from ..api import (
    ApiError,
    BedJetAPI,
    CommandOutcome,
    DeviceSnapshot,
    Refused,
    Unavailable,
    Unverified,
    agent_contract,
)

log = logging.getLogger(__name__)

#: Hostnames a request's ``Host`` header may carry. Anything else is a sign the request
#: arrived via a name that resolves here rather than by being addressed here.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

API_PREFIX = "/api/v1"

#: How the API's three failure modes leave this transport. One mapping, used both to
#: translate the exceptions in ``guard`` and to describe itself in the served contract —
#: so the number an agent is told to expect is the number the middleware produces, by
#: construction. 409 refused / 503 unavailable / 502 unverified; the reasoning for keeping
#: them distinct is ADR-0004 decision 4.
FAILURE_MODE_STATUS: dict[str, int] = {
    "refused": 409,
    "unavailable": 503,
    "unverified": 502,
}

_API_KEY = web.AppKey[BedJetAPI]("bedjet_api")
_CONFIG_KEY = web.AppKey["ServerConfig"]("bedjet_config")
_SOCKETS_KEY = web.AppKey[set[web.WebSocketResponse]]("bedjet_sockets")


class ConfigurationRefused(Exception):
    """A server configuration that would be unsafe to run, refused before it listens."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """How the listener is exposed. The defaults are the safe ones.

    Args:
        host: interface to bind. Anything outside loopback requires a token.
        port: TCP port.
        token: shared secret required in ``Authorization: Bearer <token>``. ``None`` means
            no authentication, which is only permitted on loopback.
        allowed_hosts: extra ``Host`` header values to accept, for reaching the service by
            a name (a Pi's hostname, say). Loopback names are always accepted.
    """

    host: str = "127.0.0.1"
    port: int = 8787
    token: str | None = None
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.token is None and not self._is_loopback():
            raise ConfigurationRefused(
                f"refusing to bind {self.host} without a token.\n"
                f"On any interface but loopback this would let anything that can reach the "
                f"port switch on a heater. Either bind 127.0.0.1, or set a token "
                f"(BEDJET_TOKEN, or --token).\n"
                f"Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if self.token is not None and len(self.token) < 16:
            raise ConfigurationRefused(
                "the token is too short to be worth having. Use at least 16 characters — "
                "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return self.host in LOOPBACK_HOSTNAMES

    def permitted_hosts(self) -> frozenset[str]:
        return LOOPBACK_HOSTNAMES | self.allowed_hosts | {self.host}


def hostname_of(header: str) -> str:
    """The hostname in a ``Host`` header, without the port.

    Bracketed IPv6 (``[::1]:8787``) and bare IPv6 (``::1``) both have to survive this, which
    a naive ``split(":")[0]`` does not — it turns ``[::1]:8787`` into ``[``.
    """
    value = header.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value.lstrip("[")
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def host_is_permitted(header: str, config: ServerConfig) -> bool:
    """Whether a ``Host`` header is one this service should answer to.

    **An IP literal always passes, and that is not a hole.** DNS rebinding needs a *name*:
    the attacker points a hostname they control at an address inside the victim's network,
    and the browser then sends that hostname in ``Host``. A request addressed to a bare
    address had no name to rebind. Rejecting IP literals here would instead break the case
    ADR-0004 explicitly supports — a token-protected listener reached over the LAN, where
    the header is the server's own address and nobody has configured a name for it.

    A browser reaching a LAN address directly is a different attack, and it is the ``Origin``
    check and the token that answer it, not this one.
    """
    hostname = hostname_of(header)
    if not hostname:
        return True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return True
    permitted = {hostname_of(entry).lower() for entry in config.permitted_hosts()}
    return hostname.lower() in permitted


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _error(kind: str, detail: str, status: int) -> web.Response:
    return _json({"error": kind, "detail": detail}, status=status)


@web.middleware
async def guard(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Authentication and the two browser checks, in front of everything but ``/healthz``.

    ``/healthz`` is exempt because a process supervisor has to be able to ask "are you
    alive?" without holding a credential, and the answer says nothing about the device.
    """
    config = request.app[_CONFIG_KEY]

    if request.path != "/healthz":
        host = request.headers.get("Host") or ""
        if not host_is_permitted(host, config):
            # DNS rebinding: the request reached this port by resolving somebody else's
            # name to 127.0.0.1. A request genuinely addressed to us names us.
            return _error(
                "forbidden",
                f"unrecognised Host header {host!r}. If this service should answer to that "
                f"name, add it to allowed_hosts.",
                403,
            )
        if "Origin" in request.headers:
            # Only browsers send Origin. No browser has business commanding this heater,
            # and a page that tries is the rebinding attack arriving from the other side.
            return _error(
                "forbidden",
                "requests carrying an Origin header are refused: this is a local machine "
                "API, not a web origin. Use a client that is not a browser.",
                403,
            )
        if config.token is not None:
            supplied = request.headers.get("Authorization", "")
            prefix = "Bearer "
            token = supplied[len(prefix) :] if supplied.startswith(prefix) else ""
            # Constant-time: the comparison is against a secret, and a timing oracle on a
            # local socket is cheap to build.
            if not secrets.compare_digest(token, config.token):
                return _error(
                    "unauthorized",
                    "supply the token as `Authorization: Bearer <token>`. It is not "
                    "accepted as a query parameter — URLs end up in logs.",
                    401,
                )

    try:
        return await handler(request)
    except Unavailable as exc:
        return _error("unavailable", str(exc), FAILURE_MODE_STATUS["unavailable"])
    except Unverified as exc:
        # 502, deliberately: something downstream of us did not answer for itself. This is
        # the response an operator must not be able to confuse with a refusal.
        return _error("unverified", str(exc), FAILURE_MODE_STATUS["unverified"])
    except Refused as exc:
        return _error("refused", str(exc), FAILURE_MODE_STATUS["refused"])
    except ApiError as exc:  # pragma: no cover - defensive
        return _error("error", str(exc), 500)


async def _body(request: web.Request) -> dict[str, Any]:
    """Parse a JSON object body. An empty body is an empty object, not an error."""
    if not request.can_read_body:
        return {}
    try:
        parsed = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": f"malformed JSON: {exc}"}),
            content_type="application/json",
        ) from exc
    if not isinstance(parsed, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": "expected a JSON object"}),
            content_type="application/json",
        )
    return parsed


def _bool(body: dict[str, Any], key: str, default: bool = False) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": f"{key} must be true or false"}),
            content_type="application/json",
        )
    return value


def _number(body: dict[str, Any], key: str) -> float | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": f"{key} must be a number"}),
            content_type="application/json",
        )
    return float(value)


def _outcome(outcome: CommandOutcome) -> web.Response:
    return _json(outcome.to_dict())


# ── handlers ────────────────────────────────────────────────────────────────────────────


async def handle_health(_request: web.Request) -> web.Response:
    """Liveness only. Deliberately says nothing about the device: this is the one route
    that answers without a credential."""
    return _json({"status": "ok"})


async def handle_contract(_request: web.Request) -> web.Response:
    """The agent-facing contract (ADR-0005 decision 1), decorated with this transport.

    The core document comes from ``api/contract.py`` and knows nothing about HTTP; the
    ``http`` section added here is this adapter describing itself — the same split that
    keeps :class:`Refused` in the API and 409 in this file. The route table is ``ROUTES``,
    the one ``build_app`` registers from, so the served description cannot drift from the
    served surface.
    """
    payload = agent_contract()
    payload["http"] = {
        "auth": (
            "When a token is configured, every route except /healthz requires "
            "`Authorization: Bearer <token>`. The token is never accepted as a query "
            "parameter. Requests carrying an Origin header, or an unrecognised Host, "
            "are refused."
        ),
        "routes": {
            operation: {"method": method, "path": path} for operation, method, path, _ in ROUTES
        },
        "failure_mode_status": dict(FAILURE_MODE_STATUS),
        "other_status": {
            "200": "success, including a satisfied request that wrote nothing",
            "400": "invalid_request — malformed body; nothing was sent",
            "401": "unauthorized — token missing or wrong",
            "403": "forbidden — Origin header present, or unrecognised Host",
        },
        "notes": (
            "stream_state is a WebSocket: connect to its path with a WebSocket client; "
            'every message is {"type": "state", "state": {...}} with the same body '
            "get_state returns. Commands are REST-only."
        ),
    }
    return _json(payload)


async def handle_state(request: web.Request) -> web.Response:
    return _json(request.app[_API_KEY].snapshot().to_dict())


async def handle_capabilities(request: web.Request) -> web.Response:
    return _json(request.app[_API_KEY].capabilities().to_dict())


async def handle_off(request: web.Request) -> web.Response:
    body = await _body(request)
    return _outcome(await request.app[_API_KEY].turn_off(dry_run=_bool(body, "dry_run")))


async def handle_mode(request: web.Request) -> web.Response:
    body = await _body(request)
    mode = body.get("mode")
    if not isinstance(mode, str):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": "mode must be a string"}),
            content_type="application/json",
        )
    api = request.app[_API_KEY]
    return _outcome(await api.set_mode(mode, dry_run=_bool(body, "dry_run")))


async def handle_temperature(request: web.Request) -> web.Response:
    body = await _body(request)
    api = request.app[_API_KEY]
    return _outcome(
        await api.set_temperature(
            celsius=_number(body, "celsius"),
            fahrenheit=_number(body, "fahrenheit"),
            dry_run=_bool(body, "dry_run"),
        )
    )


async def handle_fan(request: web.Request) -> web.Response:
    body = await _body(request)
    percent = _number(body, "percent")
    if percent is None:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": "percent is required"}),
            content_type="application/json",
        )
    api = request.app[_API_KEY]
    return _outcome(await api.set_fan(int(percent), dry_run=_bool(body, "dry_run")))


async def handle_yield(request: web.Request) -> web.Response:
    """Hand the device's single BLE slot back to its owner for a while."""
    body = await _body(request)
    seconds = _number(body, "seconds")
    if seconds is None:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request", "detail": "seconds is required"}),
            content_type="application/json",
        )
    snapshot = await request.app[_API_KEY].yield_link(seconds)
    return _json(snapshot.to_dict())


async def handle_resume(request: web.Request) -> web.Response:
    return _json((await request.app[_API_KEY].resume_link()).to_dict())


async def handle_ws(request: web.Request) -> web.StreamResponse:
    """Live state, pushed. One-way on purpose — commands go through REST.

    Every message is ``{"type": "state", "state": {...}}`` with the same body ``GET /state``
    returns, so a consumer parses one shape rather than two.
    """
    socket = web.WebSocketResponse(heartbeat=30.0)
    await socket.prepare(request)

    api = request.app[_API_KEY]
    sockets = request.app[_SOCKETS_KEY]
    sockets.add(socket)

    # Bounded, and the newest snapshot wins when it overflows. The producer is the BLE
    # notification path — it publishes several times a second and must never wait on a
    # socket — and state is a snapshot rather than a log, so a consumer too slow to keep up
    # wants the latest reading, not a backlog of stale ones.
    pending: asyncio.Queue[DeviceSnapshot] = asyncio.Queue(maxsize=16)

    def on_snapshot(snapshot: DeviceSnapshot) -> None:
        while True:
            try:
                pending.put_nowait(snapshot)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    pending.get_nowait()

    unsubscribe = api.subscribe(on_snapshot)
    on_snapshot(api.snapshot())
    writer = asyncio.create_task(_pump(socket, pending))
    try:
        async for message in socket:
            if message.type is WSMsgType.ERROR:  # pragma: no cover - transport-level
                log.warning("websocket error: %s", socket.exception())
                break
            # Nothing else is read. The socket is an output, and saying so by ignoring
            # input is better than accepting commands on a second path.
    finally:
        unsubscribe()
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
        sockets.discard(socket)
        await socket.close()
    return socket


async def _pump(socket: web.WebSocketResponse, pending: asyncio.Queue[DeviceSnapshot]) -> None:
    """Drain snapshots to one socket, in order, until cancelled."""
    while True:
        snapshot = await pending.get()
        if socket.closed:
            return
        try:
            await socket.send_json({"type": "state", "state": snapshot.to_dict()})
        except (ConnectionResetError, RuntimeError):  # pragma: no cover - timing-dependent
            log.debug("dropped a state push to a closing socket")
            return


#: Every route this adapter serves, named by the operation it implements in the contract.
#: ``build_app`` registers from this table and ``handle_contract`` describes it, so the
#: description and the surface cannot disagree; the parity test in
#: ``tests/integration/test_http_ws.py`` holds this list and the contract's operation
#: list to exactly each other. Add a route here without a contract entry — or the other
#: way round — and that test names the missing half.
ROUTES: tuple[tuple[str, str, str, Callable[[web.Request], Awaitable[web.StreamResponse]]], ...] = (
    ("health", "GET", "/healthz", handle_health),
    ("get_contract", "GET", f"{API_PREFIX}/contract", handle_contract),
    ("get_state", "GET", f"{API_PREFIX}/state", handle_state),
    ("get_capabilities", "GET", f"{API_PREFIX}/capabilities", handle_capabilities),
    ("stream_state", "GET", f"{API_PREFIX}/ws", handle_ws),
    ("turn_off", "POST", f"{API_PREFIX}/command/off", handle_off),
    ("set_mode", "POST", f"{API_PREFIX}/command/mode", handle_mode),
    ("set_temperature", "POST", f"{API_PREFIX}/command/temperature", handle_temperature),
    ("set_fan", "POST", f"{API_PREFIX}/command/fan", handle_fan),
    ("yield_link", "POST", f"{API_PREFIX}/link/yield", handle_yield),
    ("resume_link", "POST", f"{API_PREFIX}/link/resume", handle_resume),
)


def build_app(api: BedJetAPI, config: ServerConfig | None = None) -> web.Application:
    """Assemble the application. Separate from serving so tests can drive it directly."""
    app = web.Application(middlewares=[guard])
    app[_API_KEY] = api
    app[_CONFIG_KEY] = config or ServerConfig()
    app[_SOCKETS_KEY] = set()

    app.add_routes([web.route(method, path, handler) for _, method, path, handler in ROUTES])
    app.on_shutdown.append(_close_sockets)
    return app


async def _close_sockets(app: web.Application) -> None:
    """Close every socket before the loop stops, so clients see a clean close rather than
    a connection that simply stops answering."""
    sockets: set[web.WebSocketResponse] = app[_SOCKETS_KEY]
    for socket in list(sockets):
        await socket.close(code=1001, message=b"server shutting down")


async def serve(api: BedJetAPI, config: ServerConfig) -> web.AppRunner:
    """Start listening. Returns the runner so the caller owns the shutdown."""
    runner = web.AppRunner(build_app(api, config), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()
    log.info(
        "listening on http://%s:%d%s (auth: %s)",
        config.host,
        config.port,
        API_PREFIX,
        "token" if config.token else "none — loopback only",
    )
    return runner
