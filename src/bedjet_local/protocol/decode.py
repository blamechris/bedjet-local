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
    FAN_STEP_MAX,
    FAN_STEP_MIN,
    HEADER_LENGTH,
    MAX_TARGET_C,
    MIN_STATUS_LENGTH,
    MIN_TARGET_C,
    PACKET_FORMAT_DEBUG,
    PACKET_FORMAT_V3_HOME,
    UNKNOWN_REGION,
    Offset,
    StatusMode,
    fan_step_to_percent,
    temp_byte_to_c,
)
from .packets import StatusPacket


def _u8(data: bytes, offset: int) -> int | None:
    return data[offset] if offset < len(data) else None


def _u16_le(data: bytes, offset: int) -> int | None:
    if offset + 1 >= len(data):
        return None
    return int.from_bytes(data[offset : offset + 2], "little")


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
    actual_temp_c = _decode_temp(data, Offset.ACTUAL_TEMP, "actual", anomalies)
    target_temp_c = _decode_temp(data, Offset.TARGET_TEMP, "target", anomalies)
    ambient_temp_c = _decode_temp(data, Offset.AMBIENT_TEMP, "ambient", anomalies, strict=False)
    min_temp_c = _decode_temp(data, Offset.MIN_TEMP, "min bound", anomalies, strict=False)
    max_temp_c = _decode_temp(data, Offset.MAX_TEMP, "max bound", anomalies, strict=False)

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
            anomalies.append(
                f"status mode 0x{mode_raw:02x} not yet verified. Only COOL (0x04) is known. "
                f"Set this mode in the vendor app and capture it to identify the value "
                f"(RL-012) — do NOT assume the command-mode table applies here."
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
        turbo_time_s=_u16_le(data, Offset.TURBO_TIME),
        shutdown_reason=_u8(data, Offset.SHUTDOWN_REASON),
        update_phase=_u8(data, Offset.UPDATE_PHASE),
        flags=_u8(data, Offset.FLAGS),
        sequence_step=_u8(data, Offset.SEQUENCE_STEP),
        notify_code=_u8(data, Offset.NOTIFY_CODE),
        unknown_region=unknown_region,
        anomalies=tuple(anomalies),
    )


def _decode_temp(
    data: bytes,
    offset: int,
    label: str,
    anomalies: list[str],
    *,
    strict: bool = True,
) -> float | None:
    """Decode one temperature byte.

    Out-of-range values are reported, never clamped: clamping an *input* is a safety
    measure and belongs in the device layer; clamping an *observation* is lying about what
    the device said.
    """
    raw = _u8(data, offset)
    if raw is None:
        return None
    celsius = temp_byte_to_c(raw)
    if strict and not (MIN_TARGET_C <= celsius <= MAX_TARGET_C):
        anomalies.append(
            f"{label} temperature {celsius:.1f}C (byte 0x{raw:02x} at offset {offset}) "
            f"outside documented range {MIN_TARGET_C}-{MAX_TARGET_C}C"
        )
    return celsius


def reassemble(first: bytes, remainder: bytes) -> bytes:
    """Join a partial notification with the follow-up read.

    ✅ VERIFIED (RL-012): our first capture arrived as a notification plus a follow-up read
    and reassembled to **exactly** the length its own header declared (27 payload + 4 header
    = 31 bytes received). The remainder continues the packet and does **not** repeat the
    header — the assumption held, and the length byte proves it rather than us hoping.
    """
    return bytes(first) + bytes(remainder)
