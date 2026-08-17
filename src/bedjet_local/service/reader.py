"""Status reading: subscription, partial-packet reassembly, state fan-out.

Milestone 1's entire service layer. Read-only by construction — this module has no way to
send a command, which is the point: bring-up cannot accidentally write to a heater.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..device.state import BedJetState
from ..protocol.constants import STATUS_UUID
from ..protocol.decode import decode_status, reassemble
from ..protocol.packets import StatusPacket
from ..transport.base import Transport

log = logging.getLogger(__name__)

StateCallback = Callable[[BedJetState, StatusPacket], None]


class StatusReader:
    """Subscribes to status notifications and publishes decoded state.

    Partial packets: upstream documents that a V3 status notification can arrive flagged
    partial, with the remainder available from an explicit read of the same
    characteristic. 📖 UPSTREAM, unverified on our device — so this class *handles* the
    case and *logs loudly* when it sees it, which is how we find out whether it is real.
    """

    def __init__(self, transport: Transport, on_state: StateCallback) -> None:
        self._transport = transport
        self._on_state = on_state
        self._pending_partial: bytes | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # The device re-sends the same status several times a second, so logging every
        # occurrence of an unchanged condition buries anything that actually changed.
        self._last_anomalies: tuple[str, ...] | None = None
        self._logged_split_notice = False
        self.packets_seen = 0
        self.partials_seen = 0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._transport.subscribe(STATUS_UUID, self._on_notify)
        log.info("status reader started")

    async def stop(self) -> None:
        await self._transport.unsubscribe(STATUS_UUID)
        log.info(
            "status reader stopped (%d packets, %d partial)", self.packets_seen, self.partials_seen
        )

    def _on_notify(self, data: bytes) -> None:
        """Called from the transport's notification thread/task. Kept trivial."""
        self.packets_seen += 1
        packet = decode_status(data)

        # Completeness is decided by the header's own length byte, not by the partial flag
        # (RL-012). The flag stays set on a reassembled packet because it comes from the
        # first fragment, so keying off it would loop; and a length the device asserts
        # about itself is better evidence than a flag we only half understand.
        if not packet.is_complete:
            self.partials_seen += 1
            if not self._logged_split_notice:
                # Expected on this device: every status arrives split. Say so once, then
                # drop to debug — a per-packet INFO line is noise, not information.
                log.info(
                    "status packets arrive split (%d of %s bytes) — fetching the remainder; "
                    "further occurrences at debug level",
                    len(data),
                    packet.expected_total,
                )
                self._logged_split_notice = True
            else:
                log.debug("incomplete packet (%d of %s bytes)", len(data), packet.expected_total)
            self._pending_partial = data
            if self._loop is not None:
                self._loop.create_task(self._complete_partial(data))
            return

        self._publish(packet)

    async def _complete_partial(self, first: bytes) -> None:
        try:
            remainder = await self._transport.read(STATUS_UUID)
        except Exception:
            log.exception("follow-up read for partial packet failed; state may be stale")
            return
        self._pending_partial = None
        self._publish(decode_status(reassemble(first, remainder)))

    def _publish(self, packet: StatusPacket) -> None:
        state = BedJetState.from_status(packet, available=self._transport.is_connected)
        if packet.anomalies != self._last_anomalies:
            for anomaly in packet.anomalies:
                log.warning("anomaly: %s", anomaly)
            self._last_anomalies = packet.anomalies
        self._on_state(state, packet)
