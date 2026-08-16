"""BedJet protocol constants.

PROVENANCE: values are tagged ✅ VERIFIED (observed on our device, with a fixture),
📖 UPSTREAM, or ❓ HYPOTHESIS. ``docs/protocol/PROTOCOL.md`` is the authority; this module
is its executable shadow and the two must be kept in step.

Nothing here was copied from upstream source. See docs/decisions/ADR-0003-licensing.md.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

# ── GATT ────────────────────────────────────────────────────────────────────────────────
# ✅ VERIFIED 2026-08-16 — enumerated on our device (RL-007).

SERVICE_UUID: Final = "00001000-bed0-0080-aa55-4265644a6574"
STATUS_UUID: Final = "00002000-bed0-0080-aa55-4265644a6574"
COMMAND_UUID: Final = "00002004-bed0-0080-aa55-4265644a6574"

#: ⚠️ Upstream calls this "device name"; a read returns 4 bytes that are not a name (RL-007).
NAME_UUID: Final = "00002001-bed0-0080-aa55-4265644a6574"

#: 📖 role from bedjet-re; ✅ VERIFIED present.
SEQUENCE_UUID: Final = "00002005-bed0-0080-aa55-4265644a6574"

#: ✅ VERIFIED present, documented in NO upstream source (RL-007). `2003` is write-only and
#: therefore untouchable until we have a hypothesis and a reversal — see docs/SAFETY.md.
UNDOCUMENTED_UUIDS: Final = (
    "00002002-bed0-0080-aa55-4265644a6574",
    "00002003-bed0-0080-aa55-4265644a6574",
    "00002006-bed0-0080-aa55-4265644a6574",
)

CHARACTERISTICS: Final = {
    STATUS_UUID: "status (write, read, notify)",
    NAME_UUID: 'upstream "device name" — role unverified (RL-007)',
    UNDOCUMENTED_UUIDS[0]: "undocumented (write, read)",
    UNDOCUMENTED_UUIDS[1]: "undocumented (write only) — do not probe",
    COMMAND_UUID: "command (write only)",
    SEQUENCE_UUID: "biorhythm sequence fragments (write, read)",
    UNDOCUMENTED_UUIDS[2]: "undocumented (write, read)",
}

NAME_PREFIXES: Final = ("BEDJET", "BedJet")


# ── Status packet layout ────────────────────────────────────────────────────────────────


class Offset(IntEnum):
    """Byte offsets into a reassembled status packet.

    ✅ The 4-byte header was **solved** by the first real capture (RL-004). Our earlier
    guess had the fields in the wrong order; the capture settled it, which is exactly what
    the raw-header-retention was for.
    """

    # ✅ VERIFIED (RL-004). Header, 4 bytes.
    IS_PARTIAL = 0  # 0x01 on the first fragment of a split packet
    PACKET_FORMAT = 1  # 0x56 = V3 home
    PAYLOAD_LENGTH = 2  # bytes AFTER the header; 0x1b (27) + 4 == 31 observed
    PACKET_TYPE = 3  # 0x01 = status

    # ✅ VERIFIED against a known device state (RL-012).
    TIME_HOURS = 4
    TIME_MINUTES = 5
    TIME_SECONDS = 6
    ACTUAL_TEMP = 7
    TARGET_TEMP = 8
    MODE = 9
    FAN_STEP = 10

    # ❓ plausible but unconfirmed — consistent with one capture, needs a second state.
    MAX_RUNTIME_HOURS = 11
    MAX_RUNTIME_MINUTES = 12
    MIN_TEMP = 13  # 0x26 -> 19.0C -> 66.2F, matches the documented minimum
    MAX_TEMP = 14  # 0x34 -> 26.0C -> 78.8F, NOT the documented 104F maximum
    TURBO_TIME = 15  # uint16, bytes 15-16
    AMBIENT_TEMP = 17
    SHUTDOWN_REASON = 18
    UPDATE_PHASE = 26
    FLAGS = 27
    SEQUENCE_STEP = 28
    NOTIFY_CODE = 29


#: Length of the header preceding the payload. ✅ VERIFIED (RL-004).
HEADER_LENGTH: Final = 4

#: Bytes 19-25 have no known meaning. Retained verbatim by the decoder so a diff across
#: device states can attack them without any writes.
UNKNOWN_REGION: Final = range(19, 26)

#: ✅ VERIFIED: our device sent a 31-byte status packet, one byte longer than the 30 upstream
#: describes. Byte 30 (0x31 in the first capture) is unaccounted for.
OBSERVED_PACKET_LENGTH: Final = 31

MIN_STATUS_LENGTH: Final = 11

PACKET_FORMAT_V3_HOME: Final = 0x56
PACKET_FORMAT_DEBUG: Final = 0x05
PACKET_TYPE_STATUS: Final = 0x01
PACKET_TYPE_DEBUG: Final = 0x02


class StatusMode(IntEnum):
    """Mode as reported in a **status packet**.

    ⚠️ This is **not** the same enum as :class:`CommandMode`. Our first capture had the
    device in **Cool** (set via the vendor app) and byte 9 read ``0x04`` — which the command
    table calls *turbo*. Decoding status with the command enum reports the wrong mode, and
    did: our first run displayed "turbo" for a unit that was cooling (RL-012).

    Only ``COOL`` is verified. Every other value decodes to ``None`` with an anomaly rather
    than to a guess, because a plausible-looking wrong mode is worse than an admitted
    unknown — especially for a device that makes heat. Capture each mode from the app to
    fill this in.
    """

    COOL = 0x04  # ✅ VERIFIED 2026-08-16 — fixture cool_fan50_target75f.bin


class CommandMode(IntEnum):
    """Mode operand of the ``0x01`` **command**. 📖 UPSTREAM, never yet sent by us.

    Do not use these to decode status — see :class:`StatusMode`.
    """

    OFF = 0x01
    COOL = 0x02
    HEAT = 0x03
    TURBO = 0x04
    DRY = 0x05
    EXTENDED_HEAT = 0x06


# ── Encodings ───────────────────────────────────────────────────────────────────────────

#: ✅ VERIFIED (RL-001): byte 8 was 0x30 (48) with the app's target set to 75F. 48/2 = 24.0C
#: = 75.2F. Wire format is 2 x degrees Celsius.
TEMP_SCALE: Final = 2.0

MIN_TARGET_C: Final = 19.0
MAX_TARGET_C: Final = 40.0

FAN_STEP_MIN: Final = 0
FAN_STEP_MAX: Final = 19

#: ✅ VERIFIED (RL-002): byte 10 was 0x09 (step 9) with the app's fan set to 50%.
#: ESPHome's ``5 + 5 * step`` gives 50; the MQTT bridge's ``5 * step`` gives 45. ESPHome is
#: correct. The contest is settled.
FAN_PERCENT_BASE: Final = 5
FAN_PERCENT_STEP: Final = 5


def fan_step_to_percent(step: int) -> int:
    """Fan step index -> percent. ✅ VERIFIED (RL-002)."""
    return FAN_PERCENT_BASE + FAN_PERCENT_STEP * step


def temp_byte_to_c(raw: int) -> float:
    """Wire temperature byte -> degrees Celsius. ✅ VERIFIED (RL-001)."""
    return raw / TEMP_SCALE


def c_to_f(celsius: float) -> float:
    """Exact conversion, for display only."""
    return celsius * 9.0 / 5.0 + 32.0
