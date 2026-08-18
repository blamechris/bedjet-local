"""The write path. The only module outside ``transport/`` permitted to put bytes on the wire.

**A command is not "write and hope".** The BedJet has no acknowledgement, so a GATT write
that returns cleanly proves only that the radio accepted the bytes. RL-018 is the proof: a
write that logged perfectly was silently discarded by the local Bluetooth stack and never
reached the device at all. So every command runs the same loop:

    read state  →  check the write would be observable  →  write  →  read state back
                →  assert the state actually changed as intended

with both the baseline and the confirmation required to come from a packet that **passed its
checksum** — RL-017, where a corrupt fragment reported a heater as switched off.

**Commands are verified one at a time**, in increasing order of consequence
(`docs/SAFETY.md`), and each is added here only after the previous one is proven on hardware:

- ✅ ``send_off`` — `01 01`, VERIFIED (RL-019, reproduced RL-020)
- ✅ ``set_fan_percent`` — `07 <step>`, VERIFIED (RL-020)
- ✅ ``set_mode`` — `01 <mode>`, COOL VERIFIED (RL-021); restricted to the **thermally safe**
  operands, with the modes that make heat refused in code rather than by convention
- ✅ ``set_temperature`` — `03 <temp>`, VERIFIED (RL-022)
- 📖 ``set_mode(HEAT)`` — unlocked once OFF was proven reproducible; see :data:`UNLOCKED_MODES`
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..device.state import BedJetState, Power
from ..protocol import encode
from ..protocol.constants import (
    COMMAND_UUID,
    CommandMode,
    StatusMode,
    c_to_f,
)
from ..protocol.packets import StatusPacket
from ..transport.base import Transport, TransportError
from .reader import StateCallback, StatusReader

log = logging.getLogger(__name__)


#: Mode operands this module is permitted to send.
#:
#: **HEAT was unlocked once the preconditions for it were met** — not because the risk
#: assessment changed, but because it was discharged. The danger was never "the device makes
#: heat": that is its job, under its own thermostat and its own hardware thermal protection,
#: neither of which we touch. The danger was that our *stack* was unverified — if the decoder
#: were wrong we could not tell what the device was doing, and RL-017 proved that concern
#: justified by reporting a running heater as switched off.
#:
#: What changed:
#:   - four commands verified end-to-end against observed state
#:   - state gated on a checksum, so a corrupt packet can no longer masquerade as truth
#:   - **OFF is verified and reproducible** — a proven software stop is the single best
#:     precondition for testing heat, and we did not have it before
#:   - targets are bounded by the range the device reports for its current mode
#:
#: TURBO and EXTENDED_HEAT stay locked. Turbo is the device's most aggressive setting (a
#: fixed 43 °C for 10 minutes) and nothing needs it. Extended heat is worse than untested:
#: status ``0x03`` has never been observed, so its result could not be verified even if the
#: command worked — and :meth:`Commander.set_mode` refuses it on those grounds independently
#: of this set.
UNLOCKED_MODES = frozenset(
    {CommandMode.OFF, CommandMode.COOL, CommandMode.DRY, CommandMode.HEAT},
)

#: Alias kept so nothing that imported the previous name breaks silently.
THERMALLY_SAFE_MODES = UNLOCKED_MODES


class CommandRefused(Exception):
    """The command was not sent, and the reason is not a device failure."""


class AlreadySatisfied(CommandRefused):
    """The device was already in the requested state, so nothing was sent.

    Still a refusal — no bytes went out, and the caller must not be told the command was
    verified, because nothing was verified. But it is the *benign* refusal, and an API
    needs to tell it apart from the others structurally rather than by reading the message:
    "turn it off" against an already-off heater is a satisfied request, whereas "the
    baseline failed its checksum" is a fault. Both would otherwise be one exception type.
    """


class CommandUnverified(Exception):
    """The command was sent but the device did not visibly do it.

    Deliberately distinct from a transport error. This is the dangerous case: bytes went
    out, nothing observable happened, and we do not know whether the device ignored them,
    misunderstood them, or did something we are not looking at.
    """


#: Which **status** mode a given **command** mode should produce.
#:
#: This mapping is the whole RL-014 finding in one table, and it is why verification cannot
#: just compare a command byte to a status byte: they are different enums, and every value
#: they share means something different in each. Command COOL is `0x02`; the cooling unit
#: reports status `0x04`.
_EXPECTED_STATUS_FOR = {
    CommandMode.OFF: StatusMode.STANDBY,
    CommandMode.COOL: StatusMode.COOL,
    CommandMode.DRY: StatusMode.DRY,
    CommandMode.HEAT: StatusMode.HEAT,
}


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
        on_state: optional observer, called with every trustworthy state this commander
            sees. It exists so that a long-running session can publish state without
            opening a *second* subscription to the same characteristic — which would put
            two follow-up reads in flight against one handle, the precise collision that
            produced the corrupt fragment in RL-017.
        poll_interval: passed to the reader. ``None`` (the default) keeps the verified
            notification path; a value selects whole-packet polling (#2 — hypothesis
            until measured on hardware).
    """

    def __init__(
        self,
        transport: Transport,
        *,
        settle_timeout: float = 10.0,
        on_state: StateCallback | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._transport = transport
        self._settle_timeout = settle_timeout
        self._observer = on_state
        self._latest: tuple[BedJetState, StatusPacket] | None = None
        self._updated = asyncio.Event()
        self._reader = StatusReader(transport, self._on_state, poll_interval=poll_interval)

    @property
    def reader(self) -> StatusReader:
        """The underlying reader, for its counters. Read-only by construction."""
        return self._reader

    def _on_state(self, state: BedJetState, packet: StatusPacket) -> None:
        # The reader already drops untrustworthy packets, so anything arriving here has
        # passed its checksum. Both layers check anyway: this one guards a physical action.
        self._latest = (state, packet)
        self._updated.set()
        if self._observer is not None:
            # An observer that raises must not break command verification: this callback
            # runs on the notification path, and the packet it is being handed is the same
            # one a pending command is waiting on.
            try:
                self._observer(state, packet)
            except Exception:
                log.exception("a state observer raised; continuing")

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

    async def _send_verified(
        self,
        payload: bytes,
        *,
        description: str,
        satisfied: Callable[[BedJetState], bool],
        dry_run: bool = False,
    ) -> CommandResult:
        """Write ``payload`` and prove the device did it.

        Args:
            payload: the exact bytes to write.
            description: what we are asking for, used in every message.
            satisfied: given a decoded state, has the device done it? Called only on states
                built from packets that passed their checksum.
        """
        before_state, before_packet = await self.wait_for_state(self._settle_timeout)

        if not before_packet.is_trustworthy:
            raise CommandRefused(
                "the current state came from a packet that failed its checksum, so there is "
                "no trustworthy baseline to verify against. Retry — and if it persists, the "
                "link is too poor to command the device safely."
            )

        if satisfied(before_state):
            raise AlreadySatisfied(
                f"the device already satisfies '{description}', so the write would be "
                f"unobservable — and an unobservable command teaches us nothing while still "
                f"being a write to a heater. Change the device first, then retry."
            )

        if dry_run:
            log.warning("DRY RUN: would write %s to %s", payload.hex(" "), COMMAND_UUID)
            return CommandResult(
                before=before_state,
                after=before_state,
                payload=payload,
                before_packet=before_packet,
                after_packet=before_packet,
            )

        # Loud on purpose: this line is the record of what we asked a heater to do.
        log.warning("WRITING %s to %s — %s", payload.hex(" "), COMMAND_UUID, description)
        self._updated.clear()
        self._latest = None
        try:
            # response=None: the transport picks the write type from the characteristic's own
            # properties. Pinning it wrong is how RL-018 threw every command away.
            await self._transport.write(COMMAND_UUID, payload)
        except TransportError as exc:
            # From this point on, "the link failed" and "the device was written to" are not
            # exclusive: a write request can be delivered and its confirmation lost. That is
            # the epistemic state CommandUnverified exists for — reporting it as a link
            # problem would tell the caller "nothing was sent" about a write that may have
            # landed (ADR-0004 decision 4).
            raise CommandUnverified(
                f"the link failed during the write of {payload.hex(' ')} ({description}): "
                f"{exc}.\n"
                f"The request may or may not have reached the device — a write can be "
                f"delivered even when the link dies before its confirmation. Read the "
                f"state once the link returns before deciding anything, and do not "
                f"blind-retry."
            ) from exc
        # In poll mode this pulls the next read forward so verification sees the device's
        # reaction in one round-trip rather than at the next tick; under notifications it
        # is a no-op, since the device announces its own changes.
        self._reader.poke()

        deadline = asyncio.get_running_loop().time() + self._settle_timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=0.5)
            except TimeoutError:
                # A quiet half-second while a command settles is worth a fresh sample:
                # the device is not obliged to reflect a command within one read, and a
                # poll interval longer than the settle window would otherwise report a
                # command the device obeyed as unverified. No-op under notifications.
                self._reader.poke()
                continue
            self._updated.clear()
            assert self._latest is not None
            after_state, after_packet = self._latest
            # RL-017: gate on integrity, not just on fields. A corrupt fragment once
            # decoded to mode=standby and was accepted as proof a heater had switched off.
            if not after_packet.is_trustworthy:
                log.warning("ignoring an untrustworthy packet while verifying")
                continue
            if satisfied(after_state):
                return CommandResult(
                    before=before_state,
                    after=after_state,
                    payload=payload,
                    before_packet=before_packet,
                    after_packet=after_packet,
                )

        observed = self._latest[0].describe() if self._latest else "no status at all"
        raise CommandUnverified(
            f"wrote {payload.hex(' ')} but the device did not satisfy '{description}' within "
            f"{self._settle_timeout:.0f}s. Observed: {observed}.\n"
            f"The bytes were accepted by the radio, which proves only that — there is no "
            f"acknowledgement in this protocol.\n"
            f"**If the unit is running and you want it off, use the vendor app or unplug it.**"
        )

    async def send_off(self, *, dry_run: bool = False) -> CommandResult:
        """✅ VERIFIED (RL-019). `01 01` — switch the unit off."""
        return await self._send_verified(
            encode.turn_off(),
            description="mode -> off",
            satisfied=lambda state: state.mode is StatusMode.STANDBY or state.power is Power.OFF,
            dry_run=dry_run,
        )

    async def set_mode(self, mode: CommandMode, *, dry_run: bool = False) -> CommandResult:
        """`01 <mode>` — select a mode. COOL ✅ VERIFIED (RL-021); HEAT unlocked but 📖.

        Only :data:`UNLOCKED_MODES` may be sent. Turbo and extended heat are refused here —
        in code — because turbo is the device's most aggressive setting and nothing needs
        it, and extended heat has never been observed in status, so its result could not be
        verified even if it worked. Heat was unlocked once OFF was proven reproducible,
        respecting `SAFETY.md`'s ordering (heat last, attended).
        """
        if mode not in UNLOCKED_MODES:
            raise CommandRefused(
                f"{mode.name} is not in UNLOCKED_MODES. Turbo is the device's most "
                f"aggressive setting and nothing needs it; extended heat has never been "
                f"observed in status, so its result could not be verified even if it worked. "
                f"Unlock deliberately, in its own commit, with a reason."
            )

        expected = _EXPECTED_STATUS_FOR.get(mode)
        if expected is None:
            raise CommandRefused(
                f"no verified status mode corresponds to command {mode.name}, so the result "
                f"could not be verified even if it worked. Capture that mode from the vendor "
                f"app first (RL-012)."
            )

        return await self._send_verified(
            encode.set_mode(mode),
            description=f"mode -> {mode.name.lower()}",
            satisfied=lambda state: state.mode is expected,
            dry_run=dry_run,
        )

    async def set_temperature(self, celsius: float, *, dry_run: bool = False) -> CommandResult:
        """📖 UNVERIFIED. `03 <temp>` — set the target temperature.

        The permitted range comes from the **live status packet** (bytes 13-14), never from a
        constant: RL-013 established that it moves with the mode, and that turbo's 43 °C
        exceeds the 104 °F every public source calls the device maximum. Asking the device
        what it accepts is both safer and more correct than any table we could write.

        The wire encoding has 0.5 °C granularity, so the value actually sent may differ
        slightly from the one requested. The rounded value is what gets verified, and callers
        can read it back from the result — a request that quietly becomes a different
        temperature is exactly the sort of thing that should be visible on a heater.
        """
        before_state, before_packet = await self.wait_for_state(self._settle_timeout)

        if before_state.power is not Power.ON:
            raise CommandRefused(
                "the unit is not running, so a target change may not be observable. Start it "
                "with `bedjet mode <address> cool` first."
            )
        if before_packet.min_temp_c is None or before_packet.max_temp_c is None:
            raise CommandRefused(
                "the device did not report its permitted range in the current status packet, "
                "so there is nothing to bound the request against. Retry."
            )

        # Round to the wire's granularity BEFORE validating, so the value we check is the
        # value we send.
        rounded = encode.snap_target_c(celsius)
        payload = encode.set_temperature(
            rounded, min_c=before_packet.min_temp_c, max_c=before_packet.max_temp_c
        )
        return await self._send_verified(
            payload,
            description=f"target -> {rounded:.1f}C ({c_to_f(rounded):.1f}F)",
            satisfied=lambda state: state.target_temp_c == rounded,
            dry_run=dry_run,
        )

    async def set_fan_percent(self, percent: int, *, dry_run: bool = False) -> CommandResult:
        """📖 UNVERIFIED. `07 <step>` — set fan speed.

        Command #2 by the bring-up order: thermally inert, directly observable in status
        byte 10, and it exercises a **different opcode** from OFF — which is the point.
        OFF verified the `01` opcode only.

        Refuses unless the unit is running: fan speed on a standby unit is not observable
        (the byte holds its last-set value regardless — RL-013).

        The wire has 5% granularity, so the value actually sent may differ from the one
        requested. As with temperature, the snapped value is what gets verified and what
        the command description reports — verifying the raw request would report
        unverified for a command the device obeyed (#23).
        """
        before_state, _ = await self.wait_for_state(self._settle_timeout)
        if before_state.power is not Power.ON:
            raise CommandRefused(
                "the unit is not running, so a fan change would not be observable — the fan "
                "byte holds its last-set value in standby (RL-013). Start it in Cool from "
                "the vendor app, close the app, and retry."
            )
        snapped = encode.snap_fan_percent(percent)
        return await self._send_verified(
            encode.set_fan_percent(snapped),
            description=f"fan -> {snapped}%",
            satisfied=lambda state: state.fan_percent == snapped,
            dry_run=dry_run,
        )
