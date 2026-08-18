"""The connection lease: who holds the BedJet's one BLE slot, and when they give it back.

Everything before this module was a *command*: connect, do one thing, release. A daemon is
different in kind — it holds the link for hours, and the BedJet permits exactly one BLE
client at a time. While we hold it, the owner's vendor app cannot connect, and there is no
working physical remote (``docs/SAFETY.md``). So the lease is not a detail of the daemon;
it is the reason this module exists:

- **The link is held, not owned.** :meth:`DeviceSession.yield_link` releases it on demand
  and refuses to reconnect until the yield expires, so the owner can always take their own
  heater back without stopping the service.
- **Dropped links are reconnected with backoff**, because the radio dips 22 dB while
  nothing moves (RL-010) and a daemon that gives up on the first drop is offline all night.
- **Availability and power stay independent.** A lost link tells us nothing about whether
  the unit is running, so the last reading is retained *with its age* rather than being
  reinterpreted or thrown away. Consumers get "here is what we last saw, and it was 4
  seconds ago" — never a fresh-looking guess.

One subscription, one commander, one reader. Anything that wants state subscribes here
rather than opening its own: two subscriptions to the status characteristic means two
follow-up reads in flight against one handle, which is the collision that manufactured a
"heater is off" reading from a tail fragment in RL-017.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from enum import Enum

from ..device.state import BedJetState
from ..protocol.packets import StatusPacket
from ..transport.base import Transport, TransportError
from .commander import Commander

log = logging.getLogger(__name__)

#: A reading older than this is reported as stale. Generous on purpose: the BedJet notifies
#: several times a second while running but is near-silent in standby, so a quiet minute is
#: normal rather than a fault. Stale means "we have not heard from it recently" — it never
#: means "it is off", and nothing downstream may read it that way.
DEFAULT_STALE_AFTER_S = 60.0

StateListener = Callable[["SessionSnapshot"], None]


class LinkState(Enum):
    """Where the BLE link is, from this session's point of view."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    YIELDED = "yielded"
    """Deliberately released so the owner can use the vendor app. Not a fault, and the
    supervisor will not reconnect until the yield expires."""
    LOST = "lost"
    """Dropped unexpectedly. The supervisor is retrying."""


class SessionSnapshot:
    """What the session believes right now, link and device kept apart.

    Deliberately a plain object rather than a dataclass of the device state: the two halves
    have different lifetimes. ``link`` is true now; ``reading`` is the last thing the device
    told us, which may be seconds or minutes old, and :attr:`reading_age_s` is the only
    honest way to say which.
    """

    __slots__ = ("link", "reading", "reading_age_s", "stale")

    def __init__(
        self,
        link: LinkState,
        reading: BedJetState | None,
        reading_age_s: float | None,
        stale: bool,
    ) -> None:
        self.link = link
        self.reading = reading
        self.reading_age_s = reading_age_s
        self.stale = stale

    @property
    def available(self) -> bool:
        """Whether the BLE link is up. Says nothing about whether the unit is running."""
        return self.link is LinkState.CONNECTED

    def describe(self) -> str:
        if self.reading is None:
            return f"link={self.link.value}  no reading yet"
        age = f"{self.reading_age_s:.0f}s ago" if self.reading_age_s is not None else "age unknown"
        stale = " ⚠️ stale" if self.stale else ""
        return f"link={self.link.value}  {self.reading.describe()}  ({age}{stale})"


class DeviceSession:
    """Holds the link to one BedJet and publishes what it says.

    Args:
        transport: an *unconnected* transport. The session owns its lifecycle from here.
        address: the device address, already checked against the registry by the caller.
        connect_timeout: passed through to the transport on every attempt.
        settle_timeout: how long a command waits for the device to confirm it.
        backoff_initial / backoff_max: reconnection backoff bounds, in seconds.
        supervise_interval: how often to check the link. The BLE stack notices a drop on
            its own; this only decides how quickly we react to it.
        stale_after_s: age at which a reading is reported stale.
        poll_interval: seconds between whole-packet status reads instead of the verified
            notification path (#2 experiment). ``None``, the default, means notifications.
    """

    def __init__(
        self,
        transport: Transport,
        address: str,
        *,
        connect_timeout: float = 20.0,
        settle_timeout: float = 10.0,
        backoff_initial: float = 2.0,
        backoff_max: float = 60.0,
        supervise_interval: float = 1.0,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        poll_interval: float | None = None,
    ) -> None:
        self._transport = transport
        self._address = address
        self._connect_timeout = connect_timeout
        self._settle_timeout = settle_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._supervise_interval = supervise_interval
        self._stale_after_s = stale_after_s

        self._commander = Commander(
            transport,
            settle_timeout=settle_timeout,
            on_state=self._on_state,
            poll_interval=poll_interval,
        )
        self._link = LinkState.STOPPED
        self._reading: BedJetState | None = None
        self._packet: StatusPacket | None = None
        self._reading_at: float | None = None
        self._yield_until: float | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._listeners: list[StateListener] = []
        self._connected = asyncio.Event()
        # Serialises taking and releasing the link, so a reconnect in flight cannot outlive
        # the yield that was meant to stop it.
        self._link_lock = asyncio.Lock()

    # ── properties ──────────────────────────────────────────────────────────────────────

    @property
    def address(self) -> str:
        return self._address

    @property
    def link(self) -> LinkState:
        return self._link

    @property
    def commander(self) -> Commander:
        """The one object permitted to send. Exposed so the API layer can command without
        the session growing a pass-through method per command."""
        return self._commander

    @property
    def packet(self) -> StatusPacket | None:
        """The last trustworthy packet, for diagnostics. Not part of the API contract —
        anything above ``api/`` that wants this wants a field on the state instead."""
        return self._packet

    def snapshot(self) -> SessionSnapshot:
        age = None if self._reading_at is None else time.monotonic() - self._reading_at
        return SessionSnapshot(
            link=self._link,
            reading=self._reading,
            reading_age_s=age,
            stale=age is None or age > self._stale_after_s,
        )

    # ── subscription ────────────────────────────────────────────────────────────────────

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Register a listener, and return the function that removes it.

        Returning the unsubscribe rather than exposing a ``remove`` method means a caller
        cannot accidentally unsubscribe somebody else's listener, and a websocket handler
        can hold one closure for its whole life.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        snapshot = self.snapshot()
        for listener in list(self._listeners):
            # One broken consumer must not take down the device link, and must not stop the
            # other consumers from being told.
            try:
                listener(snapshot)
            except Exception:
                log.exception("a session listener raised; continuing")

    def _on_state(self, state: BedJetState, packet: StatusPacket) -> None:
        self._reading = state
        self._packet = packet
        self._reading_at = time.monotonic()
        self._notify()

    def _set_link(self, link: LinkState) -> None:
        if link is self._link:
            return
        log.info("link %s -> %s", self._link.value, link.value)
        self._link = link
        if link is LinkState.CONNECTED:
            self._connected.set()
        else:
            self._connected.clear()
        self._notify()

    # ── lifecycle ───────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect, subscribe, and begin supervising the link.

        The **first** connection is awaited and its failure is raised: an address that is
        wrong, unregistered, or held by the vendor app should stop a daemon at boot with the
        transport's own diagnosis, not disappear into a retry loop that reports nothing.
        Every *subsequent* drop is the supervisor's problem and is retried with backoff.
        """
        if self._supervisor is not None:
            raise RuntimeError("session already started")
        self._set_link(LinkState.CONNECTING)
        try:
            await self._connect()
        except Exception:
            self._set_link(LinkState.STOPPED)
            raise
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Release the link and stop supervising. Safe to call twice."""
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._teardown()
        self._set_link(LinkState.STOPPED)

    async def _connect(self) -> None:
        """Take the link, unless it has been given away in the meantime.

        The lock and the re-check are both load-bearing. A reconnect can be in flight when
        a yield arrives — a drop and a yield seconds apart is an ordinary evening — and a
        yield that silently loses a race is a yield that did not happen. Since
        :meth:`yield_link` sets the deadline *before* it awaits anything, either this sees
        it here and declines to connect, or the teardown queues behind us on the lock and
        undoes what we just did. Both orderings end with the owner holding their heater.
        """
        async with self._link_lock:
            if self._yield_until is not None:
                log.info("abandoning a reconnect: the link has been yielded")
                return
            await self._transport.connect(self._address, timeout=self._connect_timeout)
            await self._commander.start()
            self._set_link(LinkState.CONNECTED)

    async def _teardown(self) -> None:
        async with self._link_lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        # Order matters: unsubscribe before dropping the link, or the backend logs a storm
        # of errors for a subscription whose connection has gone (RL-017).
        with contextlib.suppress(Exception):
            await self._commander.stop()
        with contextlib.suppress(Exception):
            await self._transport.disconnect()

    async def wait_until_connected(self, timeout: float) -> bool:
        """Block until the link is up. Returns False on timeout rather than raising."""
        if self._link is LinkState.CONNECTED:
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def wait_for_reading(self, timeout: float) -> BedJetState | None:
        """Wait for a trustworthy reading, or return None.

        Returns None rather than raising because silence is genuinely ambiguous here: the
        device is near-silent in standby, so "no packet in 10 seconds" is not evidence of a
        fault and must not be presented as one.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self._reading is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.1, remaining))
        return self._reading

    # ── the lease ───────────────────────────────────────────────────────────────────────

    async def yield_link(self, seconds: float) -> None:
        """Release the BLE link for ``seconds``, then take it back.

        This is the escape hatch that makes it acceptable to run a daemon against this
        device at all. The BedJet allows one BLE client; there is no working physical
        remote; so without an explicit yield, starting the service would take the owner's
        heater away from them until they killed the process. Returns as soon as the link is
        released — the reconnection happens in the supervisor when the yield expires.
        """
        if seconds <= 0:
            raise ValueError("a yield of zero seconds would not release anything")
        log.warning("yielding the BLE link for %.0fs — the vendor app can connect", seconds)
        self._yield_until = time.monotonic() + seconds
        await self._teardown()
        self._set_link(LinkState.YIELDED)

    async def resume(self) -> None:
        """End a yield early and reconnect at the next supervision tick."""
        if self._yield_until is None:
            return
        log.info("ending the yield early")
        self._yield_until = None

    @property
    def yield_remaining_s(self) -> float | None:
        if self._yield_until is None:
            return None
        return max(0.0, self._yield_until - time.monotonic())

    # ── supervision ─────────────────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Keep the link up, unless we have deliberately given it away."""
        backoff = self._backoff_initial
        while True:
            await asyncio.sleep(self._supervise_interval)

            if self._yield_until is not None:
                if time.monotonic() < self._yield_until:
                    continue
                log.info("yield expired; reclaiming the link")
                self._yield_until = None

            if self._transport.is_connected:
                if self._link is not LinkState.CONNECTED:
                    self._set_link(LinkState.CONNECTED)
                backoff = self._backoff_initial
                continue

            if self._link is LinkState.CONNECTED:
                # Reported, not inferred: the link is down, so we no longer know anything
                # about the device. The last reading is kept, with its age.
                log.warning("link lost; will reconnect")
            self._set_link(LinkState.LOST)

            # Tear down before reconnecting. A stale subscription against a dead client
            # leaves the backend issuing reads that can never complete.
            await self._teardown()
            try:
                await self._connect()
            except (TransportError, OSError) as exc:
                log.warning("reconnect failed (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("unexpected error reconnecting; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
            else:
                backoff = self._backoff_initial
