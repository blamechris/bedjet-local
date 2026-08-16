"""Wire-faithful representations of BedJet packets.

A ``StatusPacket`` describes *what the wire said*, not what the device means. Semantic
interpretation (is it available? is it on?) belongs in ``device/``. Keeping these apart is
what lets us change our minds about semantics without re-capturing fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import Mode


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
    """Upstream reports the V3 status packet exceeds the notification MTU and arrives in
    two parts, the first flagged partial and the remainder fetched by an explicit read.
    📖 UPSTREAM — unverified on our device."""

    packet_format: int | None = None
    packet_type: int | None = None

    time_remaining_s: int | None = None
    actual_temp_c: float | None = None
    target_temp_c: float | None = None
    ambient_temp_c: float | None = None
    mode: Mode | None = None
    mode_raw: int | None = None
    """The raw mode byte, kept even when it maps to no known :class:`Mode`."""
    fan_step: int | None = None
    fan_percent: int | None = None

    min_temp_c: float | None = None
    max_temp_c: float | None = None
    turbo_time_s: int | None = None
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
    def is_plausible(self) -> bool:
        """Whether this packet looks like a real V3 status packet.

        Deliberately weak — it exists to catch "we subscribed to the wrong characteristic",
        not to validate protocol correctness.
        """
        return not self.anomalies and self.actual_temp_c is not None
