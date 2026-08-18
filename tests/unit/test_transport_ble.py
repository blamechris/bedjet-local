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
import contextlib
import importlib
import inspect
import pkgutil

import bleak
import bleak_retry_connector
import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason, BleakError

from bedjet_local.transport import ble
from bedjet_local.transport.base import TransportError

#: What a powered-off adapter actually raises — the exception from RL-024's hardware run,
#: constructed the way bleak constructs it rather than approximated by a bare BleakError.
POWERED_OFF = BleakBluetoothNotAvailableError(
    "Bluetooth device is turned off", BleakBluetoothNotAvailableReason.POWERED_OFF
)

#: Bases that mark an exception as a **bug**, not a link condition — and therefore as one
#: that must *not* be translated.
#:
#: ``TransportError`` is the reconnect loop's cue to back off and try again. Feeding a
#: programming error into it would retry a defect forever at WARNING, which is the mirror
#: image of #5: #5 made an expected condition look like a crash, and this would make a crash
#: look like an expected condition. A traceback is the correct outcome for these, so the
#: catch in ``_bleak_errors_as_transport`` is ``BleakError`` and not ``Exception``.
#:
#: Real instance: ``bleak.backends.bluezdbus.signals.InvalidMessageTypeError`` is a
#: ``TypeError`` raised when a D-Bus match rule is built with a message type outside a fixed
#: list. Only bleak's own code builds those, always with ``"signal"``, so it fires only if
#: bleak has a bug — exactly when a stack trace is what you want. It is invisible on macOS
#: (the BlueZ backend does not import) and appears on Linux, which is where CI found it.
NOT_LINK_CONDITIONS = (TypeError, ValueError, AttributeError, KeyError, IndexError)


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

    async def start_notify(self, _characteristic: str, _cb: object, **_kw: object) -> None:
        raise self._exc

    async def stop_notify(self, _characteristic: str) -> None:
        raise self._exc


def _transport_holding(exc: BaseException) -> ble.BleakTransport:
    transport = ble.BleakTransport()
    transport._client = _FakeClient(exc)  # type: ignore[assignment]
    return transport


# ── the premise ─────────────────────────────────────────────────────────────────────────


def test_every_bleak_exception_is_a_link_condition_or_a_bug() -> None:
    """The base-class catch is only complete while this holds — so assert it, don't assume.

    Enumerating leaf types is what #5 *was*: ``BleakBluetoothNotAvailableError`` was missing
    from a list, and the symptom was a 3am traceback rather than a failing test. Catching
    ``BleakError`` fixes that only if upstream keeps deriving from it. If a future bleak
    adds an exception outside this hierarchy, this test is the notice — and the fix is to
    widen the catch in ``_bleak_errors_as_transport``, never to relax this.

    It walks the **packages**, not the two public namespaces. Reading ``dir(bleak.exc)`` and
    ``dir(bleak_retry_connector)`` sees 10 of the 27 exception classes on bleak 3.0.2, and
    the ones it misses — anything defined in a backend and not re-exported — are precisely
    where a platform-specific stray would appear. A guard that inspects a third of the
    surface is the same shape of mistake as the list this replaced. The walk found
    ``InvalidMessageTypeError`` on the Linux runner within one CI run of being written, and
    it is invisible from macOS.

    ``NOT_LINK_CONDITIONS`` is exempt, and that exemption is the policy rather than a
    concession — see its comment. This therefore asserts something sharper than "everything
    derives from BleakError": **every bleak exception is either a link condition we
    translate, or a bug we deliberately let crash.** Nothing may sit between the two.
    """
    strays: list[str] = []
    checked: set[type[BaseException]] = set()
    for package in (bleak, bleak_retry_connector):
        modules = [package]
        for found in pkgutil.walk_packages(
            getattr(package, "__path__", []), package.__name__ + "."
        ):
            with contextlib.suppress(Exception):  # backends for other platforms will not import
                modules.append(importlib.import_module(found.name))

        for module in modules:
            for obj in vars(module).values():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseException)
                    and obj.__module__.split(".")[0] in {"bleak", "bleak_retry_connector"}
                    and obj not in checked
                ):
                    checked.add(obj)
                    if not issubclass(obj, (BleakError, *NOT_LINK_CONDITIONS)):
                        strays.append(f"{obj.__module__}.{obj.__qualname__}")

    assert checked, "walked no exception classes at all — the walk itself is broken"
    assert not strays, (
        f"{sorted(strays)} derive from neither BleakError nor a programming-error base, so "
        f"_bleak_errors_as_transport does not translate them and the session layer will log "
        f"them as unexpected (#5). Decide which they are: a link condition belongs in the "
        f"catch in transport/ble.py, a bug belongs in NOT_LINK_CONDITIONS with a reason. "
        f"Do not relax this test."
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


async def test_subscribe_hands_bleak_the_notification_discriminator() -> None:
    """The #6 guard exists only if the discriminator actually reaches bleak.

    CoreBluetooth funnels notifications and read responses for one characteristic through
    a single delegate callback, and while a read is pending bleak assumes every value is
    the response — unless ``start_notify``'s ``cb`` argument carries a discriminator to
    ask. A notification mistaken for the response steals the read's future, and the true
    response then raises ``InvalidStateError`` on the already-resolved future, inside
    bleak, past every ``try`` of ours (RL-033 §6). So the wiring *is* the fix: record what
    ``start_notify`` receives and assert the function arrives intact, None staying None.
    """
    received: list[object] = []

    class _Recorder:
        is_connected = True

        async def start_notify(self, _characteristic: str, _cb: object, **kwargs: object) -> None:
            received.append(kwargs.get("cb"))

    transport = ble.BleakTransport()
    transport._client = _Recorder()  # type: ignore[assignment]

    def is_notification(_data: bytes) -> bool:  # pragma: no cover - never called here
        return True

    uuid = "0000dead-0000-1000-8000-00805f9b34fb"
    await transport.subscribe(uuid, lambda _data: None, notification_discriminator=is_notification)
    await transport.subscribe(uuid, lambda _data: None)

    assert received == [
        {"notification_discriminator": is_notification},
        {"notification_discriminator": None},
    ]


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


async def test_a_programming_error_is_not_translated() -> None:
    """The other half of the policy in ``NOT_LINK_CONDITIONS``, asserted on the real code.

    ``bleak.backends.bluezdbus.signals.InvalidMessageTypeError`` is a ``TypeError`` that can
    only fire if bleak builds a malformed D-Bus match rule. Translating it would hand a
    defect to the reconnect loop, which would back off and retry it every few seconds,
    forever, at WARNING — the mirror image of #5. It must keep its traceback.
    """
    transport = _transport_holding(TypeError("invalid message type: nonsense"))
    with pytest.raises(TypeError):
        await transport.read("0000dead-0000-1000-8000-00805f9b34fb")


async def test_cancellation_is_not_swallowed() -> None:
    """``CancelledError`` is not an ``Exception`` and must stay that way: the supervisor
    cancels this task on shutdown, and a cancel translated into TransportError would be
    retried instead of honoured."""
    transport = _transport_holding(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await transport.read("0000dead-0000-1000-8000-00805f9b34fb")
