"""Bleak-backed BLE transport. The only module in the project that imports ``bleak``.

Discover, connect, enumerate, read, subscribe, write. Connection establishment goes through
``bleak-retry-connector`` (issue #1): a single ``connect()`` is a *sample* of a link that
swings 22 dB while nothing moves (RL-010), and a daemon that gives up on the first refusal
is a daemon that is offline every time the radio dips.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakAbortedError,
    BleakClientWithServiceCache,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from ..protocol.constants import NAME_PREFIXES, SERVICE_UUID
from .base import DiscoveredDevice, NotifyCallback, TransportError

log = logging.getLogger(__name__)

#: How many times ``establish_connection`` may try before giving up. Four is the library's
#: own default and it is chosen for exactly our failure mode: transient BLE refusals that
#: succeed on a retry seconds later.
DEFAULT_CONNECT_ATTEMPTS = 4


@contextlib.contextmanager
def _bleak_errors_as_transport(operation: str) -> Iterator[None]:
    """Translate any ``bleak`` exception into a :class:`TransportError`.

    ``base.py`` promises that "layers above should not need to know bleak's exceptions",
    and the session layer takes that literally: it classifies by our vocabulary, sending
    :class:`TransportError` and ``OSError`` to a WARNING-with-backoff and *everything else*
    to ``log.exception`` — ERROR severity, a full traceback, and the word **unexpected**.
    Any bleak type that escapes this module is therefore not merely untranslated, it is
    actively mislabelled (#5, RL-024).

    The catch is deliberately the **base class**. Every exception bleak and
    ``bleak-retry-connector`` define derives from ``BleakError`` — measured, not assumed —
    so enumerating leaf types would be a list that silently falls out of date on the next
    upstream release, with a fresh traceback at 3am as the only notice. "Powering the
    adapter off" is among the most expected things that can happen to a BLE daemon; it
    reached the operator as an unexpected error only because ``BleakBluetoothNotAvailableError``
    was not in such a list.

    The bleak type name is kept in the message. Severity should drop, diagnosis should not.
    """
    try:
        yield
    except BleakError as exc:
        raise TransportError(f"{operation}: {type(exc).__name__}: {exc}") from exc


async def scan(timeout: float = 10.0, *, all_devices: bool = False) -> list[DiscoveredDevice]:
    """Scan for BedJet-looking devices.

    By default results are filtered to devices advertising the BedJet service UUID or a
    known name prefix. The brief asks us not to needlessly collect information about
    unrelated nearby Bluetooth devices, so the unfiltered view is opt-in and exists only
    to answer "is our unit advertising under a name we do not recognise?".
    """
    log.debug("scanning for %.1fs (all_devices=%s)", timeout, all_devices)
    found: list[DiscoveredDevice] = []

    with _bleak_errors_as_transport("scan failed"):
        results = await BleakScanner.discover(timeout=timeout, return_adv=True)

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

    def __init__(self, *, max_attempts: int = DEFAULT_CONNECT_ATTEMPTS) -> None:
        self._client: BleakClientWithServiceCache | None = None
        self._max_attempts = max_attempts
        # Bleak's CoreBluetooth backend keys pending GATT operations by characteristic
        # handle, so two overlapping reads of the same characteristic collide: one
        # cancels the other's future and they can return each other's data. That is how a
        # tail fragment was mistaken for a whole packet in RL-017. Serialise here, at the
        # layer that owns the backend, so no caller has to know.
        self._gatt = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _resolve(self, address: str, timeout: float) -> BLEDevice:
        """Turn an address into a live ``BLEDevice``, or explain why we cannot.

        ``establish_connection`` wants a device object rather than an address, which turns
        out to be the better shape anyway: "did not appear in a scan" and "appeared but
        refused the link" become two separate outcomes at the point where they differ,
        instead of two exception types we have to tell apart afterwards (RL-009).

        The scan is guarded because *this* is where the reconnect loop meets a powered-off
        adapter: it is the first bleak call of a reconnect, and it was the only unguarded
        one on that path while ``scan()`` above already wrapped its own ``discover``. That
        asymmetry, rather than the exception itself, was the defect in #5.
        """
        with _bleak_errors_as_transport(f"scanning for {address}"):
            device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is not None:
            return device
        raise TransportError(
            f"{address} did not appear in a {timeout:.0f}s scan, so no connection was "
            "attempted.\n"
            "The device is not advertising. Common causes, cheapest first:\n"
            "  · another client holds the link (the vendor app on a phone) — many BLE "
            "peripherals stop advertising while connected\n"
            "  · the unit lost power, or has gone into a deep idle\n"
            "  · it is out of range, or the radio path changed\n"
            "  · on macOS this address is a host-local CoreBluetooth UUID; if the "
            "device rotated its BLE address, macOS may now know it under a different "
            "one (see RL-009) — re-run `bedjet discover`"
        )

    async def connect(self, address: str, *, timeout: float = 20.0) -> None:
        """Connect, retrying transient failures.

        ``timeout`` bounds the *scan* that resolves the address. The per-attempt connect
        timeout and the backoff between attempts belong to ``bleak-retry-connector``, which
        varies them by error class — a link that is out of connection slots wants a
        different wait from one that timed out.

        **The GATT service cache is deliberately disabled.** It would let a reconnect skip
        service discovery, and the write path decides *with-response vs without-response*
        from the command characteristic's declared properties. A stale cached property
        table is therefore the one thing that could silently resurrect RL-018 — every
        command accepted by the radio and discarded before it reached the device. Rediscovery
        costs a fraction of a second on a link we hold for hours.
        """
        log.info(
            "connecting to %s (scan timeout %.1fs, up to %d attempt(s))",
            address,
            timeout,
            self._max_attempts,
        )
        device = await self._resolve(address, timeout)
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                device.name or address,
                max_attempts=self._max_attempts,
                use_services_cache=False,
                disconnected_callback=self._on_disconnected,
            )
        except BleakNotFoundError as exc:
            # It advertised for the scan and was gone by the time we reached for it. Not
            # the same as never having appeared, and not the same as a refusal.
            raise TransportError(
                f"{address} was found by the scan but had disappeared by the time we "
                f"connected, after {self._max_attempts} attempt(s): {exc}. The link is "
                "marginal — run `bedjet discover --repeat 5` and judge the RSSI spread "
                "(RL-010)."
            ) from exc
        except BleakOutOfConnectionSlotsError as exc:
            raise TransportError(
                f"the Bluetooth adapter is out of connection slots: {exc}. Something else "
                "on this host is holding BLE connections open."
            ) from exc
        except (BleakAbortedError, BleakError) as exc:
            # Found, but the link was refused or dropped, and retries did not help. The
            # BedJet permits exactly one BLE client at a time, which is the usual reason.
            raise TransportError(
                f"could not connect to {address} after {self._max_attempts} attempt(s): "
                f"{exc}. The device was found but refused the link. The BedJet allows only "
                "one BLE client at a time — check that the vendor app is not connected."
            ) from exc
        self._client = client
        log.info("connected to %s", address)

    def _on_disconnected(self, _client: object) -> None:
        """Called by bleak when the link drops, expectedly or otherwise.

        Log only. Reconnection is the service layer's decision, not the transport's: this
        object has no idea whether the disconnect was a fault or a deliberate yield of the
        device's single BLE slot back to its owner.
        """
        log.warning("BLE link dropped")

    async def disconnect(self) -> None:
        if self._client is not None:
            # Cleared before the await, so a disconnect that raises still leaves this
            # object believing it holds nothing. The alternative — reporting a link we do
            # not have — is the worse of the two errors for a device with one client slot.
            client, self._client = self._client, None
            with _bleak_errors_as_transport("disconnecting"):
                await client.disconnect()
            log.info("disconnected")

    def _require(self) -> BleakClientWithServiceCache:
        if self._client is None or not self._client.is_connected:
            raise TransportError("not connected")
        return self._client

    async def read(self, characteristic: str) -> bytes:
        async with self._gatt:
            with _bleak_errors_as_transport(f"reading {characteristic}"):
                data = bytes(await self._require().read_gatt_char(characteristic))
        log.debug("read  %s -> %s", characteristic, data.hex(" "))
        return data

    def _write_needs_response(self, characteristic: str) -> bool:
        """Choose the write type from the characteristic's own declared properties.

        BLE has two distinct write types, and they are separate GATT properties:
        ``write`` means **write-with-response**, ``write-without-response`` is its own
        property. A characteristic that declares only ``write`` does not accept
        write-without-response — and CoreBluetooth **silently drops** such a write. No
        error, no exception, nothing on the wire.

        RL-018: our command characteristic (`…2004`) declares exactly ``[write]``, and we
        were defaulting to without-response. Every command we sent was discarded by the
        local Bluetooth stack before it ever reached the BedJet.

        So this asks the device rather than assuming. Defaults to with-response when the
        characteristic cannot be found: it is the safer of the two, since it is the type
        that actually reports failure.
        """
        client = self._require()
        for service in client.services:
            for char in service.characteristics:
                if char.uuid.lower() == characteristic.lower():
                    props = {p.lower() for p in char.properties}
                    return "write-without-response" not in props
        return True

    async def write(
        self, characteristic: str, data: bytes, *, response: bool | None = None
    ) -> None:
        """Write to a characteristic.

        ``response=None`` (the default) selects the write type from the characteristic's
        declared properties, which is what you want — see :meth:`_write_needs_response`.
        Pass an explicit bool only to override deliberately.
        """
        if response is None:
            with _bleak_errors_as_transport(f"inspecting {characteristic}"):
                response = self._write_needs_response(characteristic)
        # Deliberately loud. Every write to this device is a physical event, and the log
        # is the record of what we asked a heater to do.
        log.warning(
            "WRITE %s <- %s (%s)",
            characteristic,
            data.hex(" "),
            "with response" if response else "without response",
        )
        async with self._gatt:
            with _bleak_errors_as_transport(f"writing {characteristic}"):
                await self._require().write_gatt_char(characteristic, data, response=response)

    async def subscribe(self, characteristic: str, callback: NotifyCallback) -> None:
        client = self._require()

        def _on_notify(_sender: object, data: bytearray) -> None:
            log.debug("notify %s -> %s", characteristic, bytes(data).hex(" "))
            callback(bytes(data))

        with _bleak_errors_as_transport(f"subscribing to {characteristic}"):
            await client.start_notify(characteristic, _on_notify)
        log.info("subscribed to %s", characteristic)

    async def unsubscribe(self, characteristic: str) -> None:
        with _bleak_errors_as_transport(f"unsubscribing from {characteristic}"):
            await self._require().stop_notify(characteristic)

    async def services(self) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
        client = self._require()
        layout: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        with _bleak_errors_as_transport("enumerating services"):
            for service in client.services:
                layout[service.uuid] = [
                    (char.uuid, tuple(char.properties)) for char in service.characteristics
                ]
        return layout
