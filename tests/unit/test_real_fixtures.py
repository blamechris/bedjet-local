"""Tests against packets captured from **our** device.

Everything here is ✅ VERIFIED evidence, unlike the synthetic packets in `test_decode.py`
which only prove the decoder does what we told it to. These assert against ground truth
that was set in the vendor app and written down *before* the capture — see
`tests/fixtures/PROVENANCE.md`.

If one of these fails, either the decoder regressed or the firmware changed. Both matter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bedjet_local.device.state import BedJetState, Power
from bedjet_local.protocol.constants import StatusMode
from bedjet_local.protocol.decode import decode_status

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def cool_fan50_target75f() -> bytes:
    """Ground truth, set in the vendor app 2026-08-16: Cool, fan 50%, target 75 F."""
    return (FIXTURES / "cool_fan50_target75f.bin").read_bytes()


def test_header_layout(cool_fan50_target75f: bytes) -> None:
    """RL-004. The header order our first guess got wrong, now pinned to real bytes."""
    packet = decode_status(cool_fan50_target75f)
    assert packet.header == bytes([0x01, 0x56, 0x1B, 0x01])
    assert packet.packet_format == 0x56, "format is byte 1, not byte 2"
    assert packet.packet_type == 0x01
    assert packet.declared_length == 27


def test_declared_length_matches_the_packet(cool_fan50_target75f: bytes) -> None:
    """The arithmetic identity that makes the length byte trustworthy: 27 + 4 == 31."""
    packet = decode_status(cool_fan50_target75f)
    assert len(cool_fan50_target75f) == 31
    assert packet.expected_total == 31
    assert packet.is_complete


def test_target_temperature_matches_what_the_app_was_set_to(cool_fan50_target75f: bytes) -> None:
    """RL-001. Target was set to 75 F in the app; byte 8 is 0x30 = 48 = 24.0 C = 75.2 F."""
    packet = decode_status(cool_fan50_target75f)
    assert packet.target_temp_c == 24.0
    assert packet.target_temp_c is not None
    assert round(packet.target_temp_c * 9 / 5 + 32) == 75


def test_fan_percent_matches_what_the_app_was_set_to(cool_fan50_target75f: bytes) -> None:
    """RL-002 settled. Fan was set to 50%; byte 10 is step 9.

    ESPHome's `5 + 5*step` gives 50. The MQTT bridge's `5*step` gives 45. Ground truth
    says 50, so ESPHome is right and the contest is over.
    """
    packet = decode_status(cool_fan50_target75f)
    assert packet.fan_step == 9
    assert packet.fan_percent == 50


def test_mode_is_cool_not_turbo(cool_fan50_target75f: bytes) -> None:
    """RL-012, the finding that matters most.

    The app was set to **Cool**. Byte 9 reads 0x04, which the *command* table calls turbo.
    Decoding status with the command enum reported "turbo" for a cooling unit. Status and
    command are different enums, and this test exists so nothing merges them again.
    """
    packet = decode_status(cool_fan50_target75f)
    assert packet.mode_raw == 0x04
    assert packet.mode is StatusMode.COOL


def test_time_remaining_decodes(cool_fan50_target75f: bytes) -> None:
    packet = decode_status(cool_fan50_target75f)
    assert packet.time_remaining_s == 9 * 3600 + 59 * 60 + 25


def test_unknown_region_is_retained_verbatim(cool_fan50_target75f: bytes) -> None:
    """Bytes 19-25 mean nothing to us yet. Keeping them lets a future diff attack them."""
    packet = decode_status(cool_fan50_target75f)
    assert packet.unknown_region == bytes([0x12, 0x01, 0x9A, 0x01, 0x10, 0xFF, 0x00])


def test_packet_is_one_byte_longer_than_upstream_documents(cool_fan50_target75f: bytes) -> None:
    """Upstream's layout ends at byte 29. Ours carries a byte 30 (0x31), unaccounted for."""
    assert len(cool_fan50_target75f) == 31
    assert cool_fan50_target75f[30] == 0x31


def test_decodes_without_anomalies(cool_fan50_target75f: bytes) -> None:
    """A real packet from a healthy device should decode clean. If this starts failing,
    the firmware changed or we broke something."""
    packet = decode_status(cool_fan50_target75f)
    assert packet.anomalies == (), f"unexpected anomalies: {packet.anomalies}"
    assert packet.is_plausible


def test_device_state_from_real_packet(cool_fan50_target75f: bytes) -> None:
    state = BedJetState.from_status(decode_status(cool_fan50_target75f), available=True)
    assert state.available is True
    assert state.power is Power.ON
    assert state.mode is StatusMode.COOL
    assert state.fan_percent == 50
    assert state.target_temp_c == 24.0
    assert state.errors == ()
