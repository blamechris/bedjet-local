"""Bring-up CLI: discover → identify → watch.

Mirrors the safe bring-up order in ``docs/SAFETY.md``. ``discover``, ``identify`` and
``watch`` are read-only. ``off`` is the **one** subcommand that writes — bring-up step 11,
gated behind a typed confirmation, and it verifies the result rather than assuming it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .device import registry
from .device.state import BedJetState
from .protocol import encode
from .protocol.constants import CHARACTERISTICS, NAME_UUID, SERVICE_UUID
from .protocol.packets import StatusPacket
from .service.commander import Commander, CommandRefused, CommandUnverified
from .service.reader import StatusReader
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bedjet",
        description="Local BedJet bring-up. Read-only: this CLI cannot send commands.",
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
