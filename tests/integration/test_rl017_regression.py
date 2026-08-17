"""Regression tests for RL-017: a corrupt fragment reported a heater as switched off.

What happened: `bedjet off` wrote `01 01`, an 11-byte tail fragment arrived, its tenth byte
happened to be `0x00`, it decoded as `mode=standby`, and the tool printed
**"✅ verified: the device did what we asked"** while the BedJet carried on running.

Three independent defects lined up. Each gets a test, because any one of them alone would
have prevented the false positive and all three were needed to produce it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bedjet_local.protocol.constants import STATUS_UUID, StatusMode
from bedjet_local.protocol.decode import decode_status, looks_like_packet_start
from bedjet_local.service.commander import Commander, CommandRefused, CommandUnverified
from bedjet_local.service.reader import StatusReader
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: The actual bytes that caused the false verification, captured from the real session.
CORRUPT_TAIL = (FIXTURES / "corrupt_tail_fragment.bin").read_bytes()

RUNNING = build_status(mode=StatusMode.COOL, target=50)


def test_the_corrupt_fragment_still_decodes_to_standby() -> None:
    """The trap itself, preserved. This is why field values alone cannot be trusted."""
    packet = decode_status(CORRUPT_TAIL)
    assert packet.mode is StatusMode.STANDBY, "the misleading decode is the whole point"
    assert packet.checksum_ok is False
    assert not packet.is_trustworthy, "but it must never be believed"


def test_the_corrupt_fragment_is_rejected_as_a_packet_start() -> None:
    """Defect 1: a tail fragment was decoded as though it were a whole packet."""
    assert not looks_like_packet_start(CORRUPT_TAIL)
    assert looks_like_packet_start(build_status())


async def test_reader_never_publishes_an_untrustworthy_packet() -> None:
    """Defect 2: the reader published state from a packet that failed its own checksum."""
    transport = MockTransport()
    await transport.connect("mock")
    published: list[object] = []
    reader = StatusReader(transport, lambda state, packet: published.append(state))
    await reader.start()

    transport.emit(STATUS_UUID, CORRUPT_TAIL)

    assert published == [], "a corrupt packet must never reach the device layer"
    assert reader.rejected >= 1


async def test_reader_rejects_a_corrupted_full_length_packet() -> None:
    """A packet that starts correctly but has been mangled must also be dropped."""
    mangled = bytearray(build_status())
    mangled[9] = 0x00  # flip the mode to standby without fixing the checksum
    transport = MockTransport()
    await transport.connect("mock")
    published: list[object] = []
    reader = StatusReader(transport, lambda state, packet: published.append(state))
    await reader.start()

    transport.emit(STATUS_UUID, bytes(mangled))

    assert published == []
    assert reader.rejected_checksum == 1


async def test_off_is_not_verified_by_a_corrupt_packet() -> None:
    """Defect 3, and the one that mattered: verification accepted noise as proof.

    This is the exact scenario — running unit, write goes out, a corrupt fragment arrives
    that decodes to standby. It must end in CommandUnverified, not success.
    """
    transport = MockTransport()
    await transport.connect("mock")
    commander = Commander(transport, settle_timeout=0.3)
    await commander.start()
    transport.emit(STATUS_UUID, RUNNING)

    async def send_corruption() -> None:
        for _ in range(3):
            await asyncio.sleep(0.05)
            transport.emit(STATUS_UUID, CORRUPT_TAIL)

    task = asyncio.create_task(send_corruption())
    with pytest.raises(CommandUnverified):
        await commander.send_off()
    await task

    assert transport.writes, "the write happened; only the verification was wrong before"


async def test_off_refuses_when_the_baseline_is_untrustworthy() -> None:
    """No trustworthy 'before' means nothing to compare against — do not send."""
    transport = MockTransport()
    await transport.connect("mock")
    commander = Commander(transport, settle_timeout=0.2)
    await commander.start()

    transport.emit(STATUS_UUID, CORRUPT_TAIL)

    with pytest.raises(CommandRefused):
        await commander.send_off()
    assert transport.writes == []


# ── Concurrency: the root cause of the corruption ───────────────────────────────────────


async def test_only_one_follow_up_read_is_in_flight_at_a_time() -> None:
    """Defect 0, the root cause.

    Every status arrives split and the device notifies several times a second, so an
    unserialised reader issues overlapping reads of one characteristic. Bleak's
    CoreBluetooth backend keys pending reads by handle, so they collide and return each
    other's data — which is what produced the stray tail in the first place.
    """
    whole = build_status()
    transport = MockTransport(reads={STATUS_UUID: whole[20:]})
    await transport.connect("mock")
    reader = StatusReader(transport, lambda state, packet: None)
    await reader.start()

    for _ in range(5):
        transport.emit(STATUS_UUID, whole[:20])

    assert reader.partials_seen == 1, "only the first should start a read"
    assert reader.dropped_while_busy == 4, "the rest must be skipped, not queued"


async def test_stop_cancels_an_in_flight_read() -> None:
    """A read outliving its subscription produced a storm of backend errors at teardown."""

    class SlowTransport(MockTransport):
        async def read(self, characteristic: str) -> bytes:
            await asyncio.sleep(10)
            raise AssertionError("should have been cancelled")

    transport = SlowTransport()
    await transport.connect("mock")
    reader = StatusReader(transport, lambda state, packet: None)
    await reader.start()
    transport.emit(STATUS_UUID, build_status()[:20])
    await asyncio.sleep(0)

    await asyncio.wait_for(reader.stop(), timeout=1.0)
