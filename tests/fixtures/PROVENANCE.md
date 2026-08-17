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

---

### `off_standby.bin`

| | |
|---|---|
| **Captured** | 2026-08-16 |
| **Device state** | **Off / idle**, set in the vendor app, app force-closed |
| **Bytes** | `01 56 1b 01 00 00 00 30 30 00 09 00 00 14 50 00 00 2c 00 12 01 9a 01 10 ff 00 15 34 00 00 8e` |
| **Expected** | mode `0x00` standby · remaining 0:00:00 · max runtime 0:00 · permitted range 10.0–40.0 °C · fan reads 50 % (**stale**, left over from the previous cool session) |

Names the standby mode value, which is what lets `Power` report OFF instead of UNKNOWN.

### `off_after_heating.bin`

| | |
|---|---|
| **Captured** | 2026-08-16, ~2 min after `off_standby` |
| **Device state** | **Off**, shortly after a heat session — ⚠️ *intended* as a heat capture, but the unit was not running |
| **Bytes** | `01 56 1b 01 00 00 00 40 3f 00 13 00 00 14 50 00 00 2b 00 12 01 9a 01 10 ff 00 15 34 00 00 66` |
| **Expected** | mode `0x00` standby · outlet 32.0 °C (residual heat) · target 31.5 °C / 88.7 °F still set · timer 0:00:00 |

**Filed under what it is, not what it was meant to be.** A fixture labelled by intention rather
than by observed state is exactly the kind of quiet lie the provenance rule exists to stop. It
is still useful: it shows a post-heat cooldown and corroborates standby. **We do not yet have a
heat capture.**

### `turbo_fan100_target109f.bin`

| | |
|---|---|
| **Captured** | 2026-08-16, ~13 s into a turbo run |
| **Device state** | **Turbo**, fan 100 %, target 109 °F — set in the app, app force-closed |
| **Bytes** | `01 56 1b 01 00 09 2f 42 56 02 13 00 0a 56 56 00 0d 2c 00 12 01 9a 01 10 ff 00 15 34 00 00 b3` |
| **Expected** | mode `0x02` turbo · target 43.0 °C / **109.4 °F** · permitted range fixed at 43.0 °C · max runtime **0:10** · remaining 0:09:47 · elapsed 13 s |

Two findings ride on this one. Turbo's target is **above the 104 °F everyone documents as the
maximum**, which is what proved the per-mode bounds are real and our hardcoded range was wrong.
And `elapsed + remaining == max runtime` (13 + 587 = 600) is what identified bytes 15–16.

### `heat_fan100_target89f.bin`

| | |
|---|---|
| **Captured** | 2026-08-16, ~30 s into a heat run |
| **Device state** | **Heat**, fan 100 %, target 89 °F, **timer confirmed counting down before the app was closed** |
| **Bytes** | `01 56 1b 01 00 1d 28 3f 3f 01 13 0c 00 2d 50 00 00 2c 00 12 01 9a 01 10 ff 00 15 34 00 00 fb` |
| **Expected** | mode `0x01` heat · target 31.5 °C / 88.7 °F · actual equal to target (at temperature) · permitted range 22.5–40.0 °C (72.5–104.0 °F) · max runtime 12:00 · remaining 0:29:40 |

The retry of the capture that produced `off_after_heating.bin`. The difference was
confirming the timer was running before releasing the link — a stopped unit reports standby
no matter what mode is selected in the app.

---

## Checksum

Every fixture here satisfies `sum(all 31 bytes) ≡ 0 (mod 256)`, asserted for all of them in
`test_every_real_packet_passes_its_checksum`. No upstream source documents this checksum. It is
also an independent proof that reassembly is correct — a mis-joined packet would not sum to zero.
