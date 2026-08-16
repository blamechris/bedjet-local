"""In-memory transport for tests. No adapter, no device, no network.

This is what lets CI exercise connection lifecycle, subscription, partial-packet
reassembly, and reconnection without any hardware — AGENTS.md rule 4.
"""

from __future__ import annotations

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
        self.connect_attempts = 0
        self._fail_connects = fail_connects
        self._connected = False
        self._subs: dict[str, list[NotifyCallback]] = defaultdict(list)

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

    async def write(self, characteristic: str, data: bytes, *, response: bool = False) -> None:
        self._require()
        self.writes.append((characteristic, bytes(data)))

    async def subscribe(self, characteristic: str, callback: NotifyCallback) -> None:
        self._require()
        self._subs[characteristic].append(callback)

    async def unsubscribe(self, characteristic: str) -> None:
        self._subs.pop(characteristic, None)

    async def services(self) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        self._require()
        return {}

    # ── test driving ────────────────────────────────────────────────────────────────────

    def emit(self, characteristic: str, data: bytes) -> None:
        """Deliver a notification to every subscriber, as the device would."""
        for callback in self._subs.get(characteristic, []):
            callback(data)

    def drop(self) -> None:
        """Simulate an unexpected disconnect."""
        self._connected = False
        self._subs.clear()


async def mock_scan(_timeout: float = 10.0) -> list[DiscoveredDevice]:
    return [
        DiscoveredDevice(
            address="00:11:22:33:44:55",
            name="BEDJET_V3",
            rssi=-62,
            service_uuids=("00001000-bed0-0080-aa55-4265644a6574",),
        )
    ]
