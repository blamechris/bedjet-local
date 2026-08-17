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

    # ✅ VERIFIED across 3 modes (RL-013): these are the PERMITTED RANGE and MAX RUNTIME
    # *for the current mode*, not fixed device limits. standby 0:00 / 10.0-40.0C,
    # cool 12:00 / 19.0-26.0C, turbo 0:10 / 43.0C fixed.
    MAX_RUNTIME_HOURS = 11
    MAX_RUNTIME_MINUTES = 12
    MIN_TEMP = 13
    MAX_TEMP = 14

    # ✅ VERIFIED (RL-013): uint16 that counts UP once per second in turbo and stays 0
    # otherwise. Two consecutive turbo packets read 13 then 14 while the remaining time fell
    # 9:47 -> 9:46 against a 10:00 turbo limit, i.e. seconds elapsed in turbo.
    TURBO_ELAPSED = 15  # uint16, bytes 15-16

    AMBIENT_TEMP = 17  # ✅ tracks room temperature across captures
    SHUTDOWN_REASON = 18
    UPDATE_PHASE = 26
    FLAGS = 27
    SEQUENCE_STEP = 28
    NOTIFY_CODE = 29


#: Length of the header preceding the payload. ✅ VERIFIED (RL-004).
HEADER_LENGTH: Final = 4

#: Bytes 19-25 are **invariant across all 5 captures spanning 3 modes** (`12 01 9a 01 10 ff
#: 00`). They are therefore not state — they are configuration or identity, and the firmware
#: version is the leading candidate given we have failed to find it anywhere else (RL-013).
UNKNOWN_REGION: Final = range(19, 26)

#: ✅ VERIFIED: our device sends 31-byte status packets. Upstream's documented layout ends at
#: byte 29; byte 30 is the checksum (see below), which no upstream source mentions.
OBSERVED_PACKET_LENGTH: Final = 31

MIN_STATUS_LENGTH: Final = 11

PACKET_FORMAT_V3_HOME: Final = 0x56
PACKET_FORMAT_DEBUG: Final = 0x05
PACKET_TYPE_STATUS: Final = 0x01
PACKET_TYPE_DEBUG: Final = 0x02


class StatusMode(IntEnum):
    """Mode as reported in a **status packet**.

    ⚠️ **Not** the same enum as :class:`CommandMode`, and the two do not even agree on the
    values they share. Byte 9 read ``0x04`` with the unit cooling; the command table calls
    ``0x04`` *turbo*. Byte 9 read ``0x02`` with the unit in turbo; the command table calls
    ``0x02`` *cool*. **The two enums have cool and turbo swapped** (RL-013) — which is worse
    than an unrelated mapping, because it is exactly the sort of coincidence that makes a
    wrong decode look right.

    Values not listed here decode to ``None`` with an anomaly rather than to a guess.
    """

    STANDBY = 0x00  # ✅ VERIFIED 2026-08-16 — off_standby.bin, off_after_heating.bin
    HEAT = 0x01  # ✅ VERIFIED 2026-08-16 — heat_fan100_target89f.bin
    TURBO = 0x02  # ✅ VERIFIED 2026-08-16 — turbo_fan100_target109f.bin
    COOL = 0x04  # ✅ VERIFIED 2026-08-16 — cool_fan50_target75f.bin
    DRY = 0x05  # ✅ VERIFIED 2026-08-16 — dry_fan100.bin (predicted first, then confirmed)
    # 0x03 remains unobserved. See STATUS_MODE_PREDICTION.


#: ❓ **A prediction, not a finding.** The ordering
#: ``standby, heat, turbo, extended-heat, cool, dry`` predicted `0x05` = dry, and a capture
#: **confirmed it** (RL-015). One successful prediction is encouraging, not licensing: `0x03`
#: is still unobserved and stays out of :class:`StatusMode`, because a prediction that
#: decodes silently is indistinguishable from a fact no matter how good its track record.
STATUS_MODE_PREDICTION: Final = {
    0x03: (
        "extended heat (predicted by the ordering that correctly predicted dry; "
        "still never observed)"
    ),
}


class CommandMode(IntEnum):
    """Mode operand of the ``0x01`` **command**.

    ✅ ``OFF`` is VERIFIED (RL-019): sent to our device, and the unit switched off.

    That one result settles the framing, the characteristic, the opcode, **and the fact that
    this enum is real**. Which confirms RL-014 from the other side: command `0x01` is off and
    status `0x01` is heat — two genuinely different enums, both correct, rather than one
    source being wrong. The remaining values are 📖 UPSTREAM and now decent bets rather than
    guesses, but each is verified on its own before being trusted.

    Do not use these to decode status — see :class:`StatusMode`.
    """

    OFF = 0x01  # ✅ VERIFIED 2026-08-16 (RL-019)
    COOL = 0x02
    HEAT = 0x03
    TURBO = 0x04
    DRY = 0x05
    EXTENDED_HEAT = 0x06


# ── Encodings ───────────────────────────────────────────────────────────────────────────

#: ✅ VERIFIED (RL-001): byte 8 was 0x30 (48) with the app's target set to 75F. 48/2 = 24.0C
#: = 75.2F. Wire format is 2 x degrees Celsius.
TEMP_SCALE: Final = 2.0

#: ⚠️ The device reports its **own** permitted target range per mode, in bytes 13-14, and
#: those bounds move with the mode (RL-013): standby 10.0-40.0 C, cool 19.0-26.0 C, turbo a
#: fixed 43.0 C. Turbo's 43.0 C (109.4 F) is ABOVE the 104 F that upstream and marketing
#: call the maximum — so a hardcoded range produces false anomalies on a perfectly healthy
#: packet, which it did. Validate against the device's own bounds; these constants are only
#: an absolute sanity envelope for catching a decode that has gone truly wrong.
ABSOLUTE_MIN_C: Final = 5.0
ABSOLUTE_MAX_C: Final = 50.0

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


# ── Checksum ────────────────────────────────────────────────────────────────────────────
#: ✅ VERIFIED (RL-013). The final byte is a checksum: the whole packet sums to zero mod 256.
#: **No upstream source documents this** — ESPHome, the HA integrations and bedjet-re all
#: stop at byte 29 and none mentions a checksum. Confirmed against 5 packets spanning 3
#: modes. It gives us real packet integrity, and independently proves our reassembly is
#: correct: a mis-joined packet would not sum to zero.


def compute_checksum(body: bytes) -> int:
    """Checksum byte for ``body`` (the packet up to but excluding the checksum itself)."""
    return (-sum(body)) & 0xFF


def verify_checksum(packet: bytes) -> bool:
    """Whether a whole packet, checksum byte included, sums to zero mod 256."""
    return bool(packet) and sum(packet) % 256 == 0
