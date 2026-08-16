"""BedJet protocol constants.

PROVENANCE: every value here is 📖 UPSTREAM or ❓ HYPOTHESIS. Nothing has been observed
on our device yet. The authority for provenance and confidence is
``docs/protocol/PROTOCOL.md``; this module is its executable shadow, and the two must be
kept in step.

Nothing in this file was copied from upstream source. The values are protocol facts read
from upstream documentation and cross-checked between two independent implementations.
See docs/decisions/ADR-0003-licensing.md.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

# ── GATT ────────────────────────────────────────────────────────────────────────────────
# 📖 UPSTREAM, high confidence: ESPHome and the pjt0620 MQTT bridge agree exactly.
# The trailing 12 hex digits are ASCII "BedJet".

SERVICE_UUID: Final = "00001000-bed0-0080-aa55-4265644a6574"
STATUS_UUID: Final = "00002000-bed0-0080-aa55-4265644a6574"
NAME_UUID: Final = "00002001-bed0-0080-aa55-4265644a6574"
COMMAND_UUID: Final = "00002004-bed0-0080-aa55-4265644a6574"
# ❓ HYPOTHESIS, low confidence: single unlicensed source (bedjet-re). Not used yet.
SEQUENCE_UUID: Final = "00002005-bed0-0080-aa55-4265644a6574"

# Name prefixes seen in the wild, used to filter discovery results.
# ❓ HYPOTHESIS — our unit's advertised name is unknown until bring-up step 2.
NAME_PREFIXES: Final = ("BEDJET", "BedJet")


# ── Status packet layout ────────────────────────────────────────────────────────────────
# 📖 UPSTREAM (ESPHome), medium confidence for the header, higher for offsets 4-10 where
# two independent implementations agree.


class Offset(IntEnum):
    """Byte offsets into a reassembled status packet."""

    # ❓ HYPOTHESIS, low confidence. Upstream documents "header (format, type, length)
    # including a partial flag" across bytes 0-3 but not their order. This arrangement is
    # a guess that decode() flags rather than trusts — see RL-004. The raw header is always
    # retained so a capture can correct it.
    LENGTH = 0
    IS_PARTIAL = 1
    PACKET_FORMAT = 2
    PACKET_TYPE = 3

    # 📖 high confidence — both upstreams agree.
    TIME_HOURS = 4
    TIME_MINUTES = 5
    TIME_SECONDS = 6
    ACTUAL_TEMP = 7
    TARGET_TEMP = 8

    # 📖 medium — ESPHome only, and the MQTT bridge reads mode elsewhere (RL-003).
    MODE = 9

    # 📖 high that the field is here; ❓ on its scaling (RL-002).
    FAN_STEP = 10

    # 📖 low confidence.
    MAX_RUNTIME_HOURS = 11
    MAX_RUNTIME_MINUTES = 12
    MIN_TEMP = 13
    MAX_TEMP = 14
    TURBO_TIME = 15  # uint16, bytes 15-16
    AMBIENT_TEMP = 17
    SHUTDOWN_REASON = 18
    UPDATE_PHASE = 26
    FLAGS = 27
    SEQUENCE_STEP = 28
    NOTIFY_CODE = 29


#: Bytes 19-25 have no known meaning. Retained verbatim by the decoder so that a diff
#: across device states can attack them without any writes (RL-003).
UNKNOWN_REGION: Final = range(19, 26)

#: Shortest packet the decoder will attempt semantic fields for.
MIN_STATUS_LENGTH: Final = 11

#: 📖 ESPHome: format 0x56 = "V3 home", 0x05 = debug.
PACKET_FORMAT_V3_HOME: Final = 0x56
PACKET_FORMAT_DEBUG: Final = 0x05
PACKET_TYPE_STATUS: Final = 0x01
PACKET_TYPE_DEBUG: Final = 0x02


class Mode(IntEnum):
    """Operating mode. 📖 UPSTREAM (pjt0620), medium confidence — see RL-003."""

    OFF = 0x01
    COOL = 0x02
    HEAT = 0x03
    TURBO = 0x04
    DRY = 0x05
    EXTENDED_HEAT = 0x06


# ── Encodings ───────────────────────────────────────────────────────────────────────────

#: Wire temperature is 2 x degrees Celsius. 📖 UPSTREAM, high confidence — the two
#: upstream formulas that appear to disagree are the same function (RL-001).
TEMP_SCALE: Final = 2.0

#: Manufacturer's documented operating range, used to flag anomalies on decode and to
#: clamp inputs in the device layer. 66-104 degrees F.
MIN_TARGET_C: Final = 19.0
MAX_TARGET_C: Final = 40.0

#: Fan step index range. 📖 UPSTREAM.
FAN_STEP_MIN: Final = 0
FAN_STEP_MAX: Final = 19

#: ❓ CONTESTED (RL-002): ESPHome maps step -> percent as ``5 + 5 * step``; the MQTT
#: bridge as ``5 * step``. ESPHome's is self-consistent with a 0-19 range (19 -> 100%),
#: so it is the working hypothesis. One capture settles it.
FAN_PERCENT_BASE: Final = 5
FAN_PERCENT_STEP: Final = 5


def fan_step_to_percent(step: int) -> int:
    """Fan step index -> percent. ❓ HYPOTHESIS, see RL-002."""
    return FAN_PERCENT_BASE + FAN_PERCENT_STEP * step


def temp_byte_to_c(raw: int) -> float:
    """Wire temperature byte -> degrees Celsius. 📖 UPSTREAM, high confidence (RL-001)."""
    return raw / TEMP_SCALE


def c_to_f(celsius: float) -> float:
    """Exact conversion, for display only."""
    return celsius * 9.0 / 5.0 + 32.0
