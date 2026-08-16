"""Bring-up CLI: discover → identify → watch.

Mirrors the safe bring-up order in ``docs/SAFETY.md`` steps 1-10. **Every command here is
read-only.** There is no way to send a command to the device from this CLI, and that is
deliberate for Milestone 1.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .device import registry
from .device.state import BedJetState
from .protocol.constants import COMMAND_UUID, NAME_UUID, SERVICE_UUID, STATUS_UUID
from .protocol.packets import StatusPacket
from .service.reader import StatusReader
from .transport.ble import BleakTransport, scan

log = logging.getLogger("bedjet")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def cmd_discover(args: argparse.Namespace) -> int:
    """Bring-up steps 1-3: discover, identify, inspect advertising data."""
    print(f"Scanning for {args.timeout:.0f}s...")
    devices = await scan(timeout=args.timeout, all_devices=args.all)
    if not devices:
        print("\nNo BedJet found.")
        print("  · Is the unit powered?")
        print("  · Is the vendor app connected? Only one BLE client at a time is allowed.")
        print("  · Are you within ~10m, ideally same room?")
        print("  · Try --all to list every nearby device, in case ours advertises")
        print("    under a name we do not recognise.")
        return 1

    known = registry.load()
    print(f"\nFound {len(devices)} device(s):\n")
    unregistered = 0
    for device in devices:
        print(f"  {device.describe()}")
        if device.service_uuids:
            print(f"      services: {', '.join(device.service_uuids)}")
        for company_id, payload in device.manufacturer_data:
            print(f"      mfr 0x{company_id:04x}: {payload.hex(' ')}")
        if SERVICE_UUID.lower() in device.service_uuids:
            print("      ✅ advertises the BedJet service UUID")
        entry = known.get(device.address.lower())
        if entry is not None:
            print(f"      ✅ ours: {entry.label}")
        else:
            unregistered += 1
            print("      ⛔ not in the device registry — will not be connected to")
        print()

    if unregistered:
        # RL-006: a neighbour's BedJet is indistinguishable from ours by advertisement.
        print(
            f"{unregistered} device(s) are not registered as ours. A BedJet in radio range "
            "looks\nidentical to yours — same name, same service UUID, no manufacturer data. "
            "Identify\nyours with the power test (RL-006) before adding it to "
            f"{registry.registry_path()}.\n"
        )
    print("Record these observations in docs/research/RESEARCH-LOG.md before you forget them.")
    return 0


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
        known = {
            SERVICE_UUID.lower(): "BedJet service",
            STATUS_UUID.lower(): "status (notify+read)",
            NAME_UUID.lower(): "device name",
            COMMAND_UUID.lower(): "command (write)",
        }
        for service_uuid, characteristics in layout.items():
            label = known.get(service_uuid.lower(), "")
            print(f"service {service_uuid}  {label}")
            for char_uuid, properties in characteristics:
                char_label = known.get(char_uuid.lower(), "")
                print(f"    char {char_uuid}  [{', '.join(properties)}]  {char_label}")
            print()

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
    """Bring-up steps 7-10: subscribe, observe, decode, and compare to the physical unit."""
    _check_ownership(args)
    transport = BleakTransport()
    await transport.connect(args.address, timeout=args.timeout)

    captures: list[bytes] = []

    def on_state(state: BedJetState, packet: StatusPacket) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        print(f"[{stamp}] {state.describe()}")
        if args.raw:
            print(f"          raw: {packet.raw.hex(' ')}")
            print(f"          hdr: {packet.header.hex(' ')}")
            print(f"          unknown[19:26]: {packet.unknown_region.hex(' ')}")
        for anomaly in packet.anomalies:
            print(f"          ⚠️  {anomaly}")
        captures.append(packet.raw)

    reader = StatusReader(transport, on_state)
    await reader.start()

    print("\nWatching. The BedJet notifies rapidly while ON and is mostly silent while OFF —")
    print("silence here is not necessarily a fault. Ctrl-C to stop.\n")
    try:
        while True:
            await asyncio.sleep(1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await reader.stop()
        await transport.disconnect()

    print(f"\n{reader.packets_seen} packets seen ({reader.partials_seen} flagged partial).")
    if args.save and captures:
        args.save.write_bytes(captures[-1])
        print(f"Saved last packet to {args.save}")
        print("Record its provenance in tests/fixtures/PROVENANCE.md — date, firmware,")
        print("and the device state it was captured in. An unlabelled fixture is worthless.")
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
    p_watch.add_argument("--save", type=Path, help="write last packet to file")
    p_watch.set_defaults(func=cmd_watch)

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
