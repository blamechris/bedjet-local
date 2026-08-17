"""Command encoding. Pure: builds byte strings, sends nothing.

⚠️ **Every command in this module is 📖 UPSTREAM and has never been sent to our device.**
That is a weaker footing than it sounds, for a specific reason established by RL-012/13/14:

The same upstream sources that gave us these command values also implied a *status* mode
enum, and that implication was **wrong** — status and command use different, offset enums,
and every overlap decoded as another real mode rather than as nonsense. The command table
may well be right (it was presumably derived from watching the vendor app's writes), but
the source has demonstrably conflated two things once already. We have no independent
verification of any byte here.

The practical consequence, and the rule for Milestone 2: **every command must be verified by
reading the status back.** A write that produces no observable state change has not
succeeded, it has merely not errored.

Nothing in this module can reach the device. Sending requires a transport, and no caller
wires one up — see ``tests/unit/test_layering.py::test_no_code_path_sends_a_command``.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from .constants import (
    FAN_PERCENT_STEP,
    FAN_STEP_MAX,
    FAN_STEP_MIN,
    TEMP_SCALE,
    CommandMode,
)

#: Command opcodes, operands of a write to the command characteristic.
#: 📖 UPSTREAM (pjt0620), unverified on our device.
OPCODE_BUTTON: Final = 0x01
OPCODE_TIMER: Final = 0x02
OPCODE_TEMPERATURE: Final = 0x03
OPCODE_FAN: Final = 0x07


class Button(IntEnum):
    """Button/preset operands of :data:`OPCODE_BUTTON`. 📖 UPSTREAM, unverified."""

    FAN_UP = 0x10
    FAN_DOWN = 0x11
    TEMP_UP = 0x12
    TEMP_DOWN = 0x13
    PRESET_M1 = 0x20
    PRESET_M2 = 0x21
    PRESET_M3 = 0x22


class CommandError(ValueError):
    """A command that would be invalid or unsafe to send."""


def set_mode(mode: CommandMode) -> bytes:
    """Set the operating mode.

    ⚠️ Takes a :class:`CommandMode`, never a ``StatusMode``. The two enums are offset and
    every overlapping value means a *different real mode* in each (RL-014), so passing the
    wrong one selects a plausible, wrong mode rather than failing.
    """
    if not isinstance(mode, CommandMode):
        raise CommandError(
            f"expected a CommandMode, got {type(mode).__name__}. Status and command modes "
            f"are different enums with different values — see RL-014."
        )
    return bytes([OPCODE_BUTTON, int(mode)])


def turn_off() -> bytes:
    """The safest command the device has, and the first one we will ever send."""
    return set_mode(CommandMode.OFF)


def press(button: Button) -> bytes:
    """Press a button or recall a preset."""
    return bytes([OPCODE_BUTTON, int(button)])


def set_fan_step(step: int) -> bytes:
    """Set fan speed by step index (0-19)."""
    if not FAN_STEP_MIN <= step <= FAN_STEP_MAX:
        raise CommandError(f"fan step {step} outside {FAN_STEP_MIN}-{FAN_STEP_MAX}")
    return bytes([OPCODE_FAN, step])


#: The device supports 5-100% in 5% steps. ✅ VERIFIED mapping (RL-002).
FAN_PERCENT_MIN: Final = 5
FAN_PERCENT_MAX: Final = 100


def set_fan_percent(percent: int) -> bytes:
    """Set fan speed by percentage, snapped to the nearest supported 5% step.

    Values outside 5-100% are rejected rather than snapped inward. Rounding 4% up to 5% is
    harmless; accepting 200% and quietly sending 100% teaches the caller that its input was
    fine. On a device with physical effects, an out-of-range request is a bug worth
    surfacing, not smoothing over.
    """
    if not FAN_PERCENT_MIN <= percent <= FAN_PERCENT_MAX:
        raise CommandError(
            f"fan {percent}% is outside the supported {FAN_PERCENT_MIN}-{FAN_PERCENT_MAX}% range"
        )
    return set_fan_step(round((percent - FAN_PERCENT_MIN) / FAN_PERCENT_STEP))


def set_temperature(
    celsius: float,
    *,
    min_c: float | None = None,
    max_c: float | None = None,
) -> bytes:
    """Set the target temperature.

    ``min_c``/``max_c`` are the bounds **the device reported for its current mode** (status
    bytes 13-14). They are required, and that is deliberate: RL-013 showed the permitted
    range moves with the mode — standby 10-40 °C, cool 19-26 °C, heat 22.5-40 °C, turbo a
    fixed 43 °C — and that turbo's 43 °C exceeds the 104 °F every public source calls the
    device maximum. A hardcoded table would be both wrong and unsafe. The device is the
    authority on what it will accept; ask it.

    Out-of-range values raise rather than clamp. Silently heating to a different temperature
    than the caller asked for is the wrong failure mode for a heater.
    """
    if min_c is None or max_c is None:
        raise CommandError(
            "device-reported bounds are required. Read them from the current status packet "
            "(min_temp_c/max_temp_c) — they change with the mode, so a hardcoded range is "
            "both wrong and unsafe (RL-013)."
        )
    if not min_c <= celsius <= max_c:
        raise CommandError(
            f"target {celsius:.1f}C is outside the {min_c:.1f}-{max_c:.1f}C range the device "
            f"reports for its current mode"
        )
    raw = round(celsius * TEMP_SCALE)
    if not 0 <= raw <= 0xFF:
        raise CommandError(f"target {celsius:.1f}C does not fit in one byte")
    return bytes([OPCODE_TEMPERATURE, raw])


def set_timer(hours: int, minutes: int, *, max_runtime_s: int | None = None) -> bytes:
    """Set the run timer.

    ``max_runtime_s`` is the device-reported maximum for the current mode (status bytes
    11-12). Turbo's is 10 minutes, so a hardcoded ceiling would be wrong there too.
    """
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise CommandError(f"invalid timer {hours}:{minutes:02d}")
    if max_runtime_s is not None and hours * 3600 + minutes * 60 > max_runtime_s:
        raise CommandError(
            f"timer {hours}:{minutes:02d} exceeds the {max_runtime_s // 60} minute maximum "
            f"the device reports for its current mode"
        )
    return bytes([OPCODE_TIMER, hours, minutes])
