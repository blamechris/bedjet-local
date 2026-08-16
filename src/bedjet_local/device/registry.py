"""Which BedJets are ours.

RL-005/RL-006 established that this device class advertises **no identity**: every unit is
named ``BEDJET_V3``, advertises the same service UUID, carries no manufacturer data, and
reports a host-local address. Two units were in range during bring-up and only one was
ours; an RSSI-sorted scan would have picked the right one by luck.

So ownership is recorded here, deliberately, once — by a human who has performed a physical
test — and everything that opens a connection checks it first. Connecting takes the device's
single BLE slot, so connecting to a stranger's BedJet would knock them off their own heater.
That is not a preference to encode as sort order.

The registry file is **gitignored**. Addresses are host-local (a CoreBluetooth UUID on macOS,
a MAC on Linux), so the file is per-machine anyway, and a device address is nobody else's
business.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILENAME = "devices.local.toml"


@dataclass(frozen=True, slots=True)
class KnownDevice:
    address: str
    label: str
    notes: str = ""


class UnknownDeviceError(Exception):
    """Raised when something tries to connect to a device that is not ours."""


def registry_path() -> Path:
    """Where the registry lives. ``BEDJET_DEVICES`` overrides, for tests and for a Pi."""
    override = os.environ.get("BEDJET_DEVICES")
    if override:
        return Path(override)
    return Path.cwd() / DEFAULT_FILENAME


def load(path: Path | None = None) -> dict[str, KnownDevice]:
    """Load the registry, keyed by lowercased address. Missing file -> empty registry."""
    target = path or registry_path()
    if not target.exists():
        return {}

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    devices: dict[str, KnownDevice] = {}
    for label, entry in (raw.get("device") or {}).items():
        address = str(entry["address"])
        devices[address.lower()] = KnownDevice(
            address=address,
            label=label,
            notes=str(entry.get("notes", "")),
        )
    return devices


def lookup(address: str, path: Path | None = None) -> KnownDevice | None:
    return load(path).get(address.lower())


def require_known(address: str, path: Path | None = None) -> KnownDevice:
    """Assert that ``address`` is one of ours, or refuse.

    An empty registry refuses everything. That is the intended behaviour on a fresh
    checkout: the first thing a new host must do is decide, once, which BedJet is its own.
    """
    devices = load(path)
    known = devices.get(address.lower())
    if known is not None:
        return known

    target = path or registry_path()
    if not devices:
        raise UnknownDeviceError(
            f"No device registry at {target}.\n"
            f"Before connecting, establish which BedJet is yours — unplug it, re-run "
            f"`bedjet discover`, and note which address disappears (see RL-006). Then:\n\n"
            f"  [device.bedroom]\n"
            f'  address = "{address}"\n'
            f'  notes = "identified by power test <date>"\n\n'
            f"This tool will not connect to an unregistered device. Any BedJet in radio "
            f"range looks identical to yours, and connecting takes its single BLE slot."
        )
    raise UnknownDeviceError(
        f"{address} is not in {target}, which lists: "
        f"{', '.join(f'{d.label} ({d.address})' for d in devices.values())}.\n"
        f"If this device really is yours, add it. If you are not certain it is yours, it is "
        f"not — connecting would take a stranger's BedJet away from them."
    )
