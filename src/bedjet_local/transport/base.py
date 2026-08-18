"""Transport abstraction.

The narrow waist of the whole system. Everything above this file is unaware of BLE, and
everything BLE-specific lives below it. That boundary is what makes ADR-0001's staged plan
free: a direct ``bleak`` client, an ESPHome Bluetooth Proxy, and a replay mock all satisfy
this interface, so choosing between them later is a configuration change rather than a
rewrite.

Nothing in ``protocol/``, ``device/``, or above may import ``bleak``. Enforced by
``tests/unit/test_layering.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

NotifyCallback = Callable[[bytes], None]

NotificationDiscriminator = Callable[[bytes], bool]
"""Given a characteristic value, ``True`` if it is a notification, ``False`` if it is the
response to a read. Only a backend where the two arrive through one delivery path needs
the distinction — see :meth:`Transport.subscribe`."""


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A BedJet-looking device seen in a scan.

    ``address`` is **platform-dependent and not portable**: a MAC address on Linux/BlueZ,
    a system-assigned UUID on macOS, because CoreBluetooth does not expose MACs. Config
    must therefore never assume a MAC, and an address recorded on one host will not
    resolve on another. 📖 UPSTREAM (bleak docs) — to be confirmed in bring-up step 3.
    """

    address: str
    name: str | None
    rssi: int | None
    service_uuids: tuple[str, ...] = ()
    manufacturer_data: tuple[tuple[int, bytes], ...] = ()

    def describe(self) -> str:
        rssi = f"{self.rssi} dBm" if self.rssi is not None else "rssi unknown"
        return f"{self.name or '(unnamed)'}  {self.address}  {rssi}"


@runtime_checkable
class Transport(Protocol):
    """Everything the layers above need from a BLE link, and nothing more."""

    async def connect(self, address: str, *, timeout: float = 20.0) -> None: ...

    async def disconnect(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    async def read(self, characteristic: str) -> bytes: ...

    async def write(
        self, characteristic: str, data: bytes, *, response: bool | None = None
    ) -> None:
        """Write to a characteristic.

        ``response=None`` means "choose from the characteristic's declared properties".
        Defaulting to without-response is a trap: BLE treats ``write`` and
        ``write-without-response`` as separate properties, and CoreBluetooth silently drops
        a without-response write to a characteristic that only declares ``write`` (RL-018).
        """
        ...

    async def subscribe(
        self,
        characteristic: str,
        callback: NotifyCallback,
        *,
        notification_discriminator: NotificationDiscriminator | None = None,
    ) -> None:
        """Subscribe to notifications on a characteristic.

        The discriminator teaches the backend to tell a notification from a read
        response. CoreBluetooth delivers both through the same delegate callback, so
        while a read is in flight every arriving value is presumed to be its response
        unless something can say otherwise. A notification landing in that window is
        consumed as the read's result — the mis-pairing behind RL-017 — and the real
        response then lands on a future that has already been resolved, which is #6's
        ``InvalidStateError`` inside bleak, unreachable by any ``try`` of ours. A caller
        that both subscribes to and reads the same characteristic should pass one; a
        backend without the ambiguity is free to ignore it.
        """
        ...

    async def unsubscribe(self, characteristic: str) -> None: ...

    async def services(self) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        """Discovered GATT layout: ``{service_uuid: [(char_uuid, (properties, ...))]}``."""
        ...

    def adapter_power_on_event(self) -> asyncio.Event:
        """The event that fires when the host's Bluetooth adapter powers on.

        Set on every off→on transition the backend can observe; the consumer clears it to
        arm for the next one. Single consumer by design — the session's reconnect loop —
        two clearers would race each other for the same edge.

        A backend that cannot observe adapter state returns an event that never fires,
        which degrades to exactly the behavior before #20: every reconnect backoff runs to
        completion. Never ``None`` — an unobservable adapter and a quiet one look the same
        to a waiter, so there is no reason to make callers tell them apart.
        """
        ...


class TransportError(Exception):
    """Any transport-level failure. Layers above should not need to know bleak's exceptions."""


Scanner = Callable[[float], Awaitable[list[DiscoveredDevice]]]
