"""Hardware-in-the-loop bring-up tests. ⚠️ THESE TALK TO A REAL HEATER.

Deselected by default (``addopts = -m 'not hardware'`` in pyproject.toml). Run only via
``make test-hardware``, only with the device powered, in range, and a human watching it.

**Every test in this file is read-only.** Nothing here writes to the device. When
Milestone 2 adds control tests they go in a separate module with their own gate, and they
follow the bring-up order in docs/SAFETY.md — off, then fan, then heat, last and attended.

Set BEDJET_ADDRESS to the address printed by ``bedjet discover``. It is host-specific: a
MAC on Linux, a CoreBluetooth UUID on macOS.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from bedjet_local.device.state import BedJetState
from bedjet_local.protocol.constants import (
    COMMAND_UUID,
    NAME_UUID,
    SERVICE_UUID,
    STATUS_UUID,
)
from bedjet_local.protocol.packets import StatusPacket
from bedjet_local.service.reader import StatusReader
from bedjet_local.transport.ble import BleakTransport, scan

pytestmark = pytest.mark.hardware

ADDRESS = os.environ.get("BEDJET_ADDRESS")
needs_address = pytest.mark.skipif(not ADDRESS, reason="set BEDJET_ADDRESS (see `bedjet discover`)")


async def test_step_1_device_is_discoverable() -> None:
    """Bring-up step 1. Records the answer to PROTOCOL.md's empty 'device identity' table."""
    devices = await scan(timeout=15.0)
    assert devices, "no BedJet found — powered? in range? vendor app holding the link?"
    for device in devices:
        print(f"\nDISCOVERED: {device.describe()}")
        print(f"  services: {device.service_uuids}")
        print(f"  mfr data: {device.manufacturer_data}")
    print("\n→ Record these in docs/research/RESEARCH-LOG.md and PROTOCOL.md.")


@needs_address
async def test_step_5_gatt_layout_matches_documented_uuids() -> None:
    """Bring-up steps 4-6. The first real test of our UPSTREAM protocol claims."""
    assert ADDRESS
    transport = BleakTransport()
    await transport.connect(ADDRESS)
    try:
        layout = await transport.services()
        uuids = {u.lower() for u in layout}
        chars = {c.lower() for chars in layout.values() for c, _ in chars}

        print(f"\nSERVICES: {sorted(uuids)}")
        print(f"CHARACTERISTICS: {sorted(chars)}")

        assert SERVICE_UUID.lower() in uuids, (
            "our device does not expose the documented BedJet service — this invalidates "
            "the UPSTREAM assumptions in PROTOCOL.md, which is a finding, not a failure"
        )
        for uuid, label in (
            (STATUS_UUID, "status"),
            (NAME_UUID, "name"),
            (COMMAND_UUID, "command"),
        ):
            assert uuid.lower() in chars, f"missing {label} characteristic {uuid}"
    finally:
        await transport.disconnect()


@needs_address
async def test_step_7_status_notifications_decode() -> None:
    """Bring-up steps 7-9. Turn the unit ON with the physical remote before running.

    This is the test that promotes PROTOCOL.md rows from 📖 UPSTREAM to ✅ VERIFIED — but
    only after a human has compared the printed values against the unit's own display.
    A green test here means "we decoded something self-consistent", not "we decoded it
    correctly". Step 10 is the human's job and cannot be automated away.
    """
    assert ADDRESS
    transport = BleakTransport()
    await transport.connect(ADDRESS)
    seen: list[tuple[BedJetState, StatusPacket]] = []

    try:
        reader = StatusReader(transport, lambda state, packet: seen.append((state, packet)))
        await reader.start()
        for _ in range(30):
            if seen:
                break
            await asyncio.sleep(1.0)
        await reader.stop()
    finally:
        await transport.disconnect()

    assert seen, (
        "no status notifications in 30s. The BedJet is mostly silent while OFF — turn it "
        "on with the physical remote and retry before treating this as a fault."
    )
    for state, packet in seen[:5]:
        print(f"\n{state.describe()}")
        print(f"  raw: {packet.raw.hex(' ')}")
        print(f"  anomalies: {packet.anomalies}")
    print("\n→ STEP 10: compare the above against the unit's display before believing it.")
