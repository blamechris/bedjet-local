"""Wire-faithful representations of BedJet packets.

A ``StatusPacket`` describes *what the wire said*, not what the device means. Semantic
interpretation (is it available? is it on?) belongs in ``device/``. Keeping these apart is
what lets us change our minds about semantics without re-capturing fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import HEADER_LENGTH, StatusMode


@dataclass(frozen=True, slots=True)
class StatusPacket:
    """A decoded BedJet status packet.

    Every field is optional: a truncated or unexpected packet decodes as far as it can and
    records the rest in :attr:`anomalies` rather than raising. A heater that sends us one
    odd packet should not take the daemon down.
    """

    raw: bytes
    """The exact bytes decoded, retained so any field can be re-derived later."""

    header: bytes
    """Bytes 0-3 verbatim. The header's internal layout is a hypothesis (RL-004); keeping
    it raw means a capture can correct our interpretation without a re-capture."""

    is_partial: bool = False
    """Byte 0 of the header. ✅ VERIFIED that the split-packet mechanism is real: our first
    capture arrived as a notification plus a follow-up read and reassembled to exactly the
    length the header declared.

    **Not load-bearing.** After reassembly this flag still reads from the first fragment,
    so it stays set on a complete packet. Use :attr:`is_complete`, which compares the
    declared length against what we actually have."""

    packet_format: int | None = None
    packet_type: int | None = None

    declared_length: int | None = None
    """Header byte 2: payload bytes following the 4-byte header. ✅ VERIFIED (RL-004) —
    ``27 + 4 == 31``, the exact size of the reassembled capture. This is the authority on
    whether a packet is whole."""

    time_remaining_s: int | None = None
    actual_temp_c: float | None = None
    target_temp_c: float | None = None
    ambient_temp_c: float | None = None
    mode: StatusMode | None = None
    mode_raw: int | None = None
    """The raw mode byte, kept even when it maps to no known :class:`StatusMode` — which is
    most of the time, since only ``COOL`` has been verified so far."""
    fan_step: int | None = None
    fan_percent: int | None = None

    min_temp_c: float | None = None
    """Lowest target the device permits **in the current mode** (byte 13), not a fixed
    device limit. ✅ VERIFIED across three modes (RL-013)."""

    max_temp_c: float | None = None
    """Highest target the device permits **in the current mode** (byte 14). Turbo reports
    43.0 C — above the 104 F every public source calls the maximum."""

    max_runtime_s: int | None = None
    """Longest run the device permits in the current mode. Standby 0, cool 12 h, turbo
    10 min. ✅ VERIFIED (RL-013)."""

    turbo_elapsed_s: int | None = None
    """Counts up once per second while in turbo, zero otherwise. ✅ VERIFIED (RL-013)."""

    checksum_ok: bool | None = None
    """Whether the packet sums to zero mod 256. ``None`` when the packet is too short to
    tell. ✅ VERIFIED (RL-013) — undocumented by every upstream source."""
    shutdown_reason: int | None = None
    update_phase: int | None = None
    flags: int | None = None
    sequence_step: int | None = None
    notify_code: int | None = None

    unknown_region: bytes = b""
    """Bytes 19-25, meaning unknown. Retained verbatim so that diffing packets across
    device states can attack them without sending a single write."""

    anomalies: tuple[str, ...] = field(default_factory=tuple)
    """Everything that did not look as expected. Never empty-by-suppression: if a value is
    outside its documented range we surface it, we do not silently clamp an observation."""

    @property
    def expected_total(self) -> int | None:
        """Full packet size the header declares, header included."""
        if self.declared_length is None:
            return None
        return self.declared_length + HEADER_LENGTH

    @property
    def is_complete(self) -> bool:
        """Whether we hold the whole packet.

        The length byte decides this, not the partial flag. A flag we half understand is a
        worse authority than an arithmetic identity the device itself asserts.
        """
        total = self.expected_total
        if total is None:
            return False
        return len(self.raw) >= total

    @property
    def is_trustworthy(self) -> bool:
        """Whether this packet's fields may be believed.

        **Anything that acts on device state must gate on this.** A packet that fails its
        checksum is not "a packet with some odd values" — its fields are meaningless, and
        reading them is worse than having no reading at all.

        Learned the hard way (RL-017): a mis-delivered 11-byte tail fragment decoded to
        ``power=off mode=standby``, purely because its tenth byte happened to be zero, and
        was accepted as proof that a heater had switched off. The checksum that would have
        caught it existed and was simply not consulted.
        """
        return self.checksum_ok is True and self.is_complete

    @property
    def is_plausible(self) -> bool:
        """Whether this packet looks like a real V3 status packet.

        Now backed by a real integrity check rather than a smell test: a packet whose
        checksum fails is not plausible regardless of how sensible its fields look.
        """
        if self.checksum_ok is False:
            return False
        return not self.anomalies and self.actual_temp_c is not None
