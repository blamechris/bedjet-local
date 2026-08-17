"""The CLI: bring-up, control, and the two daemons.

Ordered the way ``docs/SAFETY.md`` orders things. ``discover``, ``identify`` and ``watch``
are read-only. ``off``, ``fan``, ``mode`` and ``temp`` write — each behind a typed
confirmation, each verifying the result against the device's own status rather than assuming
it, and each with a ``--dry-run`` that rehearses everything but the write.

``serve`` and ``mqtt`` are different in kind from all of them: they **hold** the BLE link
instead of borrowing it. The BedJet permits one client at a time and this unit has no working
physical remote, so both print the yield command on startup — the owner must be able to
reclaim their heater without hunting for a process to kill.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .device import registry
from .device.state import BedJetState
from .protocol import encode
from .protocol.constants import (
    CHARACTERISTICS,
    NAME_UUID,
    SERVICE_UUID,
    CommandMode,
    c_to_f,
)
from .protocol.packets import StatusPacket
from .service.commander import (
    UNLOCKED_MODES,
    Commander,
    CommandRefused,
    CommandUnverified,
)
from .service.reader import StatusReader
from .service.session import DeviceSession
from .transport.base import DiscoveredDevice
from .transport.ble import BleakTransport, scan

log = logging.getLogger("bedjet")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@dataclass
class _Dedup:
    """Tracks consecutive identical packets so `watch` prints transitions, not repeats."""

    last: bytes | None = None
    repeats: int = 0


def _short(address: str, known: dict[str, registry.KnownDevice]) -> str:
    """A short, unambiguous label for one device: its registry name, or an address stub."""
    entry = known.get(address.lower())
    return entry.label if entry is not None else f"{address.split('-')[0]}…"


async def cmd_discover(args: argparse.Namespace) -> int:
    """Bring-up steps 1-3: discover, identify, inspect advertising data.

    With ``--repeat`` this becomes a presence survey rather than a snapshot. RL-008
    established that a single scan is a *sample*, not a census — a healthy device can be
    absent from one 10 s window — so "is it there?" and "how good is this location?" both
    need several rounds, not one.
    """
    known = registry.load()
    seen: dict[str, list[DiscoveredDevice]] = {}

    for index in range(args.repeat):
        if args.repeat > 1:
            print(f"Scan {index + 1}/{args.repeat} ({args.timeout:.0f}s)...")
        else:
            print(f"Scanning for {args.timeout:.0f}s...")
        devices = await scan(timeout=args.timeout, all_devices=args.all)
        for device in devices:
            seen.setdefault(device.address.lower(), []).append(device)
        if args.repeat > 1:
            # Label each reading with WHICH device it came from. Results are sorted by
            # RSSI, so two same-named units swap positions between rounds — an unlabelled
            # list reads as if one device's readings belonged to the other (RL-010).
            summary = (
                ", ".join(f"{_short(d.address, known)} {d.rssi}dBm" for d in devices) or "nothing"
            )
            print(f"  → {summary}")
            if index + 1 < args.repeat:
                await asyncio.sleep(args.interval)

    if not seen:
        print("\nNo BedJet found in any scan.")
        print("  · Is the unit powered?")
        print("  · Is the vendor app connected? Only one BLE client at a time is allowed,")
        print("    and many BLE peripherals stop advertising entirely while connected.")
        print("  · Are you within ~10m, ideally same room?")
        print("  · Try --all to list every nearby device, in case ours advertises")
        print("    under a name we do not recognise.")
        print("  · Try --repeat 5: a single scan is a sample, not a census (RL-008).")
        return 1

    print(f"\nFound {len(seen)} device(s) across {args.repeat} scan(s):\n")
    unregistered = 0
    for address, samples in seen.items():
        latest = samples[-1]
        print(f"  {latest.describe()}")
        if args.repeat > 1:
            rssis = [d.rssi for d in samples if d.rssi is not None]
            if rssis:
                spread = max(rssis) - min(rssis)
                print(
                    f"      seen in {len(samples)}/{args.repeat} scans, "
                    f"{min(rssis)}..{max(rssis)} dBm (spread {spread} dB)"
                )
                # 10 dB is a 10x power swing. Beyond that the link is fading badly enough
                # that a connect attempt can land in a null and fail as "not found"
                # (RL-009/RL-010) — which looks like a dead device, not a weak one.
                if spread >= 10:
                    print(
                        f"      ⚠️  {spread} dB swing while stationary — marginal link. "
                        "Connects may fail intermittently."
                    )
            else:
                print(f"      seen in {len(samples)}/{args.repeat} scans, rssi unknown")
        if latest.service_uuids:
            print(f"      services: {', '.join(latest.service_uuids)}")
        for company_id, payload in latest.manufacturer_data:
            print(f"      mfr 0x{company_id:04x}: {payload.hex(' ')}")
        if SERVICE_UUID.lower() in latest.service_uuids:
            print("      ✅ advertises the BedJet service UUID")
        entry = known.get(address)
        if entry is not None:
            print(f"      ✅ ours: {entry.label}")
        else:
            unregistered += 1
            print("      ⛔ not in the device registry — will not be connected to")
        print()

    missing = [d for d in known.values() if d.address.lower() not in seen]
    for absent in missing:
        print(f"  ⚠️  registered device '{absent.label}' was not seen in any scan.")
        print("      Powered? Vendor app holding the link? On macOS the address is a")
        print("      host-local UUID and can change if the device rotates its BLE address.\n")

    if unregistered:
        # RL-006: a neighbour's BedJet is indistinguishable from ours by advertisement.
        print(
            f"{unregistered} device(s) are not registered as ours. A BedJet in radio range "
            "looks\nidentical to yours — same name, same service UUID, no manufacturer data. "
            "Identify\nyours with the power test (RL-006) before adding it to "
            f"{registry.registry_path()}.\n"
        )
    print("Record these observations in docs/research/RESEARCH-LOG.md before you forget them.")
    return 0 if not missing else 1


def _check_ownership(args: argparse.Namespace) -> None:
    """Refuse to connect to a device that is not ours.

    RL-006: two BedJets were in range during bring-up and only one was ours. Connecting
    takes the device's single BLE slot, so connecting to a stranger's unit would knock them
    off their own heater. ``--force`` exists for the moment of first identification and
    says so out loud.
    """
    if args.force:
        print(
            f"⚠️  --force: connecting to {args.address} without checking the device "
            f"registry.\n    Only do this if you have confirmed by physical test that this "
            f"unit is yours.\n"
        )
        return
    known = registry.require_known(args.address)
    print(f"device registry: {args.address} is ours ({known.label})")


async def cmd_identify(args: argparse.Namespace) -> int:
    """Bring-up steps 4-6: connect and enumerate the GATT tree. Reads only."""
    _check_ownership(args)
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)
    try:
        print(f"\nConnected to {args.address}\n")
        layout = await transport.services()
        known = {uuid.lower(): label for uuid, label in CHARACTERISTICS.items()}
        known[SERVICE_UUID.lower()] = "BedJet service"
        unexpected: list[str] = []
        for service_uuid, characteristics in layout.items():
            label = known.get(service_uuid.lower(), "")
            print(f"service {service_uuid}  {label}")
            for char_uuid, properties in characteristics:
                char_label = known.get(char_uuid.lower())
                if char_label is None and service_uuid.lower() == SERVICE_UUID.lower():
                    char_label = "⚠️ NOT SEEN BEFORE — a finding, log it"
                    unexpected.append(char_uuid)
                print(f"    char {char_uuid}  [{', '.join(properties)}]  {char_label or ''}")
            print()

        if unexpected:
            print(
                f"{len(unexpected)} characteristic(s) not in PROTOCOL.md. Either the firmware "
                "changed\nor our table is incomplete — either way it belongs in the research "
                "log.\n"
            )

        try:
            name = await transport.read(NAME_UUID)
            print(f"device name characteristic: {name!r}  (hex: {name.hex(' ')})")
        except Exception as exc:
            print(f"device name characteristic unreadable: {exc}")

        print(
            "\nCompare this against docs/protocol/PROTOCOL.md. Any characteristic not "
            "listed there is a finding — log it."
        )
    finally:
        await transport.disconnect()
    return 0


async def cmd_watch(args: argparse.Namespace) -> int:
    """Bring-up steps 7-10: subscribe, observe, decode, and compare to a known state.

    ``--seconds`` time-boxes the session so the BLE link is released on a timer rather than
    on a clean exit. With no working physical remote (docs/SAFETY.md), the vendor app is the
    owner's only other control path and it cannot connect while we hold the link — so a
    session that hangs locks them out of their own heater. The timer is the guard.
    """
    _check_ownership(args)
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)

    captures: list[bytes] = []
    # The device re-sends the same status several times a second. Printing every one buries
    # the interesting moment — the transition — in identical lines, and makes the operator
    # hold Ctrl-C to escape. Only changes are printed; repeats collapse into a counter.
    seen = _Dedup()
    done = asyncio.Event()

    def flush_repeats() -> None:
        if seen.repeats:
            print(f"           … {seen.repeats} identical packet(s)")
            seen.repeats = 0

    def on_state(state: BedJetState, packet: StatusPacket) -> None:
        captures.append(packet.raw)

        if packet.raw == seen.last:
            seen.repeats += 1
            return
        flush_repeats()
        seen.last = packet.raw

        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        print(f"[{stamp}] {state.describe()}")
        if args.raw:
            print(f"          raw: {packet.raw.hex(' ')}")
            print(f"          hdr: {packet.header.hex(' ')}")
            print(f"          unknown[19:26]: {packet.unknown_region.hex(' ')}")
        for anomaly in packet.anomalies:
            print(f"          ⚠️  {anomaly}")

        if args.packets and len({bytes(c) for c in captures}) >= args.packets:
            done.set()

    reader = StatusReader(transport, on_state)
    await reader.start()

    print("\nWatching. The BedJet notifies rapidly while ON and is mostly silent while OFF —")
    print("silence here is not necessarily a fault.")
    if args.seconds:
        print(
            f"Releasing the link automatically after {args.seconds:.0f}s. Ctrl-C to stop early.\n"
        )
    else:
        print("⚠️  No --seconds limit: the vendor app cannot connect until this exits.\n")

    deadline: float | None = args.seconds if args.seconds else None
    elapsed = 0.0
    try:
        while deadline is None or elapsed < deadline:
            await asyncio.sleep(1.0)
            elapsed += 1.0
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # Always release the link. The owner's only other control path needs it back.
        await reader.stop()
        await transport.disconnect()
        print("\nLink released — the vendor app can connect again.")

    distinct = len({bytes(c) for c in captures})
    print(
        f"\n{reader.packets_seen} packets seen, {distinct} distinct "
        f"({reader.partials_seen} needed a follow-up read)."
    )
    if args.save and captures:
        args.save.write_bytes(captures[-1])
        print(f"Saved last packet to {args.save}")
        print("Record its provenance in tests/fixtures/PROVENANCE.md — date, firmware,")
        print("and the device state it was captured in. An unlabelled fixture is worthless.")
    return 0


async def cmd_off(args: argparse.Namespace) -> int:
    """⚠️ THE ONLY COMMAND THAT WRITES TO THE DEVICE. Sends OFF, then verifies it happened.

    Bring-up step 11 (docs/SAFETY.md). Off is first because it is the only command whose
    failure mode is a device that keeps doing what it was already doing.
    """
    _check_ownership(args)
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)
    commander = Commander(transport, settle_timeout=args.settle)

    try:
        await commander.start()
        before, _ = await commander.wait_for_state(args.settle)
        print(f"\ncurrent state: {before.describe()}")

        if args.dry_run:
            result = await commander.send_off(dry_run=True)
            print(
                f"\nDRY RUN — would write: {result.payload.hex(' ')} to the command "
                f"characteristic.\nNothing was sent. Re-run without --dry-run to send it."
            )
            return 0

        if not args.yes:
            print("\n⚠️  This writes to a mains-powered heater. Before confirming:")
            print("    · you are in the room and can see the unit")
            print("    · you know where its plug is — unplugging is the escape hatch")
            print("    · the vendor app is closed (it cannot connect while we hold the link)")
            print(f"\n    About to send: {encode.turn_off().hex(' ')}  (mode -> off)")
            if input("\n    Type 'off' to send, anything else to abort: ").strip() != "off":
                print("aborted — nothing was sent.")
                return 1

        result = await commander.send_off()
    except CommandRefused as exc:
        print(f"\n⛔ refused, nothing sent: {exc}")
        return 1
    except CommandUnverified as exc:
        print(f"\n❌ UNVERIFIED: {exc}")
        return 2
    finally:
        await commander.stop()
        await transport.disconnect()
        print("\nLink released — the vendor app can connect again.")

    print("\n✅ verified: the device did what we asked.")
    print(f"    sent:   {result.payload.hex(' ')}")
    print(f"    before: {result.before.describe()}")
    print(f"    after:  {result.after.describe()}")
    print("\nThe OFF command is now VERIFIED on our hardware rather than upstream guesswork.")
    print("Record it in docs/research/RESEARCH-LOG.md and promote the row in PROTOCOL.md.")
    if args.save:
        args.save.write_bytes(result.after_packet.raw)
        print(f"Saved the post-command packet to {args.save}")
    return 0


async def cmd_fan(args: argparse.Namespace) -> int:
    """⚠️ WRITES. Set fan speed and verify it took effect. Bring-up step 13.

    Thermally inert and directly observable, and it exercises a different opcode from OFF —
    which is the point of doing it second.
    """
    _check_ownership(args)
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)
    commander = Commander(transport, settle_timeout=args.settle)

    try:
        await commander.start()
        before, _ = await commander.wait_for_state(args.settle)
        print(f"\ncurrent state: {before.describe()}")

        if args.dry_run:
            result = await commander.set_fan_percent(args.percent, dry_run=True)
            print(f"\nDRY RUN — would write: {result.payload.hex(' ')}. Nothing was sent.")
            return 0

        if not args.yes:
            payload = encode.set_fan_percent(args.percent)
            print(
                f"\n⚠️  This writes to the device. About to send: {payload.hex(' ')}  "
                f"(fan -> {args.percent}%)"
            )
            print("    The unit must be running, and you should be able to hear the change.")
            if input("\n    Type 'fan' to send, anything else to abort: ").strip() != "fan":
                print("aborted — nothing was sent.")
                return 1

        result = await commander.set_fan_percent(args.percent)
    except CommandRefused as exc:
        print(f"\n⛔ refused, nothing sent: {exc}")
        return 1
    except CommandUnverified as exc:
        print(f"\n❌ UNVERIFIED: {exc}")
        return 2
    finally:
        await commander.stop()
        await transport.disconnect()
        print("\nLink released — the vendor app can connect again.")

    print("\n✅ verified: the device did what we asked.")
    print(f"    sent:   {result.payload.hex(' ')}")
    print(f"    before: {result.before.describe()}")
    print(f"    after:  {result.after.describe()}")
    print("\nOpcode 0x07 is now VERIFIED. Record it in docs/research/RESEARCH-LOG.md.")
    return 0


async def cmd_mode(args: argparse.Namespace) -> int:
    """⚠️ WRITES. Select a mode and verify it took effect. Bring-up step 14.

    Only the thermally safe operands are available: the heating modes are refused by
    ``Commander`` itself, not merely absent from this parser.
    """
    _check_ownership(args)
    mode = CommandMode[args.mode.upper()]
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)
    commander = Commander(transport, settle_timeout=args.settle)

    try:
        await commander.start()
        before, _ = await commander.wait_for_state(args.settle)
        print(f"\ncurrent state: {before.describe()}")

        if args.dry_run:
            result = await commander.set_mode(mode, dry_run=True)
            print(f"\nDRY RUN — would write: {result.payload.hex(' ')}. Nothing was sent.")
            return 0

        if not args.yes:
            payload = encode.set_mode(mode)
            print(
                f"\n⚠️  This writes to the device. About to send: {payload.hex(' ')}  "
                f"(mode -> {mode.name.lower()})"
            )
            if input("\n    Type 'go' to send, anything else to abort: ").strip() != "go":
                print("aborted — nothing was sent.")
                return 1

        result = await commander.set_mode(mode)
    except CommandRefused as exc:
        print(f"\n⛔ refused, nothing sent: {exc}")
        return 1
    except CommandUnverified as exc:
        print(f"\n❌ UNVERIFIED: {exc}")
        return 2
    finally:
        await commander.stop()
        await transport.disconnect()
        print("\nLink released — the vendor app can connect again.")

    print("\n✅ verified: the device did what we asked.")
    print(f"    sent:   {result.payload.hex(' ')}  (command {mode.name})")
    print(f"    before: {result.before.describe()}")
    print(f"    after:  {result.after.describe()}")
    print(
        f"\nCommand mode {mode.name} (0x{int(mode):02x}) is now VERIFIED — and note it "
        f"produced\nstatus mode {result.after.mode.name if result.after.mode else '?'}, a "
        f"different value. Record it in the research log."
    )
    return 0


async def cmd_temp(args: argparse.Namespace) -> int:
    """⚠️ WRITES. Set the target temperature and verify it. Bring-up step 14.

    Bounded by the range the device reports for its *current* mode, so this is safe while
    cooling and cannot be used to reach a heating target from a cooling mode.
    """
    _check_ownership(args)
    celsius = args.value if args.celsius else (args.value - 32) * 5 / 9
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)
    commander = Commander(transport, settle_timeout=args.settle)

    try:
        await commander.start()
        before, packet = await commander.wait_for_state(args.settle)
        print(f"\ncurrent state: {before.describe()}")
        if packet.min_temp_c is not None and packet.max_temp_c is not None:
            print(
                f"device-reported range for this mode: {packet.min_temp_c:.1f}"
                f"-{packet.max_temp_c:.1f}C "
                f"({c_to_f(packet.min_temp_c):.0f}-{c_to_f(packet.max_temp_c):.0f}F)"
            )

        rounded = round(celsius * 2) / 2
        if abs(rounded - celsius) > 0.01:
            print(
                f"\nnote: {celsius:.2f}C rounds to {rounded:.1f}C "
                f"({c_to_f(rounded):.1f}F) — the wire has 0.5C granularity"
            )

        if args.dry_run:
            result = await commander.set_temperature(celsius, dry_run=True)
            print(f"\nDRY RUN — would write: {result.payload.hex(' ')}. Nothing was sent.")
            return 0

        if not args.yes:
            print(
                f"\n⚠️  This writes to the device. Target -> {rounded:.1f}C ({c_to_f(rounded):.1f}F)"
            )
            if input("\n    Type 'go' to send, anything else to abort: ").strip() != "go":
                print("aborted — nothing was sent.")
                return 1

        result = await commander.set_temperature(celsius)
    except CommandRefused as exc:
        print(f"\n⛔ refused, nothing sent: {exc}")
        return 1
    except encode.CommandError as exc:
        print(f"\n⛔ rejected before sending: {exc}")
        return 1
    except CommandUnverified as exc:
        print(f"\n❌ UNVERIFIED: {exc}")
        return 2
    finally:
        await commander.stop()
        await transport.disconnect()
        print("\nLink released — the vendor app can connect again.")

    print("\n✅ verified: the device did what we asked.")
    print(f"    sent:   {result.payload.hex(' ')}")
    print(f"    before: {result.before.describe()}")
    print(f"    after:  {result.after.describe()}")
    print("\nOpcode 0x03 is now VERIFIED. Record it in docs/research/RESEARCH-LOG.md.")
    return 0


_SHUTDOWN_GRACE_S = 10.0


@contextlib.asynccontextmanager
async def _stop_signals() -> AsyncIterator[asyncio.Event]:
    """An event set by SIGTERM or SIGINT, so both stop a daemon the same way.

    Ctrl-C raises ``KeyboardInterrupt``; SIGTERM does not. Python's default action for
    SIGTERM terminates the process outright, skipping every ``finally`` on the way out —
    which is harmless for a command that borrows the link and wrong for a daemon that holds
    it, because SIGTERM is precisely what ``launchctl bootout``, any process supervisor, and
    a reboot all send. Left unhandled, the one shutdown path with nobody watching is the one
    that never releases the device.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _received(sig: signal.Signals) -> None:
        print(f"\n{sig.name} received — releasing the link.")
        stopping.set()

    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _received, sig)
        except (NotImplementedError, RuntimeError):
            # Not a Unix event loop. Ctrl-C still arrives as KeyboardInterrupt, and SIGTERM
            # keeps its default behaviour — no worse than before, and not worth refusing to
            # start over.
            continue
        installed.append(sig)
    try:
        yield stopping
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


async def _release(*closers: Callable[[], Awaitable[object]]) -> None:
    """Run the shutdown steps, and never take longer than the grace period doing it.

    A supervisor that sent SIGTERM sends SIGKILL shortly after — launchd's default is 20
    seconds — so a graceful release that hangs is strictly worse than an abrupt one: it
    burns the window and gets killed anyway, having reported nothing. Bounding it keeps the
    ordinary case clean without betting the shutdown on a BLE call that may never return.
    """
    try:
        async with asyncio.timeout(_SHUTDOWN_GRACE_S):
            for close in closers:
                await close()
    except TimeoutError:
        print(
            f"\n⚠️  Shutdown exceeded {_SHUTDOWN_GRACE_S:.0f}s and was abandoned. The OS "
            "releases the link when this process exits, but the device was not told."
        )
        return
    print("\nLink released — the vendor app can connect again.")


async def cmd_serve(args: argparse.Namespace) -> int:
    """⚠️ HOLDS THE LINK AND CAN WRITE. Run the local API daemon (Milestone 3).

    Unlike every other subcommand, this one keeps the BLE link for as long as it runs — and
    the BedJet allows one client at a time, so while it runs the vendor app cannot connect.
    That is why the API has a yield endpoint and why this prints how to use it: the owner
    must always be able to get their own heater back without killing a process they may not
    know how to find.
    """
    _check_ownership(args)
    try:
        from .api import BedJetAPI
        from .integrations.http_ws import ConfigurationRefused, ServerConfig, serve
    except ImportError as exc:
        print(f"⛔ the HTTP adapter needs its extra: `uv sync --extra http` ({exc})")
        return 1

    token = args.token or os.environ.get("BEDJET_TOKEN")
    try:
        config = ServerConfig(
            host=args.host,
            port=args.port,
            token=token,
            allowed_hosts=frozenset(args.allow_host or ()),
        )
    except ConfigurationRefused as exc:
        print(f"\n⛔ {exc}")
        return 1

    transport = BleakTransport()
    session = DeviceSession(
        transport,
        args.address,
        connect_timeout=args.timeout,
        settle_timeout=args.settle,
    )
    await session.start()
    api = BedJetAPI(session)
    runner = await serve(api, config)

    print(f"\nServing http://{config.host}:{config.port}/api/v1")
    print(f"  auth:  {'Authorization: Bearer <token>' if token else 'none (loopback only)'}")
    print("  state: GET /api/v1/state · live: GET /api/v1/ws")
    print(
        "\n⚠️  This process holds the BedJet's only BLE slot. The vendor app cannot "
        "connect while\n    it runs. To hand the device back for five minutes:\n"
        f"      curl -X POST http://{config.host}:{config.port}/api/v1/link/yield "
        "-d '{\"seconds\": 300}'\n"
    )
    print("Ctrl-C or SIGTERM to stop.\n")

    async with _stop_signals() as stopping:
        try:
            await stopping.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await _release(runner.cleanup, session.stop)
    return 0


async def cmd_mqtt(args: argparse.Namespace) -> int:
    """⚠️ HOLDS THE LINK AND CAN WRITE. Bridge the device to an MQTT broker.

    A peer of ``serve``, not a layer on top of it: run either, or both. Home Assistant
    discovery is on by default, which is the point of the bridge existing.
    """
    _check_ownership(args)
    try:
        from .api import BedJetAPI
        from .integrations.mqtt import MqttBridge, MqttConfig
    except ImportError as exc:
        print(f"⛔ the MQTT adapter needs its extra: `uv sync --extra mqtt` ({exc})")
        return 1

    config = MqttConfig(
        hostname=args.broker,
        port=args.broker_port,
        username=args.username or os.environ.get("BEDJET_MQTT_USERNAME"),
        password=os.environ.get("BEDJET_MQTT_PASSWORD"),
        base_topic=args.base_topic,
        device_id=args.device_id,
        discovery=not args.no_discovery,
    )

    transport = BleakTransport()
    session = DeviceSession(
        transport, args.address, connect_timeout=args.timeout, settle_timeout=args.settle
    )
    await session.start()
    bridge = MqttBridge(BedJetAPI(session), config)

    print(f"\nBridging to mqtt://{config.hostname}:{config.port}")
    print(f"  state:    {config.state_topic}")
    print(f"  commands: {config.command_wildcard}")
    print(f"  results:  {config.result_topic}")
    print(f"  discovery: {'on' if config.discovery else 'off'}")
    print(
        "\n⚠️  This process holds the BedJet's only BLE slot. To hand the device back for "
        f"five minutes:\n      mosquitto_pub -t {config.command_topic('link_yield')} -m 300\n"
    )
    print("Ctrl-C or SIGTERM to stop.\n")

    task = asyncio.create_task(bridge.run())

    async def _drain_bridge() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async with _stop_signals() as stopping:
        stop_wait = asyncio.create_task(stopping.wait())
        try:
            done, _ = await asyncio.wait({task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                # The bridge ended on its own. Surface why, as it did before signals
                # were handled here — a broker that went away is not a clean stop.
                await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop_wait.cancel()
            await _release(_drain_bridge, session.stop)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bedjet",
        description="Local BedJet control. Commands marked ⚠️ write to a mains-powered "
        "heater; each asks for confirmation and verifies the result. See docs/SAFETY.md.",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging, incl. raw packets")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="scan for the BedJet (bring-up steps 1-3)")
    p_discover.add_argument("--timeout", type=float, default=10.0)
    p_discover.add_argument(
        "--all",
        action="store_true",
        help="list every nearby BLE device, not just BedJet-looking ones",
    )
    p_discover.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="scan N times and report presence per device — a single scan is a sample, "
        "not a census (RL-008)",
    )
    p_discover.add_argument(
        "--interval", type=float, default=5.0, metavar="S", help="seconds between repeats"
    )
    p_discover.set_defaults(func=cmd_discover)

    force_help = "connect without checking the device registry (see RL-006)"

    p_identify = sub.add_parser("identify", help="connect and enumerate GATT (steps 4-6)")
    p_identify.add_argument("address", help="address from `bedjet discover`")
    p_identify.add_argument("--timeout", type=float, default=20.0)
    p_identify.add_argument("--force", action="store_true", help=force_help)
    p_identify.set_defaults(func=cmd_identify)

    p_watch = sub.add_parser("watch", help="subscribe and decode status (steps 7-10)")
    p_watch.add_argument("address")
    p_watch.add_argument("--timeout", type=float, default=20.0)
    p_watch.add_argument("--force", action="store_true", help=force_help)
    p_watch.add_argument("--raw", action="store_true", help="print raw packet hex")
    p_watch.add_argument(
        "--seconds",
        type=float,
        default=120.0,
        metavar="S",
        help="release the BLE link after S seconds (default 120). Use 0 to hold it "
        "indefinitely — but the vendor app cannot connect while we do.",
    )
    p_watch.add_argument(
        "--packets",
        type=int,
        default=0,
        metavar="N",
        help="stop after N distinct packets (repeats do not count). 0 = no limit.",
    )
    p_watch.add_argument("--save", type=Path, help="write last packet to file")
    p_watch.set_defaults(func=cmd_watch)

    p_off = sub.add_parser(
        "off",
        help="⚠️ WRITES TO THE DEVICE. Send OFF and verify it took effect (step 11)",
    )
    p_off.add_argument("address")
    p_off.add_argument("--timeout", type=float, default=20.0)
    p_off.add_argument("--force", action="store_true", help=force_help)
    p_off.add_argument(
        "--dry-run",
        action="store_true",
        help="rehearse everything except the write — confirms the preconditions and the "
        "read-back plumbing without sending a byte",
    )
    p_off.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_off.add_argument(
        "--settle",
        type=float,
        default=10.0,
        metavar="S",
        help="how long to wait for the device to report the new state (default 10)",
    )
    p_off.add_argument("--save", type=Path, help="write the post-command packet to file")
    p_off.set_defaults(func=cmd_off)

    p_fan = sub.add_parser(
        "fan", help="⚠️ WRITES TO THE DEVICE. Set fan speed and verify it (step 13)"
    )
    p_fan.add_argument("address")
    p_fan.add_argument("percent", type=int, help="5-100, in 5%% steps")
    p_fan.add_argument("--timeout", type=float, default=20.0)
    p_fan.add_argument("--force", action="store_true", help=force_help)
    p_fan.add_argument("--dry-run", action="store_true", help="rehearse without writing")
    p_fan.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_fan.add_argument("--settle", type=float, default=10.0, metavar="S")
    p_fan.set_defaults(func=cmd_fan)

    p_mode = sub.add_parser(
        "mode", help="⚠️ WRITES TO THE DEVICE. Select a thermally safe mode (step 14)"
    )
    p_mode.add_argument("address")
    p_mode.add_argument(
        "mode",
        choices=sorted(m.name.lower() for m in UNLOCKED_MODES),
        help="turbo and extended heat are deliberately unavailable — see docs/SAFETY.md",
    )
    p_mode.add_argument("--timeout", type=float, default=20.0)
    p_mode.add_argument("--force", action="store_true", help=force_help)
    p_mode.add_argument("--dry-run", action="store_true", help="rehearse without writing")
    p_mode.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_mode.add_argument("--settle", type=float, default=10.0, metavar="S")
    p_mode.set_defaults(func=cmd_mode)

    p_temp = sub.add_parser(
        "temp",
        help="⚠️ WRITES TO THE DEVICE. Set the target temperature and verify it (step 14)",
    )
    p_temp.add_argument("address")
    p_temp.add_argument("value", type=float, help="target, in F unless --celsius is given")
    p_temp.add_argument("--celsius", action="store_true", help="interpret value as C")
    p_temp.add_argument("--timeout", type=float, default=20.0)
    p_temp.add_argument("--force", action="store_true", help=force_help)
    p_temp.add_argument("--dry-run", action="store_true", help="rehearse without writing")
    p_temp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_temp.add_argument("--settle", type=float, default=10.0, metavar="S")
    p_temp.set_defaults(func=cmd_temp)

    p_serve = sub.add_parser(
        "serve",
        help="⚠️ HOLDS THE BLE LINK. Run the local HTTP+WS API (Milestone 3)",
        description="Run the local API. This holds the BedJet's only BLE slot for as long "
        "as it runs, so the vendor app cannot connect meanwhile — use POST "
        "/api/v1/link/yield to hand the device back temporarily.",
    )
    p_serve.add_argument("address")
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default 127.0.0.1). Binding anywhere else REQUIRES a "
        "token: this API can switch on a heater.",
    )
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument(
        "--token",
        help="shared secret for `Authorization: Bearer <token>`. Prefer the BEDJET_TOKEN "
        "environment variable — an argument is visible in the process list.",
    )
    p_serve.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="an additional Host header to accept, e.g. a Pi's hostname. Repeatable. "
        "Loopback names are always accepted.",
    )
    p_serve.add_argument("--timeout", type=float, default=20.0)
    p_serve.add_argument("--force", action="store_true", help=force_help)
    p_serve.add_argument("--settle", type=float, default=10.0, metavar="S")
    p_serve.set_defaults(func=cmd_serve)

    p_mqtt = sub.add_parser(
        "mqtt",
        help="⚠️ HOLDS THE BLE LINK. Bridge the device to an MQTT broker (Milestone 3)",
        description="Publish state and accept commands over MQTT, with Home Assistant "
        "discovery. A peer of `serve` — run either, or both.",
    )
    p_mqtt.add_argument("address")
    p_mqtt.add_argument("--broker", default="127.0.0.1", help="broker hostname")
    p_mqtt.add_argument("--broker-port", type=int, default=1883)
    p_mqtt.add_argument(
        "--username",
        help="broker username. The password comes from BEDJET_MQTT_PASSWORD only — a "
        "password in argv is visible to every process on the machine.",
    )
    p_mqtt.add_argument("--base-topic", default="bedjet")
    p_mqtt.add_argument("--device-id", default="bedjet", help="topic slug and HA unique id")
    p_mqtt.add_argument(
        "--no-discovery", action="store_true", help="do not publish Home Assistant discovery"
    )
    p_mqtt.add_argument("--timeout", type=float, default=20.0)
    p_mqtt.add_argument("--force", action="store_true", help=force_help)
    p_mqtt.add_argument("--settle", type=float, default=10.0, metavar="S")
    p_mqtt.set_defaults(func=cmd_mqtt)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.debug)
    try:
        return int(asyncio.run(args.func(args)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
