"""The stable local interface, against the mock transport.

These tests are mostly about *translation*, because that is what this layer is for: the
device's vocabulary of enums, packets and exceptions has to arrive at an adapter as strings,
numbers and three clearly different failure modes. The distinctions worth having are the
ones tested here — satisfied vs refused vs unverified, and link vs power.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bedjet_local.api import BedJetAPI, Refused, Unavailable, Unverified
from bedjet_local.protocol.constants import COMMAND_UUID, STATUS_UUID, StatusMode
from bedjet_local.service.session import DeviceSession
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)
HEATING = build_status(mode=StatusMode.HEAT, target=50, min_temp=45, max_temp=80)


async def _api(transport: MockTransport, initial: bytes | None = RUNNING) -> BedJetAPI:
    session = DeviceSession(transport, "mock", settle_timeout=0.3, supervise_interval=0.02)
    await session.start()
    if initial is not None:
        transport.emit(STATUS_UUID, initial)
    return BedJetAPI(session)


def _respond_with(transport: MockTransport, packet: bytes) -> asyncio.Task[None]:
    async def respond() -> None:
        await asyncio.sleep(0)
        transport.emit(STATUS_UUID, packet)

    return asyncio.create_task(respond())


async def test_snapshot_is_json_safe_all_the_way_down() -> None:
    """An adapter serialises this without knowing anything about the device. If a raw enum
    or a dataclass leaks into the snapshot, ``json.dumps`` is where it shows up."""
    transport = MockTransport()
    api = await _api(transport)

    payload = api.snapshot().to_dict()
    assert json.loads(json.dumps(payload))["mode"] == "cool"
    assert payload["power"] == "on"
    assert payload["available"] is True
    assert payload["target_temp_c"] == 25.0
    assert payload["target_temp_f"] == 77.0


async def test_the_device_reported_bounds_reach_the_adapter() -> None:
    """A climate entity needs a slider range. If it is not here, the adapter reaches into
    protocol/ for a byte offset — and the range is per-mode, so a constant would be wrong."""
    transport = MockTransport()
    api = await _api(transport)

    snapshot = api.snapshot()
    assert snapshot.min_target_c == 19.0
    assert snapshot.max_target_c == 26.0

    transport.emit(STATUS_UUID, HEATING)
    assert api.snapshot().min_target_c == 22.5, "the permitted range moves with the mode"


async def test_a_satisfied_request_is_not_an_error_but_admits_nothing_was_sent() -> None:
    """Automations need idempotence; heaters need honesty. Both, in one result."""
    transport = MockTransport()
    api = await _api(transport, STANDBY)

    outcome = await api.turn_off()
    assert outcome.ok is True
    assert outcome.changed is False
    assert outcome.sent is None, "nothing was written, so nothing was verified"
    assert transport.writes == []


async def test_a_verified_command_reports_what_it_put_on_the_wire() -> None:
    transport = MockTransport()
    api = await _api(transport)

    task = _respond_with(transport, STANDBY)
    outcome = await api.turn_off()
    await task

    assert outcome.ok is True
    assert outcome.changed is True
    assert outcome.sent == "01 01"
    assert outcome.after is not None and outcome.after.power == "off"
    assert transport.writes == [(COMMAND_UUID, bytes([0x01, 0x01]))]


async def test_a_dry_run_sends_nothing_and_says_so() -> None:
    transport = MockTransport()
    api = await _api(transport)

    outcome = await api.turn_off(dry_run=True)
    assert outcome.ok is True
    assert outcome.changed is False
    assert outcome.dry_run is True
    assert outcome.sent is None
    assert transport.writes == []


async def test_an_unverified_command_is_its_own_failure_mode() -> None:
    """The dangerous case: bytes went out, nothing observable happened. An adapter must be
    able to tell this apart from a refusal, because the responses are opposite — one means
    'nothing happened', the other means 'we do not know what happened'."""
    transport = MockTransport()
    api = await _api(transport)

    with pytest.raises(Unverified):
        await api.turn_off()
    assert transport.writes, "the bytes really did go out — that is what makes it unverified"


async def test_a_locked_mode_is_refused_by_name() -> None:
    transport = MockTransport()
    api = await _api(transport)

    with pytest.raises(Refused, match="locked"):
        await api.set_mode("turbo")
    assert transport.writes == []


async def test_an_unknown_mode_lists_the_ones_that_exist() -> None:
    transport = MockTransport()
    api = await _api(transport)

    with pytest.raises(Refused, match="cool"):
        await api.set_mode("cryogenic")
    assert transport.writes == []


async def test_capabilities_never_advertise_a_locked_mode() -> None:
    """The mode list is derived from the commander's allowlist, so an adapter cannot offer
    a mode the safety-critical layer refuses to send."""
    transport = MockTransport()
    api = await _api(transport)

    modes = api.capabilities().to_dict()["modes"]
    assert "turbo" not in modes
    assert "extended_heat" not in modes
    assert {"off", "cool", "heat", "dry"} == set(modes)


async def test_temperature_accepts_either_unit_and_only_one() -> None:
    transport = MockTransport()
    api = await _api(transport)

    with pytest.raises(Refused, match="exactly one"):
        await api.set_temperature(celsius=22.0, fahrenheit=72.0)
    with pytest.raises(Refused, match="exactly one"):
        await api.set_temperature()

    task = _respond_with(transport, build_status(mode=StatusMode.COOL, target=44))
    outcome = await api.set_temperature(fahrenheit=71.6)
    await task
    assert outcome.sent == "03 2c", "71.6F is 22.0C is byte 44"


async def test_an_out_of_range_target_is_refused_with_the_devices_own_bounds() -> None:
    transport = MockTransport()
    api = await _api(transport)

    with pytest.raises(Refused, match=r"19\.0-26\.0C"):
        await api.set_temperature(celsius=40.0)
    assert transport.writes == []


async def test_temperature_outcomes_report_the_rounded_target_never_the_raw_request() -> None:
    """#27 at the API surface: every detail names the target the device will be asked for.

    The fan's #23, one command over: the commander rounds to the wire's 0.5 °C granularity
    and verifies the rounded value, so a detail built from the raw request describes a
    target the device never saw.
    """
    transport = MockTransport()
    api = await _api(transport)

    # RUNNING reports byte 50 = 25.0C, so 25.2 is already satisfied — after rounding, not
    # before. This runs first: the verified 22.5C below moves the state off 25.0.
    outcome = await api.set_temperature(celsius=25.2)
    assert outcome.changed is False
    assert "already target 25.0C" in outcome.detail
    assert "25.2" not in outcome.detail

    outcome = await api.set_temperature(celsius=22.3, dry_run=True)
    assert outcome.detail == "would send 03 2d for target 22.5C; nothing was sent"

    task = _respond_with(transport, build_status(mode=StatusMode.COOL, target=45))
    outcome = await api.set_temperature(celsius=22.3)
    await task
    assert outcome.sent == "03 2d"
    assert "target 22.5C" in outcome.detail
    assert "22.3" not in outcome.detail

    # Unfittable values are refused before the commander is involved; range stays the
    # device's call and is tested above with the device's own bounds in the message.
    with pytest.raises(Refused, match="does not fit"):
        await api.set_temperature(celsius=200.0)


async def test_fan_outcomes_report_the_snapped_percent_never_the_raw_request() -> None:
    """#23 at the API surface: every detail names the percent the device will adopt.

    The served contract promises the snapped value is what gets sent, verified, and
    reported; a detail built from the raw request breaks that promise at the exact
    surface the contract describes.
    """
    transport = MockTransport()
    api = await _api(transport)

    # RUNNING reports step 9, so 52 is already satisfied — after snapping, not before.
    # This runs first: the verified 72% below moves the state off 50.
    outcome = await api.set_fan(52)
    assert outcome.changed is False
    assert "already fan 50%" in outcome.detail
    assert "52" not in outcome.detail

    outcome = await api.set_fan(72, dry_run=True)
    assert outcome.detail == "would send 07 0d for fan 70%; nothing was sent"

    task = _respond_with(transport, build_status(mode=StatusMode.COOL, fan_step=13))
    outcome = await api.set_fan(72)
    await task
    assert outcome.sent == "07 0d"
    assert "fan 70%" in outcome.detail
    assert "72" not in outcome.detail

    with pytest.raises(Refused, match="outside the supported"):
        await api.set_fan(104)


async def test_commands_are_refused_outright_while_the_link_is_yielded() -> None:
    """A yield exists so the owner can take their heater back. Queueing commands against a
    link we have deliberately given away would defeat the point."""
    transport = MockTransport()
    api = await _api(transport)
    await api.yield_link(60.0)

    with pytest.raises(Unavailable, match="yielded"):
        await api.turn_off()
    assert transport.writes == []


async def test_subscribers_receive_json_safe_snapshots() -> None:
    transport = MockTransport()
    api = await _api(transport, initial=None)
    seen: list[dict[str, object]] = []
    unsubscribe = api.subscribe(lambda snap: seen.append(snap.to_dict()))

    transport.emit(STATUS_UUID, RUNNING)
    assert seen and seen[-1]["mode"] == "cool"
    json.dumps(seen[-1])

    unsubscribe()
    before = len(seen)
    transport.emit(STATUS_UUID, STANDBY)
    assert len(seen) == before


async def test_a_stale_fan_reading_is_flagged_rather_than_presented_as_airflow() -> None:
    """The fan byte keeps its last-set value in standby (RL-013). The number alone cannot
    tell a consumer that, so the snapshot has to."""
    transport = MockTransport()
    api = await _api(transport, STANDBY)

    snapshot = api.snapshot()
    assert snapshot.fan_percent == 50
    assert snapshot.fan_is_stale is True
