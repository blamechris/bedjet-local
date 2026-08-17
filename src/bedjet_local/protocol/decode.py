"""Status packet decoding.

Pure and synchronous: bytes in, dataclasses out. No I/O, no async, no ``bleak``. This is
what makes the protocol layer fully testable without hardware, and it is enforced by
``tests/unit/test_layering.py``.

Decoding never raises on malformed input. A short, corrupt, or unexpected packet decodes
as far as it can and reports the rest in ``StatusPacket.anomalies``. Rationale: this
protocol is undocumented by the vendor and can change under a firmware update, so the
decoder's job is to *observe accurately*, including observing that something is wrong.
"""

from __future__ import annotations

from .constants import (
    ABSOLUTE_MAX_C,
    ABSOLUTE_MIN_C,
    FAN_STEP_MAX,
    FAN_STEP_MIN,
    HEADER_LENGTH,
    MIN_STATUS_LENGTH,
    PACKET_FORMAT_DEBUG,
    PACKET_FORMAT_V3_HOME,
    STATUS_MODE_PREDICTION,
    UNKNOWN_REGION,
    Offset,
    StatusMode,
    fan_step_to_percent,
    temp_byte_to_c,
    verify_checksum,
)
from .packets import StatusPacket


def _u8(data: bytes, offset: int) -> int | None:
    return data[offset] if offset < len(data) else None


def _u16_be(data: bytes, offset: int) -> int | None:
    """Big-endian uint16.

    ⚠️ Unusual for BLE, which is little-endian by convention — but the data says so. Two
    consecutive turbo packets read ``00 0d`` then ``00 0e`` while remaining time fell
    9:47 -> 9:46 against a 10:00 limit, i.e. 13 then 14 seconds elapsed. Little-endian would
    make those 3328 and 3584, which fit nothing.

    What the data cannot yet separate is big-endian-u16 from a plain u8 at offset 16 with a
    zero byte beside it. A capture more than 255 s into turbo settles it; the turbo limit is
    600 s, so it is reachable (RL-013).
    """
    if offset + 1 >= len(data):
        return None
    return int.from_bytes(data[offset : offset + 2], "big")


def decode_status(data: bytes) -> StatusPacket:
    """Decode a (reassembled) BedJet status packet.

    Args:
        data: raw bytes from the status characteristic. May be a partial notification, a
            full reassembled packet, or something else entirely.
    """
    anomalies: list[str] = []

    if not data:
        return StatusPacket(raw=b"", header=b"", anomalies=("empty packet",))

    header = bytes(data[:HEADER_LENGTH])

    # ✅ VERIFIED header layout (RL-004), settled by the first real capture:
    #   [0] partial flag, [1] format 0x56, [2] payload length, [3] type 0x01.
    packet_format = _u8(data, Offset.PACKET_FORMAT)
    packet_type = _u8(data, Offset.PACKET_TYPE)
    declared_length = _u8(data, Offset.PAYLOAD_LENGTH)
    is_partial = bool(_u8(data, Offset.IS_PARTIAL))

    if packet_format is not None and packet_format not in (
        PACKET_FORMAT_V3_HOME,
        PACKET_FORMAT_DEBUG,
    ):
        anomalies.append(
            f"unexpected packet format 0x{packet_format:02x} at offset {int(Offset.PACKET_FORMAT)}"
        )

    # The device declares its own size, so we can check it rather than assume it. This is
    # the strongest integrity check available without a checksum.
    if declared_length is not None and len(data) < declared_length + HEADER_LENGTH:
        anomalies.append(
            f"incomplete packet: header declares {declared_length + HEADER_LENGTH} bytes, "
            f"got {len(data)}"
        )

    if len(data) < MIN_STATUS_LENGTH:
        anomalies.append(f"packet too short: {len(data)} bytes (need >= {MIN_STATUS_LENGTH})")
        return StatusPacket(
            raw=bytes(data),
            header=header,
            is_partial=is_partial,
            packet_format=packet_format,
            packet_type=packet_type,
            declared_length=declared_length,
            anomalies=tuple(anomalies),
        )

    # ── Time remaining ──────────────────────────────────────────────────────────────────
    hours = _u8(data, Offset.TIME_HOURS)
    minutes = _u8(data, Offset.TIME_MINUTES)
    seconds = _u8(data, Offset.TIME_SECONDS)
    time_remaining_s: int | None = None
    if hours is not None and minutes is not None and seconds is not None:
        time_remaining_s = hours * 3600 + minutes * 60 + seconds
        if minutes > 59 or seconds > 59:
            anomalies.append(f"implausible time remaining {hours:02d}:{minutes:02d}:{seconds:02d}")

    # ── Temperatures ────────────────────────────────────────────────────────────────────
    # Bytes 13-14 are the range the device itself permits *in the current mode* (RL-013),
    # not fixed device limits: standby 10.0-40.0C, cool 19.0-26.0C, turbo a flat 43.0C.
    # Validating against a hardcoded range flagged a perfectly healthy turbo packet as an
    # anomaly, because turbo's 43.0C (109.4F) exceeds the 104F everyone calls the maximum.
    # The device knows its own limits; we should ask it rather than assume.
    min_temp_c = _decode_temp(data, Offset.MIN_TEMP)
    max_temp_c = _decode_temp(data, Offset.MAX_TEMP)
    actual_temp_c = _decode_temp(data, Offset.ACTUAL_TEMP)
    target_temp_c = _decode_temp(data, Offset.TARGET_TEMP)
    ambient_temp_c = _decode_temp(data, Offset.AMBIENT_TEMP)

    for label, value in (
        ("actual", actual_temp_c),
        ("target", target_temp_c),
        ("ambient", ambient_temp_c),
    ):
        if value is not None and not (ABSOLUTE_MIN_C <= value <= ABSOLUTE_MAX_C):
            anomalies.append(
                f"{label} temperature {value:.1f}C is outside the sanity envelope "
                f"{ABSOLUTE_MIN_C}-{ABSOLUTE_MAX_C}C — this looks like a decode error, "
                f"not a device setting"
            )

    if (
        target_temp_c is not None
        and min_temp_c is not None
        and max_temp_c is not None
        and not (min_temp_c <= target_temp_c <= max_temp_c)
    ):
        anomalies.append(
            f"target {target_temp_c:.1f}C is outside the range the device reports for this "
            f"mode ({min_temp_c:.1f}-{max_temp_c:.1f}C)"
        )

    # ── Mode ────────────────────────────────────────────────────────────────────────────
    # ✅ Byte 9 IS the mode (RL-003 resolved). But the status enum is NOT the command enum:
    # our unit read 0x04 while cooling, which the command table calls "turbo" (RL-012).
    # Only COOL is verified, so anything else decodes to None with an anomaly rather than
    # to a guess. A plausible-looking wrong mode is worse than an admitted unknown on a
    # device that makes heat.
    mode_raw = _u8(data, Offset.MODE)
    mode: StatusMode | None = None
    if mode_raw is not None:
        try:
            mode = StatusMode(mode_raw)
        except ValueError:
            known = ", ".join(f"0x{m.value:02x} {m.name.lower()}" for m in StatusMode)
            guess = STATUS_MODE_PREDICTION.get(mode_raw)
            hint = f" Predicted to be {guess}, but that is untested." if guess else ""
            anomalies.append(
                f"status mode 0x{mode_raw:02x} not yet verified (known: {known}).{hint} "
                f"Capture this mode from the vendor app to identify it — do NOT assume the "
                f"command-mode table applies here, it has cool and turbo swapped (RL-013)."
            )

    # ── Fan ─────────────────────────────────────────────────────────────────────────────
    fan_step = _u8(data, Offset.FAN_STEP)
    fan_percent: int | None = None
    if fan_step is not None:
        if FAN_STEP_MIN <= fan_step <= FAN_STEP_MAX:
            fan_percent = fan_step_to_percent(fan_step)
        else:
            anomalies.append(
                f"fan step {fan_step} outside documented range "
                f"{FAN_STEP_MIN}-{FAN_STEP_MAX} (scaling is contested — see RL-002)"
            )

    # ── Checksum ────────────────────────────────────────────────────────────────────────
    # ✅ VERIFIED (RL-013): the whole packet sums to zero mod 256. Undocumented upstream.
    # This is genuine integrity checking AND an independent proof that reassembly worked —
    # a mis-joined packet would not sum to zero.
    checksum_ok: bool | None = None
    if declared_length is not None and len(data) >= declared_length + HEADER_LENGTH:
        whole = data[: declared_length + HEADER_LENGTH]
        checksum_ok = verify_checksum(whole)
        if not checksum_ok:
            anomalies.append(
                f"checksum mismatch: packet sums to 0x{sum(whole) % 256:02x}, expected 0x00. "
                f"The packet is corrupt or mis-reassembled — do not trust its fields."
            )

    unknown_region = bytes(data[UNKNOWN_REGION.start : min(UNKNOWN_REGION.stop, len(data))])

    return StatusPacket(
        raw=bytes(data),
        header=header,
        is_partial=is_partial,
        packet_format=packet_format,
        packet_type=packet_type,
        declared_length=declared_length,
        time_remaining_s=time_remaining_s,
        actual_temp_c=actual_temp_c,
        target_temp_c=target_temp_c,
        ambient_temp_c=ambient_temp_c,
        mode=mode,
        mode_raw=mode_raw,
        fan_step=fan_step,
        fan_percent=fan_percent,
        min_temp_c=min_temp_c,
        max_temp_c=max_temp_c,
        turbo_elapsed_s=_u16_be(data, Offset.TURBO_ELAPSED),
        max_runtime_s=_max_runtime(data),
        checksum_ok=checksum_ok,
        shutdown_reason=_u8(data, Offset.SHUTDOWN_REASON),
        update_phase=_u8(data, Offset.UPDATE_PHASE),
        flags=_u8(data, Offset.FLAGS),
        sequence_step=_u8(data, Offset.SEQUENCE_STEP),
        notify_code=_u8(data, Offset.NOTIFY_CODE),
        unknown_region=unknown_region,
        anomalies=tuple(anomalies),
    )


def _decode_temp(data: bytes, offset: int) -> float | None:
    """Decode one temperature byte.

    Values are never clamped. Clamping an *input* is a safety measure and belongs in the
    device layer; clamping an *observation* is lying about what the device said.
    """
    raw = _u8(data, offset)
    return None if raw is None else temp_byte_to_c(raw)


def _max_runtime(data: bytes) -> int | None:
    """Maximum runtime permitted in the current mode, in seconds.

    ✅ VERIFIED (RL-013): standby 0:00, cool 12:00, turbo 0:10 — and the turbo packet's
    remaining time (9:47) plus its elapsed counter (13 s) reconstitute exactly that 10:00.
    """
    hours = _u8(data, Offset.MAX_RUNTIME_HOURS)
    minutes = _u8(data, Offset.MAX_RUNTIME_MINUTES)
    if hours is None or minutes is None:
        return None
    return hours * 3600 + minutes * 60


def reassemble(first: bytes, remainder: bytes) -> bytes:
    """Join a partial notification with the follow-up read.

    ✅ VERIFIED (RL-012): our first capture arrived as a notification plus a follow-up read
    and reassembled to **exactly** the length its own header declared (27 payload + 4 header
    = 31 bytes received). The remainder continues the packet and does **not** repeat the
    header — the assumption held, and the length byte proves it rather than us hoping.
    """
    return bytes(first) + bytes(remainder)
