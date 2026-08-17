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


# ── Cross-mode fixtures (RL-013) ────────────────────────────────────────────────────────


@pytest.fixture
def off_standby() -> bytes:
    """Ground truth: unit off/idle, set via the vendor app 2026-08-16."""
    return (FIXTURES / "off_standby.bin").read_bytes()


@pytest.fixture
def off_after_heating() -> bytes:
    """Off, captured shortly after a heat session — outlet still at 32.0 C."""
    return (FIXTURES / "off_after_heating.bin").read_bytes()


@pytest.fixture
def turbo() -> bytes:
    """Ground truth: Turbo, set via the vendor app 2026-08-16."""
    return (FIXTURES / "turbo_fan100_target109f.bin").read_bytes()


ALL_FIXTURES = [
    "cool_fan50_target75f.bin",
    "off_standby.bin",
    "off_after_heating.bin",
    "turbo_fan100_target109f.bin",
    "heat_fan100_target89f.bin",
    "dry_fan100.bin",
]

#: dry_fan100 legitimately carries an anomaly — the device reports a target below its own
#: stated minimum for that mode (RL-015). Listed here rather than silenced, so the anomaly
#: stays visible and has to be justified.
FIXTURES_WITH_EXPECTED_ANOMALIES = {"dry_fan100.bin"}


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_real_packet_passes_its_checksum(name: str) -> None:
    """RL-013. The final byte makes the packet sum to zero mod 256 — undocumented upstream.

    Holding across four packets and three modes is what turns this from a coincidence into
    a protocol fact. It also proves reassembly is correct: a mis-joined packet would fail.
    """
    packet = decode_status((FIXTURES / name).read_bytes())
    assert packet.checksum_ok is True, f"{name} failed its checksum"


@pytest.mark.parametrize("name", sorted(set(ALL_FIXTURES) - FIXTURES_WITH_EXPECTED_ANOMALIES))
def test_every_real_packet_decodes_clean(name: str) -> None:
    packet = decode_status((FIXTURES / name).read_bytes())
    assert packet.anomalies == (), f"{name}: {packet.anomalies}"


def test_standby_mode_is_zero(off_standby: bytes) -> None:
    """RL-013: 0x00 is standby. This is what lets Power stop answering UNKNOWN."""
    packet = decode_status(off_standby)
    assert packet.mode is StatusMode.STANDBY
    assert packet.time_remaining_s == 0
    assert packet.max_runtime_s == 0

    state = BedJetState.from_status(packet, available=True)
    assert state.power is Power.OFF


def test_turbo_mode_is_two_not_four(turbo: bytes) -> None:
    """The command table calls 0x02 *cool* and 0x04 *turbo*. Status has them the other way
    round — the coincidence that makes a wrong decode look plausible."""
    packet = decode_status(turbo)
    assert packet.mode_raw == 0x02
    assert packet.mode is StatusMode.TURBO
    assert BedJetState.from_status(packet).power is Power.ON


def test_turbo_target_exceeds_the_supposed_device_maximum(turbo: bytes) -> None:
    """43.0 C is 109.4 F — above the 104 F every public source calls the maximum, and it
    decodes without an anomaly because the device reports that bound itself."""
    packet = decode_status(turbo)
    assert packet.target_temp_c == 43.0
    assert packet.min_temp_c == 43.0
    assert packet.max_temp_c == 43.0
    assert packet.anomalies == ()


def test_permitted_range_moves_with_the_mode(
    off_standby: bytes, cool_fan50_target75f: bytes, turbo: bytes
) -> None:
    """Bytes 13-14 are a per-mode permitted range, not fixed device limits."""
    assert (decode_status(off_standby).min_temp_c, decode_status(off_standby).max_temp_c) == (
        10.0,
        40.0,
    )
    cool = decode_status(cool_fan50_target75f)
    assert (cool.min_temp_c, cool.max_temp_c) == (19.0, 26.0)
    hot = decode_status(turbo)
    assert (hot.min_temp_c, hot.max_temp_c) == (43.0, 43.0)


def test_max_runtime_moves_with_the_mode(
    off_standby: bytes, cool_fan50_target75f: bytes, turbo: bytes
) -> None:
    assert decode_status(off_standby).max_runtime_s == 0
    assert decode_status(cool_fan50_target75f).max_runtime_s == 12 * 3600
    assert decode_status(turbo).max_runtime_s == 10 * 60


def test_turbo_elapsed_reconstructs_the_runtime(turbo: bytes) -> None:
    """elapsed + remaining == the turbo limit, which is what identifies bytes 15-16."""
    packet = decode_status(turbo)
    assert packet.turbo_elapsed_s == 13
    assert packet.time_remaining_s == 9 * 60 + 47
    assert packet.turbo_elapsed_s + packet.time_remaining_s == packet.max_runtime_s == 600


def test_fan_reads_stale_when_the_unit_is_off(off_standby: bytes) -> None:
    """The fan byte holds the last-set value in standby, so state must not present it as
    live airflow."""
    state = BedJetState.from_status(decode_status(off_standby))
    assert state.power is Power.OFF
    assert state.fan_percent == 50
    assert "last set" in state.describe()


def test_bytes_19_to_25_are_invariant_across_every_mode() -> None:
    """Constant across all captures and all modes, so this region is not state — it is
    configuration or identity. The firmware version is the leading candidate (RL-013)."""
    regions = {decode_status((FIXTURES / n).read_bytes()).unknown_region for n in ALL_FIXTURES}
    assert regions == {bytes([0x12, 0x01, 0x9A, 0x01, 0x10, 0xFF, 0x00])}


# ── Heat (RL-014) ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def heat() -> bytes:
    """Ground truth: Heat, fan 100%, target 89 F, timer running — vendor app, 2026-08-16."""
    return (FIXTURES / "heat_fan100_target89f.bin").read_bytes()


def test_heat_mode_is_one(heat: bytes) -> None:
    """RL-014. Completes four of the status-mode values: 0=standby 1=heat 2=turbo 4=cool."""
    packet = decode_status(heat)
    assert packet.mode_raw == 0x01
    assert packet.mode is StatusMode.HEAT
    assert packet.anomalies == ()
    assert BedJetState.from_status(packet).power is Power.ON


def test_heat_was_actually_running(heat: bytes) -> None:
    """The previous attempt captured a stopped unit. This one has a live timer, which is
    what makes it a heat fixture rather than a standby one."""
    packet = decode_status(heat)
    assert packet.time_remaining_s == 29 * 60 + 40
    assert packet.max_runtime_s == 12 * 3600
    assert packet.target_temp_c == 31.5


def test_heat_has_its_own_permitted_range(heat: bytes) -> None:
    """Heat: 22.5-40.0 C (72.5-104.0 F) — different again from standby, cool and turbo."""
    packet = decode_status(heat)
    assert (packet.min_temp_c, packet.max_temp_c) == (22.5, 40.0)


def test_predicted_modes_are_not_decoded() -> None:
    """A prediction must stay a prediction until a capture confirms it.

    RL-014 predicted 0x03 = extended heat and 0x05 = dry. RL-015 captured dry and confirmed
    0x05, so DRY is now a verified member. 0x03 has still never been observed and must not
    be decoded — a guess that decodes silently is indistinguishable from a fact, and one
    correct prediction does not license the next.
    """
    assert 0x05 in {m.value for m in StatusMode}, "dry was confirmed by capture"
    assert 0x03 not in {m.value for m in StatusMode}, "extended heat is still only predicted"


# ── Dry (RL-015) ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dry() -> bytes:
    """Ground truth: Dry, fan 100%, timer running — vendor app, 2026-08-16."""
    return (FIXTURES / "dry_fan100.bin").read_bytes()


def test_dry_mode_is_five_as_predicted(dry: bytes) -> None:
    """RL-015. The ordering hypothesis predicted 0x05 before this capture existed, and the
    capture confirmed it. A prediction that survives a real test is worth recording as such."""
    packet = decode_status(dry)
    assert packet.mode_raw == 0x05
    assert packet.mode is StatusMode.DRY
    assert BedJetState.from_status(packet).power is Power.ON


def test_dry_reports_a_target_below_its_own_minimum(dry: bytes) -> None:
    """RL-015, an unexplained one.

    Dry reports a permitted range of 24.0-31.0 C and a target of 22.0 C — *below* its own
    stated minimum. Every other mode's target sits inside its range. The target also happens
    to equal the ambient reading in the same packet, which may or may not be a coincidence.

    The anomaly is asserted rather than suppressed: the decoder is right to complain, and
    the complaint is the finding.
    """
    packet = decode_status(dry)
    assert packet.target_temp_c == 22.0
    assert (packet.min_temp_c, packet.max_temp_c) == (24.0, 31.0)
    assert packet.ambient_temp_c == packet.target_temp_c
    assert any("outside the range the device reports" in a for a in packet.anomalies)


def test_extended_heat_remains_unverified() -> None:
    """0x03 has never been observed. One correct prediction does not license a second."""
    assert 0x03 not in {m.value for m in StatusMode}
