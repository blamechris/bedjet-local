"""Service-layer tests against the mock transport. No adapter, no device, no network."""

from __future__ import annotations

from bedjet_local.device.state import BedJetState, Power
from bedjet_local.protocol.constants import STATUS_UUID, Mode
from bedjet_local.protocol.packets import StatusPacket
from bedjet_local.service.reader import StatusReader
from bedjet_local.transport.base import TransportError
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status


class Collector:
    def __init__(self) -> None:
        self.states: list[BedJetState] = []
        self.packets: list[StatusPacket] = []

    def __call__(self, state: BedJetState, packet: StatusPacket) -> None:
        self.states.append(state)
        self.packets.append(packet)


async def test_reader_publishes_decoded_state() -> None:
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    reader = StatusReader(transport, collector)
    await reader.start()

    transport.emit(STATUS_UUID, build_status(mode=Mode.HEAT, target=50))

    assert len(collector.states) == 1
    state = collector.states[0]
    assert state.available is True
    assert state.power is Power.ON
    assert state.mode is Mode.HEAT
    assert state.target_temp_c == 25.0
    assert reader.packets_seen == 1


async def test_off_mode_maps_to_power_off_not_unavailable() -> None:
    """`available` (link) and `power` (device) are independent — the core state-model rule."""
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    await StatusReader(transport, collector).start()

    transport.emit(STATUS_UUID, build_status(mode=Mode.OFF))

    state = collector.states[0]
    assert state.power is Power.OFF
    assert state.available is True


async def test_unknown_mode_leaves_power_unknown() -> None:
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    await StatusReader(transport, collector).start()

    transport.emit(STATUS_UUID, build_status(mode=0x7F))

    assert collector.states[0].power is Power.UNKNOWN


async def test_partial_packet_triggers_follow_up_read() -> None:
    import asyncio

    remainder = bytes([0xDE, 0xAD])
    transport = MockTransport(reads={STATUS_UUID: remainder})
    await transport.connect("mock")
    collector = Collector()
    reader = StatusReader(transport, collector)
    await reader.start()

    first = build_status(partial=1)
    transport.emit(STATUS_UUID, first)
    await asyncio.sleep(0)  # let the follow-up read task run
    await asyncio.sleep(0)

    assert reader.partials_seen == 1
    assert collector.packets, "partial packet produced no state after the follow-up read"
    assert collector.packets[-1].raw == first + remainder


async def test_failed_follow_up_read_does_not_kill_the_reader() -> None:
    import asyncio

    transport = MockTransport()  # no scripted read -> follow-up raises
    await transport.connect("mock")
    collector = Collector()
    reader = StatusReader(transport, collector)
    await reader.start()

    transport.emit(STATUS_UUID, build_status(partial=1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # No state published, but the reader survives and keeps working.
    transport.emit(STATUS_UUID, build_status())
    assert collector.states, "reader stopped publishing after a failed follow-up read"


async def test_anomalies_are_carried_into_device_state() -> None:
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    await StatusReader(transport, collector).start()

    transport.emit(STATUS_UUID, build_status(actual=200))

    assert collector.states[0].errors, "decode anomalies must reach the device layer"


async def test_operations_before_connect_raise() -> None:
    transport = MockTransport()
    try:
        await transport.read(STATUS_UUID)
    except TransportError:
        pass
    else:  # pragma: no cover
        raise AssertionError("read before connect should raise TransportError")


async def test_disconnect_clears_subscriptions() -> None:
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    await StatusReader(transport, collector).start()
    await transport.disconnect()

    transport.emit(STATUS_UUID, build_status())
    assert not collector.states, "notifications delivered after disconnect"


async def test_connection_retry_semantics() -> None:
    transport = MockTransport(fail_connects=2)
    for _ in range(3):
        try:
            await transport.connect("mock")
        except TransportError:
            continue
    assert transport.is_connected
    assert transport.connect_attempts == 3


async def test_milestone_1_never_writes() -> None:
    """The read path must not send a single byte to the device."""
    transport = MockTransport()
    await transport.connect("mock")
    collector = Collector()
    reader = StatusReader(transport, collector)
    await reader.start()
    transport.emit(STATUS_UUID, build_status())
    await reader.stop()

    assert transport.writes == [], f"Milestone 1 wrote to the device: {transport.writes}"
