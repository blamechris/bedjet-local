"""Protocol decoding tests. Pure, synthetic, no hardware, no I/O.

⚠️ These fixtures are **synthetic** — built from the upstream-documented layout, not
captured from our device. They prove the decoder does what we told it to; they do NOT
prove the layout is right. Real captures land in ``tests/fixtures/`` with provenance, and
only then does PROTOCOL.md get to say ✅ VERIFIED.
"""

from __future__ import annotations

import pytest

from bedjet_local.protocol.constants import (
    Mode,
    Offset,
    c_to_f,
    fan_step_to_percent,
    temp_byte_to_c,
)
from bedjet_local.protocol.decode import decode_status, reassemble


def build_status(
    *,
    hours: int = 0,
    minutes: int = 30,
    seconds: int = 0,
    actual: int = 44,  # 22.0 C
    target: int = 50,  # 25.0 C
    mode: int = Mode.HEAT,
    fan_step: int = 9,
    partial: int = 0,
    length: int = 30,
    packet_format: int = 0x56,
    packet_type: int = 0x01,
    trailing: bytes = b"",
) -> bytes:
    """Synthesise a status packet from the hypothesised layout."""
    data = bytearray(30)
    data[Offset.LENGTH] = length
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
    data[Offset.AMBIENT_TEMP] = 42
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
    assert any("outside documented range" in a for a in packet.anomalies)


# ── Happy path ──────────────────────────────────────────────────────────────────────────


def test_decodes_a_well_formed_packet() -> None:
    packet = decode_status(build_status())
    assert packet.anomalies == ()
    assert packet.is_plausible
    assert packet.actual_temp_c == 22.0
    assert packet.target_temp_c == 25.0
    assert packet.mode is Mode.HEAT
    assert packet.fan_step == 9
    assert packet.fan_percent == 50
    assert packet.time_remaining_s == 1800
    assert not packet.is_partial


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


def test_unknown_mode_byte_is_preserved_and_flagged() -> None:
    packet = decode_status(build_status(mode=0x7F))
    assert packet.mode is None
    assert packet.mode_raw == 0x7F
    assert any("unknown mode" in a for a in packet.anomalies)


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
