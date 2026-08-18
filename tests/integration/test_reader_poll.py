"""Poll-mode reader tests, against the mock transport. No adapter, no device, no network.

Poll mode (#2) is a hardware hypothesis wearing a code change: nobody has observed what a
cold read of the status characteristic returns, so the mode must behave exactly like the
notify path when the hypothesis holds and fail loudly-but-safely when it does not. These
tests pin both, plus the instrumentation the attended run reads its verdict from.

Timing style: tests that only need the *immediate* first read use a poll interval far
longer than the test (10 s) and drive subsequent reads with ``poke()``, so nothing here
depends on a tick landing inside a sleep.
"""

from __future__ import annotations

import asyncio

import pytest

from bedjet_local.device.state import Power
from bedjet_local.protocol.constants import STATUS_UUID, StatusMode
from bedjet_local.service.commander import Commander
from bedjet_local.service.reader import StatusReader
from bedjet_local.service.session import DeviceSession
from bedjet_local.transport.mock import MockTransport
from tests.integration.test_reader import Collector
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)

#: Long enough that no interval tick fires during a test; reads happen only at start and
#: on poke().
NEVER_TICKS = 10.0


async def _polling(
    transport: MockTransport, interval: float = NEVER_TICKS
) -> tuple[StatusReader, Collector]:
    await transport.connect("mock")
    collector = Collector()
    reader = StatusReader(transport, collector, poll_interval=interval)
    await reader.start()
    await asyncio.sleep(0.03)  # let the immediate first read run
    return reader, collector


async def test_whole_packet_read_publishes_state() -> None:
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, collector = await _polling(transport)
    try:
        assert reader.polls == 1
        assert reader.polls_whole == 1
        assert collector.states, "a whole-packet read must publish"
        assert collector.states[0].power is Power.ON
        assert collector.packets[0].is_complete
        assert reader.poll_rtt_ms is not None, "the rtt instrumentation must engage"
    finally:
        await reader.stop()


async def test_poll_mode_never_subscribes() -> None:
    """The #6 race is a notification landing on a pending read's future. Poll mode's
    whole claim to dissolving it is that no notification ever flows — so a notification
    arriving anyway must mean a subscription leaked."""
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, collector = await _polling(transport)
    try:
        published = len(collector.states)
        transport.emit(STATUS_UUID, STANDBY)
        assert len(collector.states) == published, "poll mode must not receive notifications"
    finally:
        await reader.stop()


async def test_poll_mode_never_writes() -> None:
    """The read path must not send a single byte to the device — same rule as notify."""
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, _ = await _polling(transport)
    try:
        assert transport.writes == []
    finally:
        await reader.stop()


async def test_interval_paces_further_reads() -> None:
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, _ = await _polling(transport, interval=0.02)
    try:
        await asyncio.sleep(0.1)
        assert reader.polls >= 2, "the loop must keep reading on its interval"
    finally:
        await reader.stop()


async def test_partial_read_is_counted_not_published() -> None:
    """A 20-byte packet start — the shape notifications arrive in. If cold reads truncate
    the same way, the hypothesis is dead; the counter is how the attended run learns it."""
    transport = MockTransport(reads={STATUS_UUID: RUNNING[:20]})
    reader, collector = await _polling(transport)
    try:
        assert reader.polls_partial == 1
        assert reader.polls_whole == 0
        assert not collector.states, "a partial read must never publish"
    finally:
        await reader.stop()


async def test_alien_read_is_counted_not_published() -> None:
    """A headerless tail — what the firmware's read cursor would serve. Decoding it is
    exactly the RL-017 failure, so it must be counted and dropped, never completed."""
    transport = MockTransport(reads={STATUS_UUID: RUNNING[20:]})
    reader, collector = await _polling(transport)
    try:
        assert reader.polls_alien == 1
        assert not collector.states
    finally:
        await reader.stop()


async def test_corrupt_whole_read_is_rejected() -> None:
    """Whole is shape; trust is the checksum's call — the RL-017 gate holds in poll mode."""
    transport = MockTransport(reads={STATUS_UUID: build_status(checksum=False)})
    reader, collector = await _polling(transport)
    try:
        assert reader.polls_whole == 1
        assert reader.rejected_checksum == 1
        assert not collector.states, "a checksum-failing packet must never publish"
    finally:
        await reader.stop()


async def test_failed_read_does_not_kill_the_polling() -> None:
    transport = MockTransport()  # no scripted read -> every poll raises
    reader, collector = await _polling(transport, interval=0.02)
    try:
        await asyncio.sleep(0.05)
        assert reader.poll_failures >= 2, "the loop must survive a failing read"

        transport.reads[STATUS_UUID] = RUNNING
        await asyncio.sleep(0.06)
        assert collector.states, "the loop must recover once reads succeed again"
    finally:
        await reader.stop()


async def test_poke_pulls_the_next_read_forward() -> None:
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, collector = await _polling(transport)
    try:
        assert reader.polls == 1
        transport.reads[STATUS_UUID] = STANDBY
        reader.poke()
        await asyncio.sleep(0.03)
        assert reader.polls == 2, "poke must trigger a read without waiting for the tick"
        assert collector.states[-1].power is Power.OFF
    finally:
        await reader.stop()


async def test_stop_cancels_the_loop() -> None:
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    reader, _ = await _polling(transport, interval=0.02)
    await reader.stop()
    polls = reader.polls
    await asyncio.sleep(0.05)
    assert reader.polls == polls, "reads after stop mean the loop was not cancelled"


@pytest.mark.parametrize("interval", [0, -1.0, float("inf"), float("nan")])
def test_unusable_intervals_are_refused(interval: float) -> None:
    """inf sleeps forever after one read while holding the device's only client slot;
    nan corrupts the event loop's timer heap. Both pass a bare `<= 0` check."""
    with pytest.raises(ValueError):
        StatusReader(MockTransport(), Collector(), poll_interval=interval)


async def test_raising_callback_does_not_kill_the_loop() -> None:
    """The loop must outlive its listeners — count and drop, never die. The concrete
    trigger: `watch --poll ... | head` makes every publish raise BrokenPipeError once
    the pipe closes, and a dead loop would silently stop reading for the rest of an
    attended run while looking alive."""
    raises: list[int] = []

    def bad_listener(state: object, packet: object) -> None:
        raises.append(1)
        raise BrokenPipeError("consumer went away")

    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    await transport.connect("mock")
    reader = StatusReader(transport, bad_listener, poll_interval=0.02)
    await reader.start()
    try:
        await asyncio.sleep(0.1)
        assert len(raises) >= 2, "the loop must keep polling past a raising callback"
        assert reader.polls >= 2
    finally:
        await reader.stop()  # must not re-raise the callback's exception


async def test_command_verifies_through_polls_alone() -> None:
    """A verified command with no notification anywhere: baseline from a poll, then the
    write pokes the reader, and the confirmation is the very next read. This is the
    property that lets a slow poll cadence coexist with fast verification."""

    class Flipping(MockTransport):
        """Serves RUNNING until any write, then STANDBY — a BedJet that obeys OFF."""

        async def write(
            self, characteristic: str, data: bytes, *, response: bool | None = None
        ) -> None:
            await super().write(characteristic, data, response=response)
            self.reads[STATUS_UUID] = STANDBY

    transport = Flipping(reads={STATUS_UUID: RUNNING})
    await transport.connect("mock")
    commander = Commander(transport, settle_timeout=2.0, poll_interval=NEVER_TICKS)
    await commander.start()
    try:
        await asyncio.sleep(0.03)  # the baseline poll
        result = await commander.send_off()
        assert result.after.mode is StatusMode.STANDBY
        assert commander.reader.polls == 2, "verification should cost exactly one extra read"
    finally:
        await commander.stop()


async def test_slow_device_verifies_via_settle_repokes() -> None:
    """The immediate post-write read is a sample, not a guarantee: the device is not
    obliged to reflect a command within one round-trip. With a poll interval longer than
    the settle window, verification must re-poke each quiet half-second — otherwise a
    command the device obeyed gets reported unverified, and the error text tells the
    owner to unplug a heater that did as it was told."""

    class SlowToObey(MockTransport):
        """Still reports RUNNING on the first post-write read; STANDBY from the second."""

        def __init__(self, reads: dict[str, bytes]) -> None:
            super().__init__(reads)
            self._wrote = False
            self._post_write_reads = 0

        async def write(
            self, characteristic: str, data: bytes, *, response: bool | None = None
        ) -> None:
            await super().write(characteristic, data, response=response)
            self._wrote = True

        async def read(self, characteristic: str) -> bytes:
            data = await super().read(characteristic)
            if self._wrote:
                self._post_write_reads += 1
                if self._post_write_reads >= 2:
                    return STANDBY
            return data

    transport = SlowToObey(reads={STATUS_UUID: RUNNING})
    await transport.connect("mock")
    commander = Commander(transport, settle_timeout=3.0, poll_interval=NEVER_TICKS)
    await commander.start()
    try:
        await asyncio.sleep(0.03)  # the baseline poll
        result = await commander.send_off()
        assert result.after.mode is StatusMode.STANDBY
    finally:
        await commander.stop()


async def test_session_in_poll_mode_publishes_without_notifications() -> None:
    transport = MockTransport(reads={STATUS_UUID: RUNNING})
    session = DeviceSession(transport, "mock", supervise_interval=0.02, poll_interval=0.02)
    await session.start()
    try:
        await asyncio.sleep(0.06)
        snapshot = session.snapshot()
        assert snapshot.reading is not None
        assert snapshot.reading.power is Power.ON
        assert snapshot.stale is False
    finally:
        await session.stop()
