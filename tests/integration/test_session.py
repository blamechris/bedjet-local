"""The connection lease, against the mock transport. No adapter, no device, no network.

A daemon's failure modes are not a command's failure modes, so these tests are about the
things that only go wrong when you hold a link for hours: a drop that must be recovered
from, a yield that must be honoured, and a reading that must not be allowed to look fresher
than it is.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from bedjet_local.device.state import Power
from bedjet_local.protocol.constants import STATUS_UUID, StatusMode
from bedjet_local.service.session import DeviceSession, LinkState
from bedjet_local.transport.base import TransportError
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)


async def _started(transport: MockTransport, **kwargs: float) -> DeviceSession:
    session = DeviceSession(transport, "mock", supervise_interval=0.02, **kwargs)
    await session.start()
    return session


async def test_start_connects_and_publishes_readings() -> None:
    transport = MockTransport()
    session = await _started(transport)
    try:
        assert session.link is LinkState.CONNECTED
        transport.emit(STATUS_UUID, RUNNING)

        snapshot = session.snapshot()
        assert snapshot.available is True
        assert snapshot.reading is not None
        assert snapshot.reading.power is Power.ON
        assert snapshot.stale is False
    finally:
        await session.stop()


async def test_a_failed_first_connection_is_raised_not_retried_silently() -> None:
    """A daemon that cannot reach the device at boot must say so, with the transport's
    own diagnosis. Disappearing into a retry loop hides a wrong address forever."""
    transport = MockTransport(fail_connects=1)
    session = DeviceSession(transport, "mock", supervise_interval=0.02)

    with pytest.raises(TransportError):
        await session.start()
    assert session.link is LinkState.STOPPED


async def test_a_dropped_link_reconnects() -> None:
    transport = MockTransport()
    session = await _started(transport, backoff_initial=0.01, backoff_max=0.02)
    try:
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.link is LinkState.CONNECTED and transport.connect_attempts > 1:
                break
        assert session.link is LinkState.CONNECTED
        assert transport.connect_attempts > 1
    finally:
        await session.stop()


async def test_a_dropped_link_does_not_change_what_we_believe_about_the_heater() -> None:
    """Availability and power are independent. A lost radio link is not evidence the unit
    switched off, and the last reading must survive with its age rather than be reinterpreted."""
    transport = MockTransport()
    session = await _started(transport, backoff_initial=10.0)
    try:
        transport.emit(STATUS_UUID, RUNNING)
        transport.refuse_next_connects(50)
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.link is LinkState.LOST:
                break

        snapshot = session.snapshot()
        assert snapshot.available is False, "the link is down"
        assert snapshot.reading is not None
        assert snapshot.reading.power is Power.ON, "we last saw it running, and we still did"
        assert snapshot.reading_age_s is not None
    finally:
        await session.stop()


async def test_a_refused_reconnect_is_a_warning_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The severity contract behind #5, asserted on the loop rather than on the transport.

    The daemon's premise is running unattended overnight, so what an operator (or a log
    alert) sees for an ordinary adapter cycle decides whether ERROR still means anything at
    3am. A routine reconnect failure emitting ERROR-with-traceback is what trains people to
    ignore the level, and then a genuine fault arrives looking identical.

    ``TransportError`` is the whole vocabulary this loop classifies on, which is why the
    transport layer must translate every bleak type into it — see
    ``tests/unit/test_transport_ble.py``.
    """
    caplog.set_level(logging.DEBUG, logger="bedjet_local.service.session")
    transport = MockTransport()
    session = await _started(transport, backoff_initial=0.01, backoff_max=0.02)
    try:
        transport.refuse_next_connects(50)
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any("reconnect failed" in r.message for r in caplog.records):
                break

        failures = [r for r in caplog.records if "reconnect failed" in r.message]
        assert failures, "the refused reconnect should have been reported"
        for record in failures:
            assert record.levelname == "WARNING"
            assert record.exc_info is None, "a routine refusal must not carry a stack trace"

        unexpected = [r for r in caplog.records if "unexpected error reconnecting" in r.message]
        assert not unexpected, (
            f"a TransportError reached the bare `except Exception` branch: {unexpected}. "
            f"That branch is for genuine surprises, and everything it logs is ERROR."
        )
    finally:
        await session.stop()


async def test_adapter_power_on_cuts_the_backoff_short() -> None:
    """#20, RL-024 item 4: the adapter came back 22 s before a mid-backoff daemon noticed.
    Recovery latency must be bounded by device availability, not by where in a sleep the
    power-on happened to land. The backoff here is 30 s — far beyond this test's budget —
    so reconnecting at all proves the sleep was cut."""
    transport = MockTransport()
    session = await _started(transport, backoff_initial=30.0, backoff_max=60.0)
    try:
        transport.refuse_next_connects(1)
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if transport.connect_attempts >= 2:
                break
        assert transport.connect_attempts >= 2, "the first reconnect attempt should have run"
        # Via the snapshot rather than `session.link is LinkState.LOST`: mypy narrows the
        # property to that literal and then rejects the CONNECTED comparisons below as
        # non-overlapping, not knowing the supervisor mutates it.
        assert session.snapshot().available is False

        transport.power_on_adapter()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.link is LinkState.CONNECTED:
                break
        assert session.link is LinkState.CONNECTED, (
            "an adapter power-on during backoff should have triggered an immediate reconnect"
        )
    finally:
        await session.stop()


async def test_a_stale_power_on_does_not_defeat_the_backoff() -> None:
    """The event is re-armed before every attempt: a power-on from before the drop is not
    a licence to hammer reconnects after it. Each edge buys at most one early try."""
    transport = MockTransport()
    session = await _started(transport, backoff_initial=30.0, backoff_max=60.0)
    try:
        transport.power_on_adapter()  # stale: the adapter bounced while the link was up
        transport.refuse_next_connects(50)
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if transport.connect_attempts >= 2:
                break
        assert transport.connect_attempts == 2

        # The stale edge was cleared before that attempt, so the 30 s backoff must hold.
        await asyncio.sleep(0.2)
        assert transport.connect_attempts == 2, "a stale power-on must not grant extra tries"
        assert session.link is LinkState.LOST
    finally:
        await session.stop()


async def test_a_power_on_during_a_yield_does_not_reclaim_the_link() -> None:
    """The yield is the owner's. An adapter event is a hint about reachability, never
    permission to take the heater back early."""
    transport = MockTransport()
    session = await _started(transport)
    try:
        await session.yield_link(60.0)
        transport.power_on_adapter()
        await asyncio.sleep(0.2)
        assert session.link is LinkState.YIELDED
        assert transport.is_connected is False
    finally:
        await session.stop()


async def test_yield_releases_the_link_and_blocks_reconnection() -> None:
    """The escape hatch that makes running a daemon against this device acceptable: the
    owner has no working physical remote, so they must be able to get their heater back."""
    transport = MockTransport()
    session = await _started(transport)
    try:
        await session.yield_link(60.0)
        assert session.link is LinkState.YIELDED
        assert transport.is_connected is False

        attempts = transport.connect_attempts
        await asyncio.sleep(0.2)
        assert session.link is LinkState.YIELDED, "a yield must not be quietly reconnected"
        assert transport.connect_attempts == attempts
        assert session.yield_remaining_s is not None
    finally:
        await session.stop()


async def test_a_yield_during_a_reconnect_still_ends_with_the_link_released() -> None:
    """A drop and a yield seconds apart is an ordinary evening, and a yield that loses that
    race is a yield that did not happen — the owner asks for their heater back and the
    daemon quietly keeps it."""
    transport = MockTransport()
    session = await _started(transport, backoff_initial=0.01, backoff_max=0.01)
    try:
        transport.drop()
        # Yield without waiting for the supervisor to settle, so the request lands while a
        # reconnect is plausibly in flight.
        await session.yield_link(60.0)

        for _ in range(50):
            await asyncio.sleep(0.01)
            assert session.link is LinkState.YIELDED
            assert transport.is_connected is False, "a yielded link must not be reclaimed"
    finally:
        await session.stop()


async def test_a_yield_expires_and_the_link_comes_back() -> None:
    transport = MockTransport()
    session = await _started(transport)
    try:
        await session.yield_link(0.05)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.link is LinkState.CONNECTED:
                break
        assert session.link is LinkState.CONNECTED
        assert session.yield_remaining_s is None
    finally:
        await session.stop()


async def test_resume_ends_a_yield_early() -> None:
    transport = MockTransport()
    session = await _started(transport)
    try:
        await session.yield_link(600.0)
        await session.resume()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.link is LinkState.CONNECTED:
                break
        assert session.link is LinkState.CONNECTED
    finally:
        await session.stop()


async def test_a_reading_goes_stale_without_being_deleted() -> None:
    """Stale means 'we have not heard from it recently'. It never means 'it is off' — the
    BedJet is near-silent in standby, so quiet is the normal state of a switched-off unit."""
    transport = MockTransport()
    session = await _started(transport, stale_after_s=0.05)
    try:
        transport.emit(STATUS_UUID, STANDBY)
        assert session.snapshot().stale is False
        await asyncio.sleep(0.1)

        snapshot = session.snapshot()
        assert snapshot.stale is True
        assert snapshot.reading is not None, "a stale reading is still the best we have"
    finally:
        await session.stop()


async def test_subscribers_are_notified_and_can_unsubscribe() -> None:
    transport = MockTransport()
    session = await _started(transport)
    seen: list[str] = []
    unsubscribe = session.subscribe(lambda snap: seen.append(snap.link.value))
    try:
        transport.emit(STATUS_UUID, RUNNING)
        assert seen, "a subscriber must be told about a new reading"

        unsubscribe()
        before = len(seen)
        transport.emit(STATUS_UUID, STANDBY)
        assert len(seen) == before
    finally:
        await session.stop()


async def test_one_broken_subscriber_does_not_starve_the_others() -> None:
    """A websocket that dies mid-write must not take the device link down with it."""
    transport = MockTransport()
    session = await _started(transport)
    survivors: list[object] = []

    def explode(_snapshot: object) -> None:
        raise RuntimeError("this listener is broken")

    session.subscribe(explode)
    session.subscribe(survivors.append)
    try:
        transport.emit(STATUS_UUID, RUNNING)
        assert survivors, "the healthy subscriber must still have been called"
        assert session.link is LinkState.CONNECTED
    finally:
        await session.stop()


async def test_only_one_status_subscription_exists_per_session() -> None:
    """Two subscriptions to the status characteristic means two follow-up reads in flight
    against one handle — the collision that manufactured a false 'heater is off' in RL-017."""
    transport = MockTransport()
    session = await _started(transport)
    try:
        session.subscribe(lambda _snap: None)
        session.subscribe(lambda _snap: None)
        assert len(transport._subs[STATUS_UUID]) == 1
    finally:
        await session.stop()


async def test_stop_is_safe_to_call_twice() -> None:
    transport = MockTransport()
    session = await _started(transport)
    await session.stop()
    await session.stop()
    assert session.link is LinkState.STOPPED
    assert transport.is_connected is False
