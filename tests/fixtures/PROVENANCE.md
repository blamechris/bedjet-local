# Fixture provenance

Every `.bin` in this directory is a packet captured from **our** BedJet. Each one needs a
row here before it is committed. **An unlabelled fixture is worthless** — a byte string
with no recorded device state cannot confirm or refute anything, and it will be believed
anyway by whoever finds it next.

A fixture in this directory is what licenses a ✅ `VERIFIED` row in
[`docs/protocol/PROTOCOL.md`](../../docs/protocol/PROTOCOL.md). Nothing else does.

## Format

| File | Captured | Firmware | Device state (set via **physical remote**) | Capture method | Expected decode |
|---|---|---|---|---|---|

## Fixtures

*None yet. We have not connected to the device.*

The synthetic packets in `tests/unit/test_decode.py` are built from the upstream-documented
layout and prove only that the decoder does what we told it to. They are not evidence about
the hardware and must never be moved into this directory.

## Capturing

```bash
uv run bedjet watch <address> --raw --save tests/fixtures/<name>.bin
```

Then add the row. Record:

- **Device state set with the physical remote, not with software** — the point is an
  independent ground truth. A fixture captured from a state we commanded proves our encoder
  and decoder agree with each other, which is not the same as either being right.
- The firmware version, because the vendor ships updates and bytes may move under us.
- Anything unusual in the room (another BLE client, poor RSSI, the unit mid-cycle).

## Sanitisation

Strip nothing from a status packet — it carries no personal data. Do **not** commit the
device address in a fixture filename or row; addresses are host-specific and pointlessly
identifying. Raw btsnoop/pcap captures stay out of git entirely (see `.gitignore`) — promote
individual packets deliberately.
