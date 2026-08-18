"""The stable local interface. Every adapter goes through this and nothing else.

The contract, in three parts:

1. **Strings and numbers in, strings and numbers out.** ``set_mode("cool")``, not
   ``set_mode(CommandMode.COOL)``. An adapter that needs an enum from ``protocol/`` has
   leaked, and ADR-0002 says the fix is to widen this module rather than to import
   downward. ``tests/unit/test_layering.py`` enforces that on ``integrations/``.

2. **Three failure modes, kept apart, because they need different responses.**
   :class:`Refused` means nothing was sent — retrying unchanged will refuse again.
   :class:`Unavailable` means the link is down — retry later.
   :class:`Unverified` means **bytes went out and the device did not visibly obey**, which
   is the one an operator has to see. It is not a synonym for failure: the write may have
   worked and the confirmation may have been lost. On a heater, "we told it something and
   cannot tell you what happened" is its own category and must never be flattened into a
   generic 500.

3. **A satisfied request is not a failure.** Asking an off heater to switch off returns
   ``ok=True, changed=False`` rather than raising, because an idempotent API is the only
   kind an automation can drive. But ``sent`` is null, and the reason matters: nothing was
   written, so nothing was verified, and the outcome says so rather than implying a
   confirmation that never happened.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..protocol import encode
from ..protocol.constants import ABSOLUTE_MAX_C, ABSOLUTE_MIN_C, CommandMode
from ..service.commander import (
    UNLOCKED_MODES,
    AlreadySatisfied,
    CommandRefused,
    CommandResult,
    CommandUnverified,
)
from ..service.session import DeviceSession, LinkState
from ..transport.base import TransportError
from .models import (
    FAN_PERCENT_MAX,
    FAN_PERCENT_MIN,
    FAN_PERCENT_STEP,
    TEMPERATURE_STEP_C,
    Capabilities,
    CommandOutcome,
    DeviceSnapshot,
)

log = logging.getLogger(__name__)

SnapshotListener = Callable[[DeviceSnapshot], None]


class ApiError(Exception):
    """Base class for anything an adapter is expected to translate into its own idiom."""


class Unavailable(ApiError):
    """The BLE link is not up, so no command could be attempted. Retry later."""


class Refused(ApiError):
    """The request was rejected before anything was sent. Nothing reached the device."""


class Unverified(ApiError):
    """Bytes were written and the device did not visibly do the thing.

    **Not a generic server error.** The write may have taken effect with the confirmation
    lost, or been discarded entirely, and we cannot tell which — so an adapter must surface
    this distinctly rather than folding it into a failure. The BedJet has no
    acknowledgement; a clean write proves only that the radio accepted the bytes (RL-018).
    """


#: Mode names this API accepts, derived from the commander's allowlist rather than written
#: out again. Turbo and extended heat are locked in ``service/commander.py``; deriving the
#: names here means unlocking one is a single deliberate edit in the safety-critical file,
#: and cannot be done accidentally by adding a string to an adapter.
def available_modes() -> tuple[str, ...]:
    return tuple(sorted(mode.name.lower() for mode in UNLOCKED_MODES))


class BedJetAPI:
    """The device, as everything above the service layer sees it."""

    def __init__(self, session: DeviceSession) -> None:
        self._session = session

    # ── reading ─────────────────────────────────────────────────────────────────────────

    def snapshot(self) -> DeviceSnapshot:
        """The current belief. Cheap, synchronous, and never blocks on the radio."""
        return DeviceSnapshot.from_session(self._session.snapshot())

    def capabilities(self) -> Capabilities:
        return Capabilities(
            address=self._session.address,
            modes=available_modes(),
            fan_percent_min=FAN_PERCENT_MIN,
            fan_percent_max=FAN_PERCENT_MAX,
            fan_percent_step=FAN_PERCENT_STEP,
            temperature_step_c=TEMPERATURE_STEP_C,
            absolute_min_c=ABSOLUTE_MIN_C,
            absolute_max_c=ABSOLUTE_MAX_C,
        )

    def subscribe(self, listener: SnapshotListener) -> Callable[[], None]:
        """Register a listener for state changes; returns the function that removes it."""
        return self._session.subscribe(lambda snap: listener(DeviceSnapshot.from_session(snap)))

    async def wait_for_reading(self, timeout: float) -> DeviceSnapshot | None:
        """Wait for the device to say something. None on silence, which is not a fault."""
        if await self._session.wait_for_reading(timeout) is None:
            return None
        return self.snapshot()

    # ── the lease ───────────────────────────────────────────────────────────────────────

    async def yield_link(self, seconds: float) -> DeviceSnapshot:
        """Release the BLE link so the owner can use the vendor app."""
        if seconds <= 0:
            raise Refused("a yield needs a positive duration; zero would release nothing")
        await self._session.yield_link(seconds)
        return self.snapshot()

    async def resume_link(self) -> DeviceSnapshot:
        """End a yield early. The link comes back at the next supervision tick."""
        await self._session.resume()
        return self.snapshot()

    # ── commanding ──────────────────────────────────────────────────────────────────────

    async def turn_off(self, *, dry_run: bool = False) -> CommandOutcome:
        """Switch the unit off. The only command whose failure leaves things as they were."""
        return await self._command(
            lambda: self._session.commander.send_off(dry_run=dry_run),
            description="off",
            dry_run=dry_run,
        )

    async def set_mode(self, mode: str, *, dry_run: bool = False) -> CommandOutcome:
        """Select a mode by name. Locked modes are not merely absent — they are refused."""
        try:
            command_mode = CommandMode[mode.strip().upper()]
        except KeyError:
            raise Refused(
                f"unknown mode {mode!r}. This build accepts: {', '.join(available_modes())}."
            ) from None
        if command_mode not in UNLOCKED_MODES:
            raise Refused(
                f"{mode} is locked in this build. Turbo is the device's most aggressive "
                f"setting and nothing needs it; extended heat has never been observed in "
                f"status, so its result could not be verified even if it worked. Unlocking "
                f"is a deliberate edit to service/commander.py, not a request parameter."
            )
        return await self._command(
            lambda: self._session.commander.set_mode(command_mode, dry_run=dry_run),
            description=f"mode {mode.lower()}",
            dry_run=dry_run,
        )

    async def set_temperature(
        self,
        *,
        celsius: float | None = None,
        fahrenheit: float | None = None,
        dry_run: bool = False,
    ) -> CommandOutcome:
        """Set the target, in either unit.

        The permitted range is not checked here. It is per-mode, it moves, and the device is
        the authority on it (RL-013) — so the check happens against the live status packet,
        one layer down, and arrives back as a :class:`Refused` with the device's own bounds
        quoted in it.
        """
        if (celsius is None) == (fahrenheit is None):
            raise Refused("give exactly one of celsius or fahrenheit")
        target_c = celsius if celsius is not None else (fahrenheit - 32.0) * 5.0 / 9.0  # type: ignore[operator]
        return await self._command(
            lambda: self._session.commander.set_temperature(target_c, dry_run=dry_run),
            description=f"target {target_c:.1f}C",
            dry_run=dry_run,
        )

    async def set_fan(self, percent: int, *, dry_run: bool = False) -> CommandOutcome:
        """Set fan speed, snapped to the device's 5% steps."""
        return await self._command(
            lambda: self._session.commander.set_fan_percent(percent, dry_run=dry_run),
            description=f"fan {percent}%",
            dry_run=dry_run,
        )

    # ── the one place exceptions are translated ─────────────────────────────────────────

    async def _command(
        self,
        send: Callable[[], Awaitable[CommandResult]],
        *,
        description: str,
        dry_run: bool,
    ) -> CommandOutcome:
        """Run a command and turn every way it can end into one shape.

        Centralised deliberately: the mapping from "what the device did" to "what the caller
        is told" is a safety-relevant decision, and it should be readable in one place
        rather than reconstructed from five call sites.
        """
        if self._session.link is not LinkState.CONNECTED:
            raise Unavailable(
                f"the BLE link is {self._session.link.value}, so nothing was sent. "
                + (
                    f"The link was deliberately yielded and returns in "
                    f"{self._session.yield_remaining_s:.0f}s."
                    if self._session.link is LinkState.YIELDED
                    and self._session.yield_remaining_s is not None
                    else "The service is reconnecting."
                )
            )

        before = self.snapshot()
        try:
            result = await send()
        except AlreadySatisfied as exc:
            # Benign: the request is satisfied, so an automation should not treat it as an
            # error — but nothing was written, so nothing was verified, and `sent` is null.
            return CommandOutcome(
                ok=True,
                changed=False,
                detail=f"already {description}; nothing was sent. ({exc})",
                sent=None,
                dry_run=dry_run,
                before=before,
                after=self.snapshot(),
            )
        except CommandRefused as exc:
            raise Refused(str(exc)) from exc
        except encode.CommandError as exc:
            raise Refused(str(exc)) from exc
        except CommandUnverified as exc:
            raise Unverified(str(exc)) from exc
        except TransportError as exc:
            # Only failures from before the write can surface here: the commander converts
            # a TransportError raised at or after the write into CommandUnverified, so
            # "unavailable" keeps meaning what the served contract asserts — nothing was
            # sent. Kept as defence in depth for any future pre-write transport call.
            raise Unavailable(f"the link failed before anything was sent: {exc}") from exc

        return CommandOutcome(
            ok=True,
            changed=not dry_run,
            detail=(
                f"would send {result.payload.hex(' ')} for {description}; nothing was sent"
                if dry_run
                else f"{description}: verified against the device's own status"
            ),
            sent=None if dry_run else result.payload.hex(" "),
            dry_run=dry_run,
            before=before,
            after=self.snapshot(),
        )
