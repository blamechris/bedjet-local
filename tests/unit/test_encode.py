"""Command encoding tests. Pure — nothing here touches a device.

⚠️ Every expected byte string in this file is 📖 UPSTREAM and unverified. These tests pin
what we *intend* to send, so a change is deliberate; they are not evidence that the device
accepts any of it. Only a write followed by an observed state change can establish that.
"""

from __future__ import annotations

import pytest

from bedjet_local.protocol import encode
from bedjet_local.protocol.constants import CommandMode, StatusMode


def test_turn_off_is_the_documented_off_command() -> None:
    """The first command we will ever send, and the safest one available."""
    assert encode.turn_off() == bytes([0x01, 0x01])


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (CommandMode.OFF, b"\x01\x01"),
        (CommandMode.COOL, b"\x01\x02"),
        (CommandMode.HEAT, b"\x01\x03"),
        (CommandMode.TURBO, b"\x01\x04"),
    ],
)
def test_set_mode(mode: CommandMode, expected: bytes) -> None:
    assert encode.set_mode(mode) == expected


def test_set_mode_rejects_a_status_mode() -> None:
    """RL-014: the enums are offset and every overlap means a different real mode.

    Passing a StatusMode would select a plausible, wrong mode rather than failing — cool
    would ask for turbo. The type check is the guard against a mistake that has no
    symptoms.
    """
    with pytest.raises(encode.CommandError, match="different enums"):
        encode.set_mode(StatusMode.COOL)  # type: ignore[arg-type]


# ── Fan ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("percent", "step"), [(5, 0), (50, 9), (100, 19)])
def test_set_fan_percent_uses_the_verified_mapping(percent: int, step: int) -> None:
    """✅ The step mapping is verified (RL-002); the opcode around it is not."""
    assert encode.set_fan_percent(percent) == bytes([0x07, step])


def test_set_fan_percent_snaps_to_the_nearest_step() -> None:
    assert encode.set_fan_percent(52) == encode.set_fan_step(9)


@pytest.mark.parametrize(("percent", "snapped"), [(5, 5), (52, 50), (53, 55), (100, 100)])
def test_snap_fan_percent_names_the_value_the_device_will_adopt(percent: int, snapped: int) -> None:
    """The verifier must compare against this, not the raw request (#23)."""
    assert encode.snap_fan_percent(percent) == snapped


@pytest.mark.parametrize("percent", [0, 4, 101, 200])
def test_snap_fan_percent_rejects_rather_than_snapping_inward(percent: int) -> None:
    with pytest.raises(encode.CommandError):
        encode.snap_fan_percent(percent)


@pytest.mark.parametrize("percent", [0, 4, 101, 200])
def test_set_fan_percent_rejects_unsupported_values(percent: int) -> None:
    with pytest.raises(encode.CommandError):
        encode.set_fan_percent(percent)


@pytest.mark.parametrize("step", [-1, 20, 255])
def test_set_fan_step_rejects_out_of_range(step: int) -> None:
    with pytest.raises(encode.CommandError):
        encode.set_fan_step(step)


# ── Temperature: bounds come from the device, never from us ─────────────────────────────


def test_set_temperature_requires_device_reported_bounds() -> None:
    """RL-013. The permitted range moves with the mode, so there is no correct constant."""
    with pytest.raises(encode.CommandError, match="device-reported bounds are required"):
        encode.set_temperature(25.0)


def test_set_temperature_encodes_with_the_verified_scale() -> None:
    assert encode.set_temperature(24.0, min_c=19.0, max_c=26.0) == bytes([0x03, 48])


def test_set_temperature_accepts_turbos_range_which_exceeds_the_documented_maximum() -> None:
    """43.0 C is 109.4 F — above the 104 F every public source calls the device maximum.

    The device reports it as permitted in turbo, so it must be accepted. A hardcoded
    ceiling would refuse a temperature the device itself offers.
    """
    assert encode.set_temperature(43.0, min_c=43.0, max_c=43.0) == bytes([0x03, 86])


def test_set_temperature_refuses_outside_the_reported_range() -> None:
    with pytest.raises(encode.CommandError, match="outside"):
        encode.set_temperature(35.0, min_c=19.0, max_c=26.0)


def test_set_temperature_raises_rather_than_clamping() -> None:
    """Silently heating to a different temperature than asked is the wrong failure mode."""
    with pytest.raises(encode.CommandError):
        encode.set_temperature(40.0, min_c=19.0, max_c=26.0)


@pytest.mark.parametrize(
    ("celsius", "snapped"), [(22.0, 22.0), (22.2, 22.0), (22.3, 22.5), (25.24, 25.0)]
)
def test_snap_target_c_names_the_value_the_device_will_be_asked_for(
    celsius: float, snapped: float
) -> None:
    """The verifier and every detail string must use this, not the raw request (#27)."""
    assert encode.snap_target_c(celsius) == snapped


@pytest.mark.parametrize("celsius", [-1.0, 128.0])
def test_snap_target_c_rejects_what_no_wire_byte_can_carry(celsius: float) -> None:
    """Range stays the device's call (RL-013); only byte-fit is rejected here."""
    with pytest.raises(encode.CommandError, match="does not fit"):
        encode.snap_target_c(celsius)


# ── Timer ───────────────────────────────────────────────────────────────────────────────


def test_set_timer() -> None:
    assert encode.set_timer(1, 30) == bytes([0x02, 1, 30])


def test_set_timer_respects_the_device_reported_maximum() -> None:
    """Turbo's maximum runtime is 10 minutes (RL-013), so a 30 minute timer is invalid there
    even though it is fine in heat."""
    assert encode.set_timer(0, 5, max_runtime_s=600) == bytes([0x02, 0, 5])
    with pytest.raises(encode.CommandError, match="maximum"):
        encode.set_timer(0, 30, max_runtime_s=600)


@pytest.mark.parametrize(("hours", "minutes"), [(-1, 0), (24, 0), (0, 60), (0, -1)])
def test_set_timer_rejects_nonsense(hours: int, minutes: int) -> None:
    with pytest.raises(encode.CommandError):
        encode.set_timer(hours, minutes)


# ── Buttons ─────────────────────────────────────────────────────────────────────────────


def test_press_button() -> None:
    assert encode.press(encode.Button.FAN_UP) == bytes([0x01, 0x10])
    assert encode.press(encode.Button.PRESET_M1) == bytes([0x01, 0x20])


def test_every_command_is_short_and_starts_with_a_known_opcode() -> None:
    """A sanity net: nothing should encode to a surprising length or an unknown opcode."""
    known = {
        encode.OPCODE_BUTTON,
        encode.OPCODE_TIMER,
        encode.OPCODE_TEMPERATURE,
        encode.OPCODE_FAN,
    }
    commands = [
        encode.turn_off(),
        encode.set_mode(CommandMode.COOL),
        encode.set_fan_percent(50),
        encode.set_temperature(24.0, min_c=19.0, max_c=26.0),
        encode.set_timer(1, 0),
        encode.press(encode.Button.TEMP_UP),
    ]
    for command in commands:
        assert 2 <= len(command) <= 3
        assert command[0] in known
