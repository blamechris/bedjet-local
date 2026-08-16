"""Bleak-backed BLE transport. The only module in the project that imports ``bleak``.

Milestone 1 scope: discover, connect, enumerate, read, subscribe. ``write`` is implemented
because the interface requires it, but **nothing in this repository calls it yet** — see
``docs/SAFETY.md``: the first write to the device is a deliberate, attended, logged event
in Milestone 2, not a side effect of bring-up.
"""

from __future__ import annotations

import logging

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from ..protocol.constants import NAME_PREFIXES, SERVICE_UUID
from .base import DiscoveredDevice, NotifyCallback, TransportError

log = logging.getLogger(__name__)


async def scan(timeout: float = 10.0, *, all_devices: bool = False) -> list[DiscoveredDevice]:
    """Scan for BedJet-looking devices.

    By default results are filtered to devices advertising the BedJet service UUID or a
    known name prefix. The brief asks us not to needlessly collect information about
    unrelated nearby Bluetooth devices, so the unfiltered view is opt-in and exists only
    to answer "is our unit advertising under a name we do not recognise?".
    """
    log.debug("scanning for %.1fs (all_devices=%s)", timeout, all_devices)
    found: list[DiscoveredDevice] = []

    try:
        results = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except BleakError as exc:  # pragma: no cover - requires an adapter
        raise TransportError(f"scan failed: {exc}") from exc

    for device, adv in results.values():
        uuids = tuple(u.lower() for u in (adv.service_uuids or ()))
        name = adv.local_name or device.name
        looks_like_bedjet = SERVICE_UUID.lower() in uuids or (
            name is not None and name.upper().startswith(tuple(p.upper() for p in NAME_PREFIXES))
        )
        if not (all_devices or looks_like_bedjet):
            continue
        found.append(
            DiscoveredDevice(
                address=device.address,
                name=name,
                rssi=adv.rssi,
                service_uuids=uuids,
                manufacturer_data=tuple(
                    (k, bytes(v)) for k, v in (adv.manufacturer_data or {}).items()
                ),
            )
        )

    found.sort(key=lambda d: d.rssi if d.rssi is not None else -999, reverse=True)
    return found


class BleakTransport:
    """A :class:`~bedjet_local.transport.base.Transport` backed by a local BLE adapter."""

    def __init__(self) -> None:
        self._client: BleakClient | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, address: str, *, timeout: float = 20.0) -> None:
        log.info("connecting to %s (timeout %.1fs)", address, timeout)
        client = BleakClient(address, timeout=timeout)
        try:
            await client.connect()
        except BleakError as exc:
            # The BedJet permits exactly one BLE client at a time, so "the phone app is
            # connected" and "the device is out of range" both surface here. Say so,
            # rather than reporting a bare timeout that sends the reader to the wrong
            # problem.
            raise TransportError(
                f"could not connect to {address}: {exc}. "
                "The BedJet allows only one BLE client at a time — check that the vendor "
                "app is not connected, and that the unit is in range."
            ) from exc
        self._client = client
        log.info("connected to %s", address)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            log.info("disconnected")
            self._client = None

    def _require(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise TransportError("not connected")
        return self._client

    async def read(self, characteristic: str) -> bytes:
        data = bytes(await self._require().read_gatt_char(characteristic))
        log.debug("read  %s -> %s", characteristic, data.hex(" "))
        return data

    async def write(self, characteristic: str, data: bytes, *, response: bool = False) -> None:
        # Deliberately loud. Every write to this device is a physical event, and the log
        # is the record of what we asked a heater to do.
        log.warning("WRITE %s <- %s", characteristic, data.hex(" "))
        await self._require().write_gatt_char(characteristic, data, response=response)

    async def subscribe(self, characteristic: str, callback: NotifyCallback) -> None:
        client = self._require()

        def _on_notify(_sender: object, data: bytearray) -> None:
            log.debug("notify %s -> %s", characteristic, bytes(data).hex(" "))
            callback(bytes(data))

        await client.start_notify(characteristic, _on_notify)
        log.info("subscribed to %s", characteristic)

    async def unsubscribe(self, characteristic: str) -> None:
        await self._require().stop_notify(characteristic)

    async def services(self) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        client = self._require()
        layout: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        for service in client.services:
            layout[service.uuid] = [
                (char.uuid, tuple(char.properties)) for char in service.characteristics
            ]
        return layout
