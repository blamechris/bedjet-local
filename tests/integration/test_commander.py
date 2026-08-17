"""Write-path tests against the mock transport. No adapter, no device, no network.

The write path is the part of this project that can do something physical, so it gets the
most adversarial tests: refuse when the write would be unobservable, refuse when there is no
state to compare against, and — most importantly — **fail loudly when the bytes go out and
nothing happens**, rather than reporting success because the radio accepted them.
"""

from __future__ import annotations

import asyncio

import pytest

from bedjet_local.device.state import Power
from bedjet_local.protocol.constants import COMMAND_UUID, STATUS_UUID, StatusMode
from bedjet_local.service.commander import Commander, CommandRefused, CommandUnverified
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)


async def _armed(transport: MockTransport, initial: bytes | None = RUNNING) -> Commander:
    await transport.connect("mock")
    commander = Commander(transport, settle_timeout=0.3)
    await commander.start()
    if initial is not None:
        transport.emit(STATUS_UUID, initial)
    return commander


async def test_off_writes_and_verifies() -> None:
    transport = MockTransport()
    commander = await _armed(transport)

    async def respond() -> None:
        await asyncio.sleep(0)
        transport.emit(STATUS_UUID, STANDBY)

    task = asyncio.create_task(respond())
    result = await commander.send_off()
    await task

    assert transport.writes == [(COMMAND_UUID, bytes([0x01, 0x01]))]
    assert result.before.power is Power.ON
    assert result.after.power is Power.OFF
    assert result.after.mode is StatusMode.STANDBY


async def test_off_refuses_when_the_unit_is_already_off() -> None:
    """An unobservable command teaches us nothing while still being a write to a heater."""
    transport = MockTransport()
    commander = await _armed(transport, STANDBY)

    with pytest.raises(CommandRefused, match="already satisfies"):
        await commander.send_off()
    assert transport.writes == [], "refused commands must not reach the device"


async def test_off_refuses_when_there_is_no_state_to_compare_against() -> None:
    """Silence is ambiguous, not 'unchanged'. Without a baseline there is nothing to verify."""
    transport = MockTransport()
    commander = await _armed(transport, initial=None)

    with pytest.raises(CommandRefused, match="no status packet"):
        await commander.send_off()
    assert transport.writes == []


async def test_off_raises_unverified_when_the_device_ignores_it() -> None:
    """The dangerous case: bytes went out and nothing observable happened.

    This must NOT report success. There is no acknowledgement in this protocol, so a write
    returning cleanly proves only that the radio took the bytes.
    """
    transport = MockTransport()
    commander = await _armed(transport)

    async def keep_running() -> None:
        for _ in range(4):
            await asyncio.sleep(0.05)
            transport.emit(STATUS_UUID, RUNNING)

    task = asyncio.create_task(keep_running())
    with pytest.raises(CommandUnverified, match="did not satisfy"):
        await commander.send_off()
    await task

    assert transport.writes, "the write did happen — that is why this is unverified, not refused"


async def test_unverified_message_tells_the_operator_how_to_recover() -> None:
    """A heater still running after a failed OFF is exactly when the message matters."""
    transport = MockTransport()
    commander = await _armed(transport)

    with pytest.raises(CommandUnverified) as excinfo:
        await commander.send_off()
    message = str(excinfo.value)
    assert "vendor app" in message and "unplug" in message


async def test_dry_run_sends_nothing() -> None:
    transport = MockTransport()
    commander = await _armed(transport)

    result = await commander.send_off(dry_run=True)

    assert transport.writes == [], "a dry run must not write"
    assert result.payload == bytes([0x01, 0x01])


async def test_dry_run_still_enforces_the_preconditions() -> None:
    """Rehearsal is only useful if it rehearses the checks too."""
    transport = MockTransport()
    commander = await _armed(transport, STANDBY)

    with pytest.raises(CommandRefused):
        await commander.send_off(dry_run=True)


async def test_commander_sends_only_off() -> None:
    """The write path has exactly one command in it, and this is the behavioural check."""
    transport = MockTransport()
    commander = await _armed(transport)

    async def respond() -> None:
        await asyncio.sleep(0)
        transport.emit(STATUS_UUID, STANDBY)

    task = asyncio.create_task(respond())
    await commander.send_off()
    await task

    assert {payload for _, payload in transport.writes} == {bytes([0x01, 0x01])}


# ── Command #2: fan speed (RL-019's next step) ──────────────────────────────────────────


async def test_set_fan_writes_the_fan_opcode_and_verifies() -> None:
    """Fan exercises opcode 0x07 — OFF only ever verified 0x01."""
    transport = MockTransport()
    commander = await _armed(transport, build_status(mode=StatusMode.COOL, fan_step=9))

    async def respond() -> None:
        await asyncio.sleep(0)
        transport.emit(STATUS_UUID, build_status(mode=StatusMode.COOL, fan_step=19))

    task = asyncio.create_task(respond())
    result = await commander.set_fan_percent(100)
    await task

    assert transport.writes == [(COMMAND_UUID, bytes([0x07, 19]))]
    assert result.after.fan_percent == 100


async def test_set_fan_refuses_when_the_unit_is_off() -> None:
    """RL-013: the fan byte holds its last-set value in standby, so a change is invisible."""
    transport = MockTransport()
    commander = await _armed(transport, STANDBY)

    with pytest.raises(CommandRefused, match="not running"):
        await commander.set_fan_percent(50)
    assert transport.writes == []


async def test_set_fan_refuses_when_already_at_that_speed() -> None:
    transport = MockTransport()
    commander = await _armed(transport, build_status(mode=StatusMode.COOL, fan_step=9))

    with pytest.raises(CommandRefused, match="already satisfies"):
        await commander.set_fan_percent(50)
    assert transport.writes == []


async def test_set_fan_is_unverified_when_the_device_ignores_it() -> None:
    transport = MockTransport()
    commander = await _armed(transport, build_status(mode=StatusMode.COOL, fan_step=9))

    with pytest.raises(CommandUnverified, match="fan -> 100%"):
        await commander.set_fan_percent(100)
    assert transport.writes, "the write happened; only the effect is unconfirmed"
