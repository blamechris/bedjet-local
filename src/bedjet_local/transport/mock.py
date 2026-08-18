"""In-memory transport for tests. No adapter, no device, no network.

This is what lets CI exercise connection lifecycle, subscription, partial-packet
reassembly, and reconnection without any hardware — AGENTS.md rule 4.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .base import DiscoveredDevice, NotifyCallback, TransportError


class MockTransport:
    """A scripted transport.

    Args:
        reads: characteristic UUID -> bytes returned by :meth:`read`.
        fail_connects: number of initial connection attempts to fail, for retry tests.
    """

    def __init__(
        self,
        reads: dict[str, bytes] | None = None,
        *,
        fail_connects: int = 0,
    ) -> None:
        self.reads = reads or {}
        self.writes: list[tuple[str, bytes]] = []
        self.write_responses: list[bool | None] = []
        self.connect_attempts = 0
        self._fail_connects = fail_connects
        self._connected = False
        self._subs: dict[str, list[NotifyCallback]] = defaultdict(list)
        self._write_error: TransportError | None = None
        self._adapter_power_on = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, address: str, *, timeout: float = 20.0) -> None:
        self.connect_attempts += 1
        if self.connect_attempts <= self._fail_connects:
            raise TransportError(f"simulated connection failure {self.connect_attempts}")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._subs.clear()

    def _require(self) -> None:
        if not self._connected:
            raise TransportError("not connected")

    async def read(self, characteristic: str) -> bytes:
        self._require()
        if characteristic not in self.reads:
            raise TransportError(f"no scripted read for {characteristic}")
        return self.reads[characteristic]

    async def write(
        self, characteristic: str, data: bytes, *, response: bool | None = None
    ) -> None:
        self._require()
        if self._write_error is not None:
            error, self._write_error = self._write_error, None
            raise error
        self.writes.append((characteristic, bytes(data)))
        self.write_responses.append(response)

    async def subscribe(self, characteristic: str, callback: NotifyCallback) -> None:
        self._require()
        self._subs[characteristic].append(callback)

    async def unsubscribe(self, characteristic: str) -> None:
        self._subs.pop(characteristic, None)

    async def services(self) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        self._require()
        return {}

    def adapter_power_on_event(self) -> asyncio.Event:
        return self._adapter_power_on

    # ── test driving ────────────────────────────────────────────────────────────────────

    def emit(self, characteristic: str, data: bytes) -> None:
        """Deliver a notification to every subscriber, as the device would."""
        for callback in self._subs.get(characteristic, []):
            callback(data)

    def drop(self) -> None:
        """Simulate an unexpected disconnect."""
        self._connected = False
        self._subs.clear()

    def fail_next_write(self, message: str = "simulated write failure") -> None:
        """Make the next :meth:`write` raise, one-shot, without recording the write.

        This is the ambiguous case the real radio produces: a write request that dies in
        flight, where neither side of the link can say whether the payload arrived. Not
        recording it in ``writes`` is deliberate — the test, like the caller, must not get
        to know."""
        self._write_error = TransportError(message)

    def power_on_adapter(self) -> None:
        """Simulate the host adapter turning (back) on, as a power cycle or a wake would.

        Note this only fires the event — whether connecting *works* is still governed by
        ``refuse_next_connects``, because a powered adapter says nothing about whether the
        device is reachable."""
        self._adapter_power_on.set()

    def refuse_next_connects(self, count: int) -> None:
        """Make the next ``count`` connection attempts fail.

        ``fail_connects`` in the constructor only covers a device that is unreachable from
        the start. A supervised session needs the other shape too — connected, then dropped,
        then *unreachable for a while* — which is what an actual radio dip looks like and
        the only way to observe backoff or a link that stays down.
        """
        self._fail_connects = self.connect_attempts + count


async def mock_scan(_timeout: float = 10.0) -> list[DiscoveredDevice]:
    return [
        DiscoveredDevice(
            address="00:11:22:33:44:55",
            name="BEDJET_V3",
            rssi=-62,
            service_uuids=("00001000-bed0-0080-aa55-4265644a6574",),
        )
    ]
