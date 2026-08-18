"""The wire vocabulary: what an adapter is allowed to say about a BedJet.

**This module is the translation boundary.** Below it there are enums, packets, byte
offsets and Celsius halves; above it there are strings, numbers and booleans that survive a
round trip through JSON without losing their meaning. That is the whole point of ADR-0002's
"neither adapter may contain protocol knowledge": an adapter that has to know
``StatusMode.COOL is 0x04`` has already leaked, and the fix is here, not there.

Two rules the shapes below exist to enforce:

- **Link and device are separate facts.** ``available`` is about the radio; ``power`` is
  about the heater. A snapshot with ``available=False`` still carries the last reading,
  because throwing it away is no more honest than pretending it is current — so it carries
  ``reading_age_s`` instead and lets the consumer decide.
- **Bounds come from the device, per mode.** ``min_target_c`` moves when the mode moves
  (RL-013), and turbo legitimately reports 43 °C — above the 104 °F every public source
  calls the maximum. A consumer that hardcodes a range will be wrong; these fields are how
  it avoids having to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..device.state import BedJetState
from ..protocol.constants import (
    ABSOLUTE_MAX_C,
    ABSOLUTE_MIN_C,
    FAN_PERCENT_STEP,
    TEMP_SCALE,
    c_to_f,
)
from ..protocol.encode import FAN_PERCENT_MAX, FAN_PERCENT_MIN
from ..service.session import SessionSnapshot


def _f(celsius: float | None) -> float | None:
    """Celsius to Fahrenheit, or None. Both units are published because the device thinks
    in Celsius halves and every human involved with this one thinks in Fahrenheit."""
    return None if celsius is None else round(c_to_f(celsius), 1)


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Everything an adapter may know about the device, in JSON-safe primitives."""

    link: str
    """``connected`` | ``connecting`` | ``lost`` | ``yielded`` | ``stopped``. ``yielded``
    means we deliberately released the device's single BLE slot so its owner could use the
    vendor app — it is not a fault, and a consumer should not alarm on it."""

    available: bool
    """Whether the BLE link is up **right now**. Not whether the unit is running."""

    stale: bool
    """Whether the reading below is old enough to distrust. Note that a stale reading is
    not evidence the unit is off: the BedJet is near-silent in standby, so quiet is the
    normal state of a switched-off heater."""

    reading_age_s: float | None
    power: str
    """``on`` | ``off`` | ``unknown``. ``unknown`` is a real answer, not a bug."""

    mode: str | None
    target_temp_c: float | None
    target_temp_f: float | None
    actual_temp_c: float | None
    actual_temp_f: float | None
    ambient_temp_c: float | None
    ambient_temp_f: float | None
    fan_percent: int | None
    fan_is_stale: bool
    """The fan byte keeps its last-set value in standby (RL-013). When this is true the
    percentage is a memory, not airflow, and rendering it as a live fan speed is wrong."""

    time_remaining_s: int | None
    min_target_c: float | None
    max_target_c: float | None
    max_runtime_s: int | None
    anomalies: tuple[str, ...]
    """Decode oddities, surfaced rather than swallowed. Non-empty is not necessarily a
    fault — it is usually the device telling us something we have not learned yet."""

    @classmethod
    def from_session(cls, snapshot: SessionSnapshot) -> DeviceSnapshot:
        reading = snapshot.reading or BedJetState()
        return cls(
            link=snapshot.link.value,
            available=snapshot.available,
            stale=snapshot.stale,
            reading_age_s=(
                None if snapshot.reading_age_s is None else round(snapshot.reading_age_s, 1)
            ),
            power=reading.power.value,
            mode=None if reading.mode is None else reading.mode.name.lower(),
            target_temp_c=reading.target_temp_c,
            target_temp_f=_f(reading.target_temp_c),
            actual_temp_c=reading.actual_temp_c,
            actual_temp_f=_f(reading.actual_temp_c),
            ambient_temp_c=reading.ambient_temp_c,
            ambient_temp_f=_f(reading.ambient_temp_c),
            fan_percent=reading.fan_percent,
            fan_is_stale=reading.fan_is_stale,
            time_remaining_s=reading.time_remaining_s,
            min_target_c=reading.min_target_c,
            max_target_c=reading.max_target_c,
            max_runtime_s=reading.max_runtime_s,
            anomalies=reading.errors,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe mapping. Every value is a primitive, a list of primitives, or null."""
        return {
            "link": self.link,
            "available": self.available,
            "stale": self.stale,
            "reading_age_s": self.reading_age_s,
            "power": self.power,
            "mode": self.mode,
            "target_temp_c": self.target_temp_c,
            "target_temp_f": self.target_temp_f,
            "actual_temp_c": self.actual_temp_c,
            "actual_temp_f": self.actual_temp_f,
            "ambient_temp_c": self.ambient_temp_c,
            "ambient_temp_f": self.ambient_temp_f,
            "fan_percent": self.fan_percent,
            "fan_is_stale": self.fan_is_stale,
            "time_remaining_s": self.time_remaining_s,
            "min_target_c": self.min_target_c,
            "max_target_c": self.max_target_c,
            "max_runtime_s": self.max_runtime_s,
            "anomalies": list(self.anomalies),
        }


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """The result of asking the device to do something.

    ``ok`` and ``changed`` are separate because they answer separate questions, and
    collapsing them is how an API starts lying. Asking an already-off heater to switch off
    is ``ok=True, changed=False``: the request is satisfied, but nothing was sent and
    therefore **nothing was verified** — which is exactly what ``sent=None`` says.
    """

    ok: bool
    changed: bool
    detail: str
    sent: str | None
    """Hex of the bytes actually put on the wire, or null when nothing was sent. This is
    the audit trail for a physical action, so it is part of the contract, not a debug aid."""

    dry_run: bool
    before: DeviceSnapshot | None
    after: DeviceSnapshot | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "detail": self.detail,
            "sent": self.sent,
            "dry_run": self.dry_run,
            "before": None if self.before is None else self.before.to_dict(),
            "after": None if self.after is None else self.after.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this build will let you ask for — the *code's* limits, not the device's.

    The device's own limits are per-mode, live, and published on every snapshot
    (``min_target_c`` / ``max_target_c``). These are the ones we impose: the modes that have
    been verified on our hardware and deliberately unlocked, and the granularity of the
    wire. Turbo and extended heat are absent from ``modes`` because they are locked in
    ``service/commander.py``, and that is a safety decision rather than a configuration.
    """

    address: str
    modes: tuple[str, ...]
    fan_percent_min: int
    fan_percent_max: int
    fan_percent_step: int
    temperature_step_c: float
    absolute_min_c: float
    absolute_max_c: float
    """A sanity envelope for catching a decode that has gone wrong — **not** a permitted
    range. The permitted range is whatever the device reports for its current mode."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "modes": list(self.modes),
            "fan": {
                "min_percent": self.fan_percent_min,
                "max_percent": self.fan_percent_max,
                "step_percent": self.fan_percent_step,
            },
            "temperature": {
                "step_c": self.temperature_step_c,
                "absolute_min_c": self.absolute_min_c,
                "absolute_max_c": self.absolute_max_c,
                "note": (
                    "the permitted range is per-mode and published live on the state as "
                    "min_target_c/max_target_c; these absolutes are only a sanity envelope"
                ),
            },
        }


#: Fan percentages the device accepts: re-exported from the protocol layer's derivation of
#: the RL-002 grid, so this copy cannot drift from the one that builds the wire bytes.

#: Wire granularity for a target temperature: the byte is 2 x degrees Celsius (RL-001).
TEMPERATURE_STEP_C = 1.0 / TEMP_SCALE

__all__ = [
    "ABSOLUTE_MAX_C",
    "ABSOLUTE_MIN_C",
    "FAN_PERCENT_MAX",
    "FAN_PERCENT_MIN",
    "FAN_PERCENT_STEP",
    "TEMPERATURE_STEP_C",
    "Capabilities",
    "CommandOutcome",
    "DeviceSnapshot",
]
