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

### `cool_fan50_target75f.bin`

| | |
|---|---|
| **Captured** | 2026-08-16 23:38 UTC |
| **Firmware** | ⚠️ **unknown** — not in the advertisement, not in characteristic `2001` (RL-007). Unresolved. |
| **Device state** | **Cool**, fan **50 %**, target **75 °F** — set in the BedJet 3 Smart Remote app (Android), app then force-closed to release the link |
| **Capture method** | `bedjet watch <ours> --raw --seconds 60 --save …`; arrived as a notification plus a follow-up read and reassembled |
| **Host** | macOS, Darwin 25.5, CoreBluetooth via bleak |
| **Bytes** | `01 56 1b 01 09 3b 19 2d 30 04 09 0c 00 26 34 00 00 29 00 12 01 9a 01 10 ff 00 15 34 00 00 31` (31 bytes) |

**Expected decode** (asserted in `tests/unit/test_real_fixtures.py`):

| Field | Value | Evidence |
|---|---|---|
| header | `01 56 1b 01` — partial flag, format `0x56`, payload length 27, type `0x01` | 27 + 4 = 31 = actual size |
| time remaining | 9:59:25 | bytes 4–6 |
| actual temp | 22.5 °C / 72.5 °F | byte 7 = `0x2d` |
| **target temp** | 24.0 °C / **75.2 °F** | byte 8 = `0x30` — **matches the 75 °F set in the app** |
| **mode** | **Cool** | byte 9 = `0x04` — **matches Cool set in the app** |
| **fan** | step 9 → **50 %** | byte 10 = `0x09` — **matches 50 % set in the app** |
| ambient | 20.5 °C | byte 17 = `0x29` |
| unknown | `12 01 9a 01 10 ff 00` | bytes 19–25 |
| byte 30 | `0x31` | one byte beyond upstream's documented layout |

**Why this fixture is load-bearing:** three fields were set to exact values in the app
*before* the capture and read back correctly afterwards. That is independent ground truth,
not self-consistency — and it is what licenses the ✅ VERIFIED rows for the temperature
encoding, the fan-step mapping, and the header layout in `PROTOCOL.md`.

It also **confirms ownership** (RL-006, previously only probable): this unit reports the
state we set in our own app.

*Note: the "mode" recorded here is the status enum value. It is **not** the command enum —
see RL-012.*

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
