"""The write path. Currently capable of exactly one command: OFF.

This is the only module outside ``transport/`` permitted to put bytes on the wire, and
``tests/unit/test_layering.py`` enforces both that and the fact that OFF is the only command
it can construct.

**Why a whole module for one command.** RL-016: every command byte is unverified upstream
guesswork, from a source that has already been caught conflating two enums. A write that
returns without error proves nothing — the BedJet has no acknowledgement, so "success" at the
GATT layer means only that the radio accepted the bytes. The device might have ignored them,
or done something else entirely.

So a command is not "write and hope". It is:

    read state  →  check the write would be observable  →  write  →  read state back
                →  assert the state actually changed as intended

That loop is what turns an unverified byte into a verified one, and it is the whole point of
this module. It is also why OFF is first: it is the only command whose failure mode is a
device that keeps doing what it was already doing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..device.state import BedJetState, Power
from ..protocol import encode
from ..protocol.constants import COMMAND_UUID, StatusMode
from ..protocol.packets import StatusPacket
from ..transport.base import Transport
from .reader import StatusReader

log = logging.getLogger(__name__)


class CommandRefused(Exception):
    """The command was not sent, and the reason is not a device failure."""


class CommandUnverified(Exception):
    """The command was sent but the device did not visibly do it.

    Deliberately distinct from a transport error. This is the dangerous case: bytes went
    out, nothing observable happened, and we do not know whether the device ignored them,
    misunderstood them, or did something we are not looking at.
    """


@dataclass
class CommandResult:
    before: BedJetState
    after: BedJetState
    payload: bytes
    before_packet: StatusPacket
    after_packet: StatusPacket


class Commander:
    """Sends OFF, and proves it worked.

    Args:
        transport: a connected transport.
        settle_timeout: how long to wait for the device to report the new state.
    """

    def __init__(self, transport: Transport, *, settle_timeout: float = 10.0) -> None:
        self._transport = transport
        self._settle_timeout = settle_timeout
        self._latest: tuple[BedJetState, StatusPacket] | None = None
        self._updated = asyncio.Event()
        self._reader = StatusReader(transport, self._on_state)

    def _on_state(self, state: BedJetState, packet: StatusPacket) -> None:
        # The reader already drops untrustworthy packets, so anything arriving here has
        # passed its checksum. Both layers check anyway: this one guards a physical action.
        self._latest = (state, packet)
        self._updated.set()

    async def start(self) -> None:
        await self._reader.start()

    async def stop(self) -> None:
        await self._reader.stop()

    async def wait_for_state(self, timeout: float) -> tuple[BedJetState, StatusPacket]:
        """Wait for a status packet. Raises on silence.

        Silence is not treated as "unchanged": the device is near-silent when off, so an
        absent packet is genuinely ambiguous and must not be read as evidence either way.
        """
        if self._latest is not None:
            return self._latest
        self._updated.clear()
        try:
            await asyncio.wait_for(self._updated.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise CommandRefused(
                f"no status packet in {timeout:.0f}s, so there is nothing to compare against. "
                f"The BedJet is near-silent when off — set it running from the vendor app "
                f"first, then close the app and retry."
            ) from exc
        assert self._latest is not None
        return self._latest

    async def send_off(self, *, dry_run: bool = False) -> CommandResult:
        """Send OFF and verify the device actually went to standby.

        Refuses if the unit is already in standby: the write would then be unobservable, and
        an unobservable command teaches us nothing while still being a write to a heater.
        """
        before_state, before_packet = await self.wait_for_state(self._settle_timeout)

        if not before_packet.is_trustworthy:
            raise CommandRefused(
                "the current state came from a packet that failed its checksum, so there is "
                "no trustworthy baseline to verify against. Retry — and if it persists, the "
                "link is too poor to command the device safely."
            )

        if before_state.mode is StatusMode.STANDBY or before_state.power is Power.OFF:
            raise CommandRefused(
                "the unit is already in standby, so sending OFF would prove nothing. "
                "Set it running (Cool is the safest) from the vendor app, close the app, "
                "and retry — the point of the first write is an observable change."
            )

        payload = encode.turn_off()

        if dry_run:
            log.warning("DRY RUN: would write %s to %s", payload.hex(" "), COMMAND_UUID)
            return CommandResult(
                before=before_state,
                after=before_state,
                payload=payload,
                before_packet=before_packet,
                after_packet=before_packet,
            )

        # The moment of truth. Loud on purpose: this line is the record of the first time
        # this project asked a physical heater to do something.
        log.warning(
            "WRITING %s to %s — first command to the device", payload.hex(" "), COMMAND_UUID
        )
        self._updated.clear()
        self._latest = None
        await self._transport.write(COMMAND_UUID, payload)

        deadline = asyncio.get_running_loop().time() + self._settle_timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=0.5)
            except TimeoutError:
                continue
            self._updated.clear()
            assert self._latest is not None
            after_state, after_packet = self._latest
            # RL-017: gate on the packet's integrity, not just its fields. A corrupt
            # 11-byte fragment once decoded to mode=standby — because its tenth byte
            # happened to be zero — and was accepted as proof a heater had switched off.
            # The reader now discards untrustworthy packets, and this is the second lock on
            # the same door: verification of a physical action requires a packet that
            # passed its own checksum.
            if not after_packet.is_trustworthy:
                log.warning("ignoring an untrustworthy packet while verifying")
                continue
            if after_state.mode is StatusMode.STANDBY:
                return CommandResult(
                    before=before_state,
                    after=after_state,
                    payload=payload,
                    before_packet=before_packet,
                    after_packet=after_packet,
                )

        observed = self._latest[0].describe() if self._latest else "no status at all"
        raise CommandUnverified(
            f"wrote {payload.hex(' ')} but the device did not report standby within "
            f"{self._settle_timeout:.0f}s. Observed: {observed}.\n"
            f"The bytes were accepted by the radio, which proves only that — there is no "
            f"acknowledgement in this protocol. Either the command table is wrong (it is "
            f"unverified upstream guesswork, see RL-016), or the device ignored it.\n"
            f"**If the unit is still running and you want it off, use the vendor app or "
            f"unplug it.**"
        )
