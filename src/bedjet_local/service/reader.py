"""Status reading: notifications with reassembly, or whole-packet polling; state fan-out.

Read-only by construction — this module has no way to send a command.

**Two acquisition modes**, chosen at construction and never mixed:

- **Notify** (the default): subscribe, and complete each truncated notification with a
  follow-up read. This is the verified path — but the completion is not an edge case.
  17,975 of 17,982 packets across five hardware runs arrived split (RL-026 through
  RL-033), so the follow-up read runs essentially always, every occurrence opens the
  RL-017 reassembly window, and a pending read overlapping incoming notifications on the
  same characteristic was the race behind #6's ``InvalidStateError`` inside bleak. That
  race is closed at subscribe time: :func:`looks_like_packet_start` is handed to the
  backend as its notification discriminator, so a value that begins a packet is routed
  to us as a notification even while our follow-up read is pending. The routing is sound
  because on this firmware the two directions cannot produce each other's shape: every
  notification begins a packet, and a read serves the pinned remainder window — never a
  packet start — measured at 303/303 and promoted to ✅ in RL-034.

- **Poll** (``poll_interval=<seconds>``): never subscribe; read the characteristic on an
  interval and accept only whole packets. One sequential loop, so there is nothing to
  reassemble, no heuristic in the hot path, and no notification for a pending read to
  race — the #6 window does not exist here. ❓ **HYPOTHESIS until measured**: the
  follow-up read demonstrably returns the *remainder* of a notified packet (RL-012), so
  the firmware serves reads through some cursor, and what a **cold** read returns has
  never been observed on our device. This mode is instrumented — shape counters and read
  round-trip times — precisely so one attended ``bedjet watch --poll 1`` run can deliver
  the verdict (#2). Until that run, notify stays the default.

**Two hard rules, both learned from RL-017**, where a mis-delivered fragment was published
as valid state and a heater was reported off while it was still running. They hold in
either mode:

1. **A packet that fails its checksum is never published.** Its fields are meaningless, and
   a meaningless reading is worse than no reading, because something downstream will act on
   it.
2. **Only one GATT read may be in flight at a time.** Bleak's CoreBluetooth backend keys
   pending reads by characteristic handle, so overlapping reads of one characteristic
   collide and return each other's data — which is how a tail fragment ended up looking
   like a whole packet. Notify mode serialises its follow-up reads; poll mode is a single
   sequential loop and cannot overlap itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable

from ..device.state import BedJetState
from ..protocol.constants import STATUS_UUID
from ..protocol.decode import decode_status, looks_like_packet_start, reassemble
from ..protocol.packets import StatusPacket
from ..transport.base import Transport

log = logging.getLogger(__name__)

StateCallback = Callable[[BedJetState, StatusPacket], None]

#: Upper bound on one follow-up read. Bleak's CoreBluetooth backend gives a read 20 s —
#: hardcoded — and the transport holds its one-GATT-operation lock for the read's whole
#: life, so a read that will never resolve stalls every command write behind it for those
#: 20 s. The device re-sends within about a second, so abandoning a slow follow-up costs
#: one packet interval. This also bounds the discriminator's one new failure direction:
#: if a future firmware ever served a read that *begins* a packet, the value would be
#: routed to the notify path and the read would sit unresolved. RL-034 says that cannot
#: happen today (303/303 reads serve the remainder window); this makes "today" a bounded
#: bet instead of a load-bearing one.
_FOLLOW_UP_READ_TIMEOUT_S = 5.0


class StatusReader:
    """Acquires status packets — by subscription or by polling — and publishes decoded,
    **verified** state.

    Args:
        transport: a connected transport.
        on_state: called with every trustworthy state.
        poll_interval: seconds between whole-packet reads. ``None`` (the default) selects
            the verified notification path; a value selects the #2 poll experiment.
    """

    def __init__(
        self,
        transport: Transport,
        on_state: StateCallback,
        *,
        poll_interval: float | None = None,
    ) -> None:
        if poll_interval is not None and not (math.isfinite(poll_interval) and poll_interval > 0):
            # Finite matters as much as positive: inf sleeps forever after the first
            # read, and a nan timeout corrupts the event loop's timer heap ordering.
            raise ValueError("a poll interval must be a positive, finite number of seconds")
        self._transport = transport
        self._on_state = on_state
        self._poll_interval = poll_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._completion: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._poke = asyncio.Event()
        self._last_anomalies: tuple[str, ...] | None = None
        self._logged_split_notice = False
        self._logged_read_failure = False
        self._logged_poll_shape = False
        self._logged_callback_failure = False

        self.packets_seen = 0
        """Chunks of data received from the device: notifications in notify mode,
        successful reads in poll mode."""
        self.partials_seen = 0
        self.rejected_checksum = 0
        """Packets discarded because they failed their checksum. Not silent — see
        :attr:`rejected`, and the count is reported when the reader stops."""
        self.rejected_not_a_packet = 0
        """Notifications discarded because they did not begin a packet — a stray tail."""
        self.dropped_while_busy = 0
        """Notifications skipped because a follow-up read was already in flight. Harmless:
        the device repeats itself constantly, so the next one arrives in milliseconds."""

        self.polls = 0
        """Poll reads that returned data, whatever its shape."""
        self.polls_whole = 0
        """Poll reads that returned a complete packet — the #2 hypothesis confirmed, one
        read at a time. (Completeness is shape; integrity is still the checksum's call.)"""
        self.polls_partial = 0
        """Poll reads that returned a packet start shorter than its header declares — the
        same truncation notifications show, which would mean cold reads truncate too."""
        self.polls_alien = 0
        """Poll reads that returned data that does not begin a packet — most plausibly the
        firmware's read cursor serving a remainder we never asked for."""
        self.poll_failures = 0
        """Poll reads that raised. The loop keeps polling; the session owns reconnection."""

        self._rtt_min: float | None = None
        self._rtt_max = 0.0
        self._rtt_sum = 0.0

    @property
    def rejected(self) -> int:
        return self.rejected_checksum + self.rejected_not_a_packet

    @property
    def poll_rtt_ms(self) -> tuple[float, float, float] | None:
        """(min, mean, max) read round-trip in milliseconds; None before the first success.

        This is the number #2 still needs from hardware: what one status read costs, and
        therefore what poll interval keeps command verification acceptably fast. Measured
        around the transport call, so it includes our own lock wait — which is the honest
        figure, since that is the latency a verification actually experiences.
        """
        if self.polls == 0 or self._rtt_min is None:
            return None
        return (
            self._rtt_min * 1000.0,
            self._rtt_sum / self.polls * 1000.0,
            self._rtt_max * 1000.0,
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._poll_interval is not None:
            # A poke from a write that died mid-teardown must not survive into a fresh
            # connection as a phantom immediate read.
            self._poke.clear()
            self._poll_task = self._loop.create_task(self._poll_loop())
            log.info(
                "status reader started (polling a whole read every %.2fs — #2 experiment)",
                self._poll_interval,
            )
            return
        # The discriminator is the #6 guard. While our follow-up read is pending, the
        # backend cannot tell that read's response from the next notification — both
        # arrive the same way — and mis-taking a notification for the response both
        # corrupts the pairing (RL-017's shape) and leaves the true response to land on
        # a retired future. "Begins a packet" decides it: notifications always do, reads
        # serve the remainder window and never do (RL-034, ✅).
        await self._transport.subscribe(
            STATUS_UUID, self._on_notify, notification_discriminator=looks_like_packet_start
        )
        log.info("status reader started")

    async def stop(self) -> None:
        if self._poll_interval is not None:
            if self._poll_task is not None:
                task, self._poll_task = self._poll_task, None
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # The loop is guarded against this, but a stop must never lose the
                    # teardown that follows it — in `watch` that is the disconnect that
                    # frees the device's one client slot, and the experiment's summary.
                    log.exception("the poll loop had already died; stopping anyway")
            log.info(
                "status reader stopped (%d polls: %d whole, %d partial, %d alien, "
                "%d rejected, %d failed reads; read rtt min/mean/max %s)",
                self.polls,
                self.polls_whole,
                self.polls_partial,
                self.polls_alien,
                self.rejected_checksum,
                self.poll_failures,
                self._describe_rtt(),
            )
            return
        # Cancel any in-flight completion first. A read that outlives the subscription
        # produces a storm of backend errors at teardown (RL-017).
        if self._completion is not None and not self._completion.done():
            self._completion.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._completion
        self._completion = None
        with contextlib.suppress(Exception):
            await self._transport.unsubscribe(STATUS_UUID)
        log.info(
            "status reader stopped (%d packets, %d split, %d rejected, %d skipped while busy)",
            self.packets_seen,
            self.partials_seen,
            self.rejected,
            self.dropped_while_busy,
        )

    def poke(self) -> None:
        """Ask poll mode to read now rather than at the next tick.

        The commander calls this right after a write, so verification waits one read
        round-trip instead of up to a full poll interval — which is what keeps a slow
        poll cadence from costing command latency. In notify mode it is a no-op: the
        device announces its own changes.
        """
        self._poke.set()

    # ── poll mode ───────────────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        assert self._poll_interval is not None
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                data = await self._transport.read(STATUS_UUID)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Once, at warning level; the link may be mid-drop and the session owns
                # reconnection. A traceback per interval would bury everything else.
                self.poll_failures += 1
                if not self._logged_read_failure:
                    log.warning(
                        "poll read failed (%s); state may be stale — further failures at "
                        "debug level",
                        exc,
                    )
                    self._logged_read_failure = True
                else:
                    log.debug("poll read failed: %s", exc)
            else:
                self._record_rtt(loop.time() - started)
                self.polls += 1
                self.packets_seen += 1
                try:
                    self._classify_poll(data)
                except Exception:
                    # A consumer raised out of the state callback. The loop must outlive
                    # its listeners — count and drop, never die. (The notify path gets
                    # the equivalent protection from the transport's callback context;
                    # here the callback runs inside our own task, so the guard is ours.)
                    if not self._logged_callback_failure:
                        log.exception("a state callback raised; the poll loop continues")
                        self._logged_callback_failure = True
                    else:
                        log.debug("a state callback raised again; continuing")
            if self._poke.is_set():
                # A command was just written: read again immediately so its verification
                # sees the device's reaction, not the pre-write state for another tick.
                self._poke.clear()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), timeout=self._poll_interval)
            self._poke.clear()

    def _classify_poll(self, data: bytes) -> None:
        """Sort one poll read into the experiment's buckets, publishing only whole packets.

        A non-whole read is counted and dropped, never completed with a second read: the
        notify path exists for the cursor dance, and blending the two here would make the
        attended run's numbers unreadable.
        """
        if looks_like_packet_start(data):
            packet = decode_status(data)
            if packet.is_complete:
                self.polls_whole += 1
                self._publish(packet)
                return
            self.polls_partial += 1
            shape = f"a partial packet ({len(data)} of {packet.expected_total} bytes)"
        else:
            self.polls_alien += 1
            shape = f"data that does not begin a packet ({len(data)} bytes)"
        if not self._logged_poll_shape:
            log.warning(
                "a poll read returned %s — the whole-packet-read hypothesis (#2) is "
                "looking false on this firmware; counting, further occurrences at debug "
                "level",
                shape,
            )
            self._logged_poll_shape = True
        else:
            log.debug("poll read returned %s: %s", shape, data.hex(" "))

    def _record_rtt(self, seconds: float) -> None:
        self._rtt_sum += seconds
        self._rtt_min = seconds if self._rtt_min is None else min(self._rtt_min, seconds)
        self._rtt_max = max(self._rtt_max, seconds)

    def _describe_rtt(self) -> str:
        rtt = self.poll_rtt_ms
        if rtt is None:
            return "n/a"
        return f"{rtt[0]:.0f}/{rtt[1]:.0f}/{rtt[2]:.0f} ms"

    # ── notify mode ─────────────────────────────────────────────────────────────────────

    def _on_notify(self, data: bytes) -> None:
        """Called from the transport's notification thread/task. Kept trivial."""
        self.packets_seen += 1

        if not looks_like_packet_start(data):
            # A stray tail fragment. Decoding it would read meaning into offsets that hold
            # something else entirely — exactly the RL-017 failure.
            self.rejected_not_a_packet += 1
            log.debug("ignoring notification that does not start a packet: %s", data.hex(" "))
            return

        packet = decode_status(data)

        if not packet.is_complete:
            if self._completion is not None and not self._completion.done():
                # Serialised on purpose: concurrent reads of one characteristic collide in
                # the backend and return each other's data. Skipping is safe — the device
                # re-sends constantly.
                self.dropped_while_busy += 1
                return
            self.partials_seen += 1
            if not self._logged_split_notice:
                log.info(
                    "status packets arrive split (%d of %s bytes) — fetching the remainder; "
                    "further occurrences at debug level",
                    len(data),
                    packet.expected_total,
                )
                self._logged_split_notice = True
            else:
                log.debug("incomplete packet (%d of %s bytes)", len(data), packet.expected_total)
            if self._loop is not None:
                self._completion = self._loop.create_task(self._complete_partial(data))
            return

        self._publish(packet)

    async def _complete_partial(self, first: bytes) -> None:
        try:
            remainder = await asyncio.wait_for(
                self._transport.read(STATUS_UUID), timeout=_FOLLOW_UP_READ_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Once, at warning level. This fires on every packet when the link is
            # struggling, and a full traceback per occurrence buries everything else.
            if not self._logged_read_failure:
                log.warning("follow-up read failed (%s); state may be stale", exc)
                self._logged_read_failure = True
            else:
                log.debug("follow-up read failed: %s", exc)
            return
        # Publication order can briefly invert here: a *whole* notification arriving while
        # this read was pending has already published newer state, and this reassembled
        # packet is older. 7 of 17,982 packets arrive whole, the inversion corrects on the
        # next packet (~1 s), and the pre-#6 behavior corrupted both values in this window
        # instead — accepted, not accidental.
        self._publish(decode_status(reassemble(first, remainder)))

    # ── both modes ──────────────────────────────────────────────────────────────────────

    def _publish(self, packet: StatusPacket) -> None:
        if not packet.is_trustworthy:
            # RL-017. A corrupt packet's fields are meaningless; publishing them lets
            # something downstream act on noise. Count it, say so once, drop it.
            self.rejected_checksum += 1
            if packet.anomalies != self._last_anomalies:
                log.warning(
                    "discarded an untrustworthy packet (checksum_ok=%s): %s",
                    packet.checksum_ok,
                    "; ".join(packet.anomalies) or "no detail",
                )
                self._last_anomalies = packet.anomalies
            return

        state = BedJetState.from_status(packet, available=self._transport.is_connected)
        if packet.anomalies != self._last_anomalies:
            for anomaly in packet.anomalies:
                log.warning("anomaly: %s", anomaly)
            self._last_anomalies = packet.anomalies
        self._on_state(state, packet)
