"""Status reading: subscription, partial-packet reassembly, state fan-out.

Read-only by construction — this module has no way to send a command.

**Two hard rules, both learned from RL-017**, where a mis-delivered fragment was published
as valid state and a heater was reported off while it was still running:

1. **A packet that fails its checksum is never published.** Its fields are meaningless, and
   a meaningless reading is worse than no reading, because something downstream will act on
   it.
2. **Only one follow-up read may be in flight at a time.** The device notifies several times
   a second and every status arrives split, so an unserialised implementation issues
   overlapping reads of the same characteristic. Bleak's CoreBluetooth backend keys pending
   reads by characteristic handle, so overlapping reads of one characteristic collide and
   return each other's data — which is how a tail fragment ended up looking like a whole
   packet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ..device.state import BedJetState
from ..protocol.constants import STATUS_UUID
from ..protocol.decode import decode_status, looks_like_packet_start, reassemble
from ..protocol.packets import StatusPacket
from ..transport.base import Transport

log = logging.getLogger(__name__)

StateCallback = Callable[[BedJetState, StatusPacket], None]


class StatusReader:
    """Subscribes to status notifications and publishes decoded, **verified** state."""

    def __init__(self, transport: Transport, on_state: StateCallback) -> None:
        self._transport = transport
        self._on_state = on_state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._completion: asyncio.Task[None] | None = None
        self._last_anomalies: tuple[str, ...] | None = None
        self._logged_split_notice = False
        self._logged_read_failure = False

        self.packets_seen = 0
        self.partials_seen = 0
        self.rejected_checksum = 0
        """Packets discarded because they failed their checksum. Not silent — see
        :attr:`rejected`, and the count is reported when the reader stops."""
        self.rejected_not_a_packet = 0
        """Notifications discarded because they did not begin a packet — a stray tail."""
        self.dropped_while_busy = 0
        """Notifications skipped because a follow-up read was already in flight. Harmless:
        the device repeats itself constantly, so the next one arrives in milliseconds."""

    @property
    def rejected(self) -> int:
        return self.rejected_checksum + self.rejected_not_a_packet

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._transport.subscribe(STATUS_UUID, self._on_notify)
        log.info("status reader started")

    async def stop(self) -> None:
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
            remainder = await self._transport.read(STATUS_UUID)
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
        self._publish(decode_status(reassemble(first, remainder)))

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
