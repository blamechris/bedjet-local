"""Protocol decoding tests. Pure, synthetic, no hardware, no I/O.

⚠️ These fixtures are **synthetic** — built from the upstream-documented layout, not
captured from our device. They prove the decoder does what we told it to; they do NOT
prove the layout is right. Real captures land in ``tests/fixtures/`` with provenance, and
only then does PROTOCOL.md get to say ✅ VERIFIED.
"""

from __future__ import annotations

import pytest

from bedjet_local.protocol.constants import (
    HEADER_LENGTH,
    OBSERVED_PACKET_LENGTH,
    Offset,
    StatusMode,
    c_to_f,
    compute_checksum,
    fan_step_to_percent,
    temp_byte_to_c,
    verify_checksum,
)
from bedjet_local.protocol.decode import decode_status, reassemble


def build_status(
    *,
    hours: int = 0,
    minutes: int = 30,
    seconds: int = 0,
    actual: int = 44,  # 22.0 C
    target: int = 50,  # 25.0 C
    mode: int = StatusMode.COOL,
    fan_step: int = 9,
    min_temp: int = 38,  # 19.0 C — the range the device reports for cool
    max_temp: int = 52,  # 26.0 C
    partial: int = 0,
    length: int = OBSERVED_PACKET_LENGTH - HEADER_LENGTH,
    packet_format: int = 0x56,
    packet_type: int = 0x01,
    checksum: bool = True,
    trailing: bytes = b"",
) -> bytes:
    """Synthesise a status packet with the VERIFIED layout, bounds, and checksum.

    Shaped like a real packet on purpose: the decoder validates the target against the
    device-reported bounds and checks the trailing checksum, so a synthetic packet that
    omits either is not a useful stand-in for one off the wire.
    """
    data = bytearray(OBSERVED_PACKET_LENGTH)
    data[Offset.PAYLOAD_LENGTH] = length
    data[Offset.IS_PARTIAL] = partial
    data[Offset.PACKET_FORMAT] = packet_format
    data[Offset.PACKET_TYPE] = packet_type
    data[Offset.TIME_HOURS] = hours
    data[Offset.TIME_MINUTES] = minutes
    data[Offset.TIME_SECONDS] = seconds
    data[Offset.ACTUAL_TEMP] = actual
    data[Offset.TARGET_TEMP] = target
    data[Offset.MODE] = mode
    data[Offset.FAN_STEP] = fan_step
    data[Offset.MIN_TEMP] = min_temp
    data[Offset.MAX_TEMP] = max_temp
    data[Offset.AMBIENT_TEMP] = 42
    if checksum:
        data[OBSERVED_PACKET_LENGTH - 1] = compute_checksum(
            bytes(data[: OBSERVED_PACKET_LENGTH - 1])
        )
    return bytes(data) + trailing


# ── Temperature ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "celsius", "fahrenheit"),
    [(40, 20.0, 68.0), (50, 25.0, 77.0), (60, 30.0, 86.0), (44, 22.0, 71.6)],
)
def test_temperature_encoding(raw: int, celsius: float, fahrenheit: float) -> None:
    assert temp_byte_to_c(raw) == celsius
    assert c_to_f(temp_byte_to_c(raw)) == pytest.approx(fahrenheit)


@pytest.mark.parametrize("raw", [40, 50, 60, 44, 70, 80])
def test_upstream_fahrenheit_formula_agrees_with_ours(raw: int) -> None:
    """RL-001: the two upstream encodings that appear to conflict are the same function.

    ESPHome documents ``2 x C``; the pjt0620 bridge uses an integer F polynomial. This
    test pins the finding so nobody re-litigates it — and so we notice immediately if a
    real capture ever disagrees with the analysis.
    """
    delta = raw - 0x26
    upstream_f = (delta + 66) - (delta // 9)
    ours_f = c_to_f(temp_byte_to_c(raw))
    assert abs(upstream_f - ours_f) <= 1.0, f"byte {raw}: upstream {upstream_f}F vs ours {ours_f}F"


def test_out_of_range_temperature_is_reported_not_clamped() -> None:
    packet = decode_status(build_status(actual=200))  # 100 C
    assert packet.actual_temp_c == 100.0, "an observation must never be silently clamped"
    assert any("sanity envelope" in a for a in packet.anomalies)


def test_target_is_validated_against_device_reported_bounds_not_a_constant() -> None:
    """RL-013: turbo legitimately targets 43.0 C, above the 104 F everyone calls the max.

    A hardcoded ceiling flagged a healthy turbo packet as anomalous. The device reports its
    own per-mode range, so that is what the target is checked against.
    """
    turbo_like = build_status(mode=StatusMode.TURBO, target=86, min_temp=86, max_temp=86)
    packet = decode_status(turbo_like)
    assert packet.target_temp_c == 43.0
    assert packet.anomalies == (), f"healthy turbo packet flagged: {packet.anomalies}"


def test_target_outside_the_device_reported_range_is_flagged() -> None:
    packet = decode_status(build_status(target=80, min_temp=38, max_temp=52))
    assert any("outside the range the device reports" in a for a in packet.anomalies)


# ── Checksum ────────────────────────────────────────────────────────────────────────────


def test_checksum_round_trips() -> None:
    packet = decode_status(build_status())
    assert packet.checksum_ok is True
    assert verify_checksum(build_status())


def test_corrupt_packet_fails_the_checksum_and_is_not_plausible() -> None:
    data = bytearray(build_status())
    data[Offset.TARGET_TEMP] ^= 0xFF  # flip a field without fixing the checksum
    packet = decode_status(bytes(data))
    assert packet.checksum_ok is False
    assert not packet.is_plausible
    assert any("checksum mismatch" in a for a in packet.anomalies)


def test_checksum_is_unknown_for_a_truncated_packet() -> None:
    packet = decode_status(build_status()[:20])
    assert packet.checksum_ok is None, "cannot judge a checksum we have not received"


# ── Happy path ──────────────────────────────────────────────────────────────────────────


def test_decodes_a_well_formed_packet() -> None:
    packet = decode_status(build_status())
    assert packet.anomalies == ()
    assert packet.is_plausible
    assert packet.actual_temp_c == 22.0
    assert packet.target_temp_c == 25.0
    assert packet.mode is StatusMode.COOL
    assert packet.fan_step == 9
    assert packet.fan_percent == 50
    assert packet.time_remaining_s == 1800
    assert not packet.is_partial
    assert packet.is_complete


def test_raw_and_header_are_always_retained() -> None:
    raw = build_status()
    packet = decode_status(raw)
    assert packet.raw == raw
    assert packet.header == raw[:4]
    assert len(packet.unknown_region) == 7  # bytes 19..25


# ── Fan ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("step", "percent"), [(0, 5), (9, 50), (19, 100)])
def test_fan_step_mapping(step: int, percent: int) -> None:
    """RL-002 is unresolved; this pins the hypothesis we shipped so a capture that
    disagrees produces a failing test rather than a silent behaviour change."""
    assert fan_step_to_percent(step) == percent


def test_out_of_range_fan_step_is_flagged() -> None:
    packet = decode_status(build_status(fan_step=42))
    assert packet.fan_step == 42
    assert packet.fan_percent is None
    assert any("fan step" in a for a in packet.anomalies)


# ── Malformed input: must never raise ───────────────────────────────────────────────────


def test_empty_packet() -> None:
    packet = decode_status(b"")
    assert packet.anomalies == ("empty packet",)
    assert packet.actual_temp_c is None


@pytest.mark.parametrize("length", range(0, 11))
def test_truncated_packets_decode_partially_without_raising(length: int) -> None:
    packet = decode_status(build_status()[:length])
    assert not packet.is_plausible
    assert packet.raw == build_status()[:length]


def test_short_packet_still_reports_header() -> None:
    packet = decode_status(build_status()[:6])
    assert packet.header == build_status()[:4]
    assert any("too short" in a for a in packet.anomalies)


def test_unverified_mode_byte_is_preserved_and_flagged() -> None:
    """An unverified status mode must decode to None, never to a plausible guess."""
    packet = decode_status(build_status(mode=0x7F))
    assert packet.mode is None
    assert packet.mode_raw == 0x7F
    assert any("not yet verified" in a for a in packet.anomalies)


def test_command_mode_values_are_not_used_to_decode_status() -> None:
    """RL-012: the two enums differ, and conflating them reported 'turbo' for a cooling unit.

    0x03 is HEAT in the command table. It must NOT decode as a status mode.
    """
    packet = decode_status(build_status(mode=0x03))
    assert packet.mode is None, "command-mode values must not leak into status decoding"


def test_unexpected_packet_format_is_flagged() -> None:
    packet = decode_status(build_status(packet_format=0xAB))
    assert any("unexpected packet format" in a for a in packet.anomalies)


def test_implausible_time_is_flagged() -> None:
    packet = decode_status(build_status(minutes=99, seconds=99))
    assert any("implausible time" in a for a in packet.anomalies)


def test_all_zero_packet_does_not_raise() -> None:
    packet = decode_status(bytes(30))
    assert packet.anomalies  # a zero packet is not plausible, and we should say so
    assert not packet.is_plausible


def test_random_length_garbage_never_raises() -> None:
    for n in range(0, 64):
        decode_status(bytes(range(n)))


# ── Partial packets ─────────────────────────────────────────────────────────────────────


def test_partial_flag_is_detected() -> None:
    assert decode_status(build_status(partial=1)).is_partial


def test_reassembly_concatenates() -> None:
    first = build_status(partial=1)[:20]
    remainder = bytes([0xAA, 0xBB])
    assert reassemble(first, remainder) == first + remainder
