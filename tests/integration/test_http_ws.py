"""The HTTP + WebSocket adapter, driven end to end over a real loopback socket.

Two things are being tested, and the second matters more than the first. One: the adapter
translates faithfully — the three API failure modes have to arrive as three different status
codes, because a client that cannot tell "nothing was sent" from "we do not know what
happened" will retry the wrong one. Two: **this server can switch on a heater**, so the
tests that assert it refuses to expose itself are load-bearing, not box-ticking.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bedjet_local.api import CONTRACT_VERSION, BedJetAPI, agent_contract
from bedjet_local.integrations.http_ws import (
    FAILURE_MODE_STATUS,
    ROUTES,
    ConfigurationRefused,
    ServerConfig,
    build_app,
    host_is_permitted,
    hostname_of,
)
from bedjet_local.protocol.constants import COMMAND_UUID, STATUS_UUID, StatusMode
from bedjet_local.service.session import DeviceSession
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)

TOKEN = "a-token-long-enough-to-be-worth-having"


async def _client(
    transport: MockTransport,
    *,
    config: ServerConfig | None = None,
    initial: bytes | None = RUNNING,
) -> tuple[TestClient[web.Request, web.Application], DeviceSession]:
    session = DeviceSession(transport, "mock", settle_timeout=0.3, supervise_interval=0.02)
    await session.start()
    if initial is not None:
        transport.emit(STATUS_UUID, initial)
    app = build_app(BedJetAPI(session), config or ServerConfig())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, session


# ── exposure ────────────────────────────────────────────────────────────────────────────


def test_binding_beyond_loopback_without_a_token_is_refused_not_warned() -> None:
    """The failure mode this prevents is a heater API answering anything on the LAN that
    can guess a port. A warning would be read once and then scrolled past."""
    with pytest.raises(ConfigurationRefused, match="without a token"):
        ServerConfig(host="0.0.0.0")

    ServerConfig(host="0.0.0.0", token=TOKEN)  # with a token, fine


def test_a_token_too_short_to_matter_is_refused() -> None:
    with pytest.raises(ConfigurationRefused, match="too short"):
        ServerConfig(host="0.0.0.0", token="hunter2")


def test_loopback_addresses_are_recognised_in_both_families() -> None:
    ServerConfig(host="127.0.0.1")
    ServerConfig(host="::1")
    ServerConfig(host="localhost")


async def test_the_token_is_required_and_only_in_a_header() -> None:
    transport = MockTransport()
    client, session = await _client(transport, config=ServerConfig(token=TOKEN))
    try:
        assert (await client.get("/api/v1/state")).status == 401

        # A query parameter must NOT work: URLs end up in logs, proxies and history.
        assert (await client.get(f"/api/v1/state?token={TOKEN}")).status == 401

        response = await client.get("/api/v1/state", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status == 200
    finally:
        await client.close()
        await session.stop()


async def test_health_answers_without_a_credential_and_says_nothing_about_the_device() -> None:
    transport = MockTransport()
    client, session = await _client(transport, config=ServerConfig(token=TOKEN))
    try:
        response = await client.get("/healthz")
        assert response.status == 200
        assert await response.json() == {"status": "ok"}
    finally:
        await client.close()
        await session.stop()


async def test_a_request_carrying_an_origin_is_refused() -> None:
    """Only browsers send Origin, and no browser has business commanding this heater. This
    is the outbound half of the DNS-rebinding defence."""
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        response = await client.get("/api/v1/state", headers={"Origin": "https://example.invalid"})
        assert response.status == 403
        assert (await response.json())["error"] == "forbidden"
    finally:
        await client.close()
        await session.stop()


def test_an_ip_literal_host_is_accepted_including_the_lan_case() -> None:
    """The bug this covers: bound to 0.0.0.0 with a token, a LAN client sends the server's
    own address in Host — and rejecting it would break the exact deployment ADR-0004
    supports. An IP literal cannot be a rebinding target: rebinding needs a *name*."""
    config = ServerConfig(host="0.0.0.0", token=TOKEN)
    assert host_is_permitted("192.168.1.50:8787", config) is True
    assert host_is_permitted("[fe80::1]:8787", config) is True
    assert host_is_permitted("attacker.invalid:8787", config) is False


def test_a_named_host_passes_only_when_it_was_allowed() -> None:
    config = ServerConfig(host="0.0.0.0", token=TOKEN, allowed_hosts=frozenset({"pi.local"}))
    assert host_is_permitted("pi.local:8787", config) is True
    assert host_is_permitted("PI.LOCAL", config) is True, "hostnames are case-insensitive"
    assert host_is_permitted("evil.invalid", config) is False


def test_ipv6_host_headers_survive_the_port_split() -> None:
    """``split(":")[0]`` turns ``[::1]:8787`` into ``[`` and would reject loopback itself."""
    assert hostname_of("[::1]:8787") == "::1"
    assert hostname_of("::1") == "::1"
    assert hostname_of("localhost:8787") == "localhost"
    assert hostname_of("localhost") == "localhost"


async def test_an_unrecognised_host_header_is_refused() -> None:
    """DNS rebinding arrives as a request that reached this port by resolving somebody
    else's name to 127.0.0.1. A request genuinely addressed to us names us."""
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        response = await client.get("/api/v1/state", headers={"Host": "attacker.invalid"})
        assert response.status == 403
    finally:
        await client.close()
        await session.stop()


# ── reading ─────────────────────────────────────────────────────────────────────────────


async def test_state_is_the_snapshot_verbatim() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        body = await (await client.get("/api/v1/state")).json()
        assert body["mode"] == "cool"
        assert body["available"] is True
        assert body["min_target_c"] == 19.0
    finally:
        await client.close()
        await session.stop()


async def test_capabilities_do_not_advertise_a_locked_mode() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        body = await (await client.get("/api/v1/capabilities")).json()
        assert "turbo" not in body["modes"]
    finally:
        await client.close()
        await session.stop()


# ── commanding ──────────────────────────────────────────────────────────────────────────


async def test_a_verified_command_returns_what_went_on_the_wire() -> None:
    transport = MockTransport()
    client, session = await _client(transport)

    async def respond() -> None:
        await asyncio.sleep(0.05)
        transport.emit(STATUS_UUID, STANDBY)

    task = asyncio.create_task(respond())
    try:
        body = await (await client.post("/api/v1/command/off")).json()
        await task
        assert body["ok"] is True
        assert body["changed"] is True
        assert body["sent"] == "01 01"
        assert transport.writes == [(COMMAND_UUID, bytes([0x01, 0x01]))]
    finally:
        await client.close()
        await session.stop()


async def test_a_satisfied_request_is_a_200_not_an_error() -> None:
    """An automation that switches the heater off nightly must not page somebody because it
    was already off."""
    transport = MockTransport()
    client, session = await _client(transport, initial=STANDBY)
    try:
        response = await client.post("/api/v1/command/off")
        assert response.status == 200
        body = await response.json()
        assert body["ok"] is True and body["changed"] is False
        assert body["sent"] is None
        assert transport.writes == []
    finally:
        await client.close()
        await session.stop()


async def test_unverified_is_502_and_refused_is_409() -> None:
    """The distinction the whole error taxonomy exists for. 409: nothing was sent, and
    retrying unchanged will refuse again. 502: bytes went out and the device did not
    visibly obey — which may mean it obeyed and we lost the confirmation. A client must
    never respond to those two the same way."""
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        refused = await client.post("/api/v1/command/mode", json={"mode": "turbo"})
        assert refused.status == 409
        assert (await refused.json())["error"] == "refused"
        assert transport.writes == [], "a refusal must not reach the device"

        unverified = await client.post("/api/v1/command/off")
        assert unverified.status == 502
        assert (await unverified.json())["error"] == "unverified"
        assert transport.writes, "the bytes did go out — that is what makes it unverified"
    finally:
        await client.close()
        await session.stop()


async def test_commands_against_a_yielded_link_are_503() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        yielded = await client.post("/api/v1/link/yield", json={"seconds": 60})
        assert yielded.status == 200
        assert (await yielded.json())["link"] == "yielded"

        response = await client.post("/api/v1/command/off")
        assert response.status == 503
        assert (await response.json())["error"] == "unavailable"
        assert transport.writes == []
    finally:
        await client.close()
        await session.stop()


async def test_malformed_requests_are_400_and_send_nothing() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        assert (await client.post("/api/v1/command/mode", json={})).status == 400
        assert (await client.post("/api/v1/command/fan", json={"percent": "fast"})).status == 400
        assert (
            await client.post(
                "/api/v1/command/off",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
        ).status == 400
        assert transport.writes == []
    finally:
        await client.close()
        await session.stop()


async def test_a_dry_run_over_http_writes_nothing() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        body = await (await client.post("/api/v1/command/off", json={"dry_run": True})).json()
        assert body["dry_run"] is True
        assert body["sent"] is None
        assert transport.writes == []
    finally:
        await client.close()
        await session.stop()


# ── the socket ──────────────────────────────────────────────────────────────────────────


async def test_the_websocket_pushes_the_current_state_then_changes() -> None:
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        async with client.ws_connect("/api/v1/ws") as socket:
            first = await asyncio.wait_for(socket.receive_json(), timeout=2.0)
            assert first["type"] == "state"
            assert first["state"]["mode"] == "cool"

            transport.emit(STATUS_UUID, STANDBY)
            while True:
                message = await asyncio.wait_for(socket.receive_json(), timeout=2.0)
                if message["state"]["mode"] == "standby":
                    break
    finally:
        await client.close()
        await session.stop()


async def test_the_websocket_requires_the_token_too() -> None:
    transport = MockTransport()
    client, session = await _client(transport, config=ServerConfig(token=TOKEN))
    try:
        with pytest.raises(Exception, match="401"):
            async with client.ws_connect("/api/v1/ws"):
                pass
    finally:
        await client.close()
        await session.stop()


# ── the agent-facing contract (ADR-0005 decision 1) ─────────────────────────────────────


def test_the_contract_names_exactly_the_routes_this_adapter_serves() -> None:
    """The parity test both module docstrings promise. ``build_app`` registers from
    ``ROUTES`` and ``handle_contract`` describes ``ROUTES``, so within this adapter the
    description cannot drift — what can drift is the api-layer document itself. Holding
    the two name sets to exact equality means a route added without a contract entry, or
    an operation described but never served, fails here by name."""
    operations = {operation["name"] for operation in agent_contract()["operations"]}
    served = {name for name, _method, _path, _handler in ROUTES}
    assert operations == served


async def test_the_served_contract_describes_this_transport_truthfully() -> None:
    """What an agent fetches must match what this server does: the advertised status
    numbers are the ones the middleware produces (same mapping, by construction — pinned
    here against the doctrine numbers), and every operation resolves to a callable route."""
    transport = MockTransport()
    client, session = await _client(transport)
    try:
        response = await client.get("/api/v1/contract")
        assert response.status == 200
        body = await response.json()

        assert body["contract_version"] == CONTRACT_VERSION
        assert body["http"]["failure_mode_status"] == {
            "refused": 409,
            "unavailable": 503,
            "unverified": 502,
        }
        assert body["http"]["failure_mode_status"] == FAILURE_MODE_STATUS

        routes = body["http"]["routes"]
        assert set(routes) == {op["name"] for op in body["operations"]}
        assert routes["set_mode"] == {"method": "POST", "path": "/api/v1/command/mode"}
        assert routes["health"] == {"method": "GET", "path": "/healthz"}
    finally:
        await client.close()
        await session.stop()


async def test_the_contract_requires_the_token() -> None:
    """The contract describes the control surface of a heater. ``/healthz`` stays the one
    free route; a description of what the service can be told to do is not liveness."""
    transport = MockTransport()
    client, session = await _client(transport, config=ServerConfig(token=TOKEN))
    try:
        assert (await client.get("/api/v1/contract")).status == 401
        response = await client.get(
            "/api/v1/contract", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status == 200
    finally:
        await client.close()
        await session.stop()
