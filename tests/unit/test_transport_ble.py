"""No bleak exception may leave the transport layer (#5, RL-024).

``base.py`` promises that layers above do not need to know bleak's exceptions, and the
session layer takes that at its word: it sends ``TransportError`` and ``OSError`` to a
WARNING with backoff, and everything else to ``log.exception`` — ERROR, a full traceback,
and the word *unexpected*. So a bleak type that escapes this layer is not merely
untranslated, it is **mislabelled**, and it trains an operator to ignore ERROR overnight.

These use fakes rather than an adapter: the translation is the contract, and it is
verifiable without a radio (AGENTS.md rule 4).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason, BleakError

from bedjet_local.transport import ble
from bedjet_local.transport.base import TransportError

#: What a powered-off adapter actually raises — the exception from RL-024's hardware run,
#: constructed the way bleak constructs it rather than approximated by a bare BleakError.
POWERED_OFF = BleakBluetoothNotAvailableError(
    "Bluetooth device is turned off", BleakBluetoothNotAvailableReason.POWERED_OFF
)


class _FakeClient:
    """A connected bleak client whose every GATT call fails the same way."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.is_connected = True

    @property
    def services(self) -> object:
        raise self._exc

    async def disconnect(self) -> None:
        raise self._exc

    async def read_gatt_char(self, _characteristic: str) -> bytes:
        raise self._exc

    async def write_gatt_char(self, _characteristic: str, _data: bytes, **_kw: object) -> None:
        raise self._exc

    async def start_notify(self, _characteristic: str, _cb: object) -> None:
        raise self._exc

    async def stop_notify(self, _characteristic: str) -> None:
        raise self._exc


def _transport_holding(exc: BaseException) -> ble.BleakTransport:
    transport = ble.BleakTransport()
    transport._client = _FakeClient(exc)  # type: ignore[assignment]
    return transport


# ── the premise ─────────────────────────────────────────────────────────────────────────


def test_every_bleak_exception_derives_from_bleak_error() -> None:
    """The base-class catch is only complete while this holds — so assert it, don't assume.

    Enumerating leaf types is what #5 *was*: ``BleakBluetoothNotAvailableError`` was missing
    from a list, and the symptom was a 3am traceback rather than a failing test. Catching
    ``BleakError`` fixes that only if upstream keeps deriving from it. If a future bleak
    adds an exception outside this hierarchy, this test is the notice — and the fix is to
    widen the catch in ``_bleak_errors_as_transport``, never to relax this.
    """
    import bleak.exc
    import bleak_retry_connector

    strays: list[str] = []
    for module in (bleak.exc, bleak_retry_connector):
        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseException)
                and obj.__module__.split(".")[0] in {"bleak", "bleak_retry_connector"}
                and not issubclass(obj, BleakError)
            ):
                strays.append(f"{module.__name__}.{name}")

    assert not strays, (
        f"{strays} do not derive from BleakError, so _bleak_errors_as_transport does not "
        f"catch them and the session layer will log them as unexpected. Widen the catch."
    )


# ── the reported failure ────────────────────────────────────────────────────────────────


async def test_a_powered_off_adapter_is_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5's regression, at the exact call that produced it.

    The reconnect loop's first bleak call is the scan inside ``_resolve``, and it was the
    only unguarded one on that path — ``scan()`` already wrapped its own ``discover``. The
    asymmetry was the defect.
    """

    class _Scanner:
        @staticmethod
        async def find_device_by_address(_address: str, timeout: float = 0.0) -> None:
            raise POWERED_OFF

    monkeypatch.setattr(ble, "BleakScanner", _Scanner)

    with pytest.raises(TransportError):
        await ble.BleakTransport().connect("AA:BB:CC:DD:EE:FF", timeout=0.1)


async def test_the_bleak_type_survives_in_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Severity should drop; diagnosis should not. Losing the type name would trade one
    unusable log line for another."""

    class _Scanner:
        @staticmethod
        async def find_device_by_address(_address: str, timeout: float = 0.0) -> None:
            raise POWERED_OFF

    monkeypatch.setattr(ble, "BleakScanner", _Scanner)

    with pytest.raises(TransportError) as caught:
        await ble.BleakTransport().connect("AA:BB:CC:DD:EE:FF", timeout=0.1)

    assert "BleakBluetoothNotAvailableError" in str(caught.value)
    assert "turned off" in str(caught.value)
    assert caught.value.__cause__ is POWERED_OFF, "the original must stay chained for --debug"


# ── the rest of the boundary ────────────────────────────────────────────────────────────


async def test_read_translates() -> None:
    with pytest.raises(TransportError):
        await _transport_holding(POWERED_OFF).read("0000dead-0000-1000-8000-00805f9b34fb")


async def test_write_translates() -> None:
    transport = _transport_holding(POWERED_OFF)
    with pytest.raises(TransportError):
        await transport.write("0000dead-0000-1000-8000-00805f9b34fb", b"\x01", response=True)


async def test_choosing_the_write_type_translates() -> None:
    """``_write_needs_response`` reads the characteristic table off the live client, so it
    touches bleak too — and it runs *before* the write on the default path."""
    transport = _transport_holding(POWERED_OFF)
    with pytest.raises(TransportError):
        await transport.write("0000dead-0000-1000-8000-00805f9b34fb", b"\x01")


async def test_subscribe_translates() -> None:
    transport = _transport_holding(POWERED_OFF)
    with pytest.raises(TransportError):
        await transport.subscribe("0000dead-0000-1000-8000-00805f9b34fb", lambda _data: None)


async def test_unsubscribe_translates() -> None:
    with pytest.raises(TransportError):
        await _transport_holding(POWERED_OFF).unsubscribe("0000dead-0000-1000-8000-00805f9b34fb")


async def test_services_translates() -> None:
    with pytest.raises(TransportError):
        await _transport_holding(POWERED_OFF).services()


async def test_disconnect_translates() -> None:
    with pytest.raises(TransportError):
        await _transport_holding(POWERED_OFF).disconnect()


async def test_a_failed_disconnect_still_drops_the_client() -> None:
    """Reporting a link we do not have is the worse of the two errors on a device with one
    client slot: the supervisor would see ``is_connected`` and never reconnect."""
    transport = _transport_holding(POWERED_OFF)
    with pytest.raises(TransportError):
        await transport.disconnect()
    assert transport.is_connected is False


# ── what must not change ────────────────────────────────────────────────────────────────


async def test_not_connected_is_not_rewrapped() -> None:
    """``_require`` already raises TransportError. Wrapping it again would bury the message
    that tells an operator the link is simply down."""
    with pytest.raises(TransportError, match="not connected"):
        await ble.BleakTransport().read("0000dead-0000-1000-8000-00805f9b34fb")


async def test_cancellation_is_not_swallowed() -> None:
    """``CancelledError`` is not an ``Exception`` and must stay that way: the supervisor
    cancels this task on shutdown, and a cancel translated into TransportError would be
    retried instead of honoured."""
    transport = _transport_holding(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await transport.read("0000dead-0000-1000-8000-00805f9b34fb")
