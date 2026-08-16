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
    MAX_TARGET_C,
    MIN_STATUS_LENGTH,
    MIN_TARGET_C,
    PACKET_FORMAT_DEBUG,
    PACKET_FORMAT_V3_HOME,
    UNKNOWN_REGION,
    Mode,
    Offset,
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

    header = bytes(data[:4])

    # ❓ HYPOTHESIS (RL-004): upstream documents a 4-byte header carrying format, type,
    # length and a partial flag, but not their order. We interpret it, flag it when it
    # disagrees with expectations, and always keep `header` raw so a real capture can
    # correct us without invalidating the fixture.
    packet_format = _u8(data, Offset.PACKET_FORMAT)
    packet_type = _u8(data, Offset.PACKET_TYPE)
    is_partial = bool(_u8(data, Offset.IS_PARTIAL))

    if packet_format is not None and packet_format not in (
        PACKET_FORMAT_V3_HOME,
        PACKET_FORMAT_DEBUG,
    ):
        anomalies.append(
            f"unexpected packet format 0x{packet_format:02x} at offset "
            f"{int(Offset.PACKET_FORMAT)} (header hypothesis may be wrong — see RL-004)"
        )

    if len(data) < MIN_STATUS_LENGTH:
        anomalies.append(f"packet too short: {len(data)} bytes (need >= {MIN_STATUS_LENGTH})")
        return StatusPacket(
            raw=bytes(data),
            header=header,
            is_partial=is_partial,
            packet_format=packet_format,
            packet_type=packet_type,
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
    # ❓ CONTESTED (RL-003): the MQTT bridge reads mode at [13]/[14] instead. We follow
    # ESPHome's [9] and keep the raw byte so an unknown value is visible rather than lost.
    mode_raw = _u8(data, Offset.MODE)
    mode: Mode | None = None
    if mode_raw is not None:
        try:
            mode = Mode(mode_raw)
        except ValueError:
            anomalies.append(f"unknown mode byte 0x{mode_raw:02x} at offset {int(Offset.MODE)}")

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

    📖 UPSTREAM, unverified: ESPHome documents that a status notification flagged partial
    must be completed by an explicit read of the status characteristic. Whether the
    remainder repeats the header is **not known** — this implementation assumes it does not.
    The first real capture settles it, and until then this function is a hypothesis with a
    test, not a fact.
    """
    return bytes(first) + bytes(remainder)
