"""The device model: what higher layers are allowed to know about a BedJet.

Deliberately free of bytes, offsets, characteristics, and Bluetooth. If Jarvis or an MQTT
adapter ever needs a field that is not here, the answer is to add it here — not to reach
down into ``protocol/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..protocol.constants import StatusMode, c_to_f
from ..protocol.packets import StatusPacket


class Power(Enum):
    """Whether the unit is running.

    ``UNKNOWN`` is a real, common state and not a bug: the BedJet barely notifies while
    off, so a quiet link is ambiguous between "off" and "we have not heard anything yet".
    Collapsing that into ``OFF`` would make the daemon confidently wrong.
    """

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BedJetState:
    """A snapshot of what we believe about the device.

    ``available`` (is the BLE link up?) and ``power`` (is the unit running?) are
    independent and must never be inferred from one another.
    """

    available: bool = False
    power: Power = Power.UNKNOWN
    mode: StatusMode | None = None
    target_temp_c: float | None = None
    actual_temp_c: float | None = None
    ambient_temp_c: float | None = None
    fan_percent: int | None = None
    time_remaining_s: int | None = None
    sequence_step: int | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)
    """Anomalies carried up from decoding. Surfaced, never swallowed — an unexplained
    packet is information about the device, and the point of this project is to learn."""

    @classmethod
    def from_status(cls, packet: StatusPacket, *, available: bool = True) -> BedJetState:
        # ✅ STANDBY (0x00) is now verified (RL-013), so "off" is a real answer rather than
        # an absence of one. Verified running modes imply ON; anything still unrecognised
        # stays UNKNOWN rather than being guessed at.
        if packet.mode is StatusMode.STANDBY:
            power = Power.OFF
        elif packet.mode in (StatusMode.COOL, StatusMode.TURBO):
            power = Power.ON
        else:
            power = Power.UNKNOWN

        return cls(
            available=available,
            power=power,
            mode=packet.mode,
            target_temp_c=packet.target_temp_c,
            actual_temp_c=packet.actual_temp_c,
            ambient_temp_c=packet.ambient_temp_c,
            fan_percent=packet.fan_percent,
            time_remaining_s=packet.time_remaining_s,
            sequence_step=packet.sequence_step,
            errors=packet.anomalies,
        )

    def describe(self) -> str:
        """One-line human summary, for the CLI and for logs."""
        if not self.available:
            return "unavailable (no BLE link)"

        parts = [f"power={self.power.value}"]
        if self.mode is not None:
            parts.append(f"mode={self.mode.name.lower()}")
        if self.actual_temp_c is not None:
            parts.append(f"actual={self.actual_temp_c:.1f}C/{c_to_f(self.actual_temp_c):.0f}F")
        if self.target_temp_c is not None:
            parts.append(f"target={self.target_temp_c:.1f}C/{c_to_f(self.target_temp_c):.0f}F")
        if self.ambient_temp_c is not None:
            parts.append(f"ambient={self.ambient_temp_c:.1f}C")
        if self.fan_percent is not None:
            # The fan byte holds the last-set value even in standby (RL-013): an idle unit
            # reported 50% right after a 50% cool session. Reporting that as live airflow
            # would be wrong, so say so.
            stale = " (last set)" if self.power is Power.OFF else ""
            parts.append(f"fan={self.fan_percent}%{stale}")
        if self.time_remaining_s:
            h, rem = divmod(self.time_remaining_s, 3600)
            m, s = divmod(rem, 60)
            parts.append(f"remaining={h:d}:{m:02d}:{s:02d}")
        if self.errors:
            parts.append(f"anomalies={len(self.errors)}")
        return "  ".join(parts)
