# BedJet V3 BLE protocol — field reference

**Every row on this page carries a provenance tag. There are exactly three, and they are not
interchangeable:**

| Tag | Meaning |
|---|---|
| ✅ `VERIFIED` | **Observed on our device.** Requires a fixture file, a capture date, and the firmware version it was captured against. Nothing else earns this tag. |
| 📖 `UPSTREAM` | Read from someone else's source or documentation. Plausible, corroborated, **not observed here**. |
| ❓ `HYPOTHESIS` | Our inference, or an unresolved contradiction between upstream sources. |

**The rule that matters:** an `UPSTREAM` row is never promoted to `VERIFIED` because it looked
right, because two upstreams agreed, or because the code ran without crashing. It is promoted
by a fixture captured from our BedJet and a test that asserts against it. Silently converting
upstream behaviour into a claim about our hardware is the specific failure this document
exists to prevent.

**Current state: Milestone 1 complete — discover → connect → read state, all verified against
our own hardware.** The header layout, temperature encoding, fan scaling, and mode location are
✅ VERIFIED against a fixture captured with the device in a known state. Bytes 19–25, byte 30,
most of the status-mode table, and the firmware version remain open.

---

## Device identity

Observed 2026-08-16 via `bedjet discover` on macOS (Darwin 25.5), scan from a single position.
See [RL-005](../research/RESEARCH-LOG.md).

| Property | Value | Provenance | Confidence |
|---|---|---|---|
| Our model | **BedJet V3** — advertises the BedJet service UUID | ✅ VERIFIED 2026-08-16 | high |
| Advertised name | **`BEDJET_V3`** (both units) | ✅ VERIFIED 2026-08-16 | high |
| Advertised service UUIDs | `00001000-bed0-0080-aa55-4265644a6574` only | ✅ VERIFIED 2026-08-16 | high |
| Manufacturer data | **none present** in the advertisement | ✅ VERIFIED 2026-08-16 | high |
| Address form | CoreBluetooth UUID on macOS, as predicted; **not** a MAC | ✅ VERIFIED 2026-08-16 | high |
| **Units visible** | **two**, both `BEDJET_V3`; ours ranges −95 … −73 dBm (22 dB spread, RL-010) | ✅ VERIFIED 2026-08-16 | high |
| **Ours** | confirmed by reading back a state we set in our own app (RL-012) | ✅ VERIFIED 2026-08-16 | high |
| **Not ours** | the second unit — a neighbour's. **Never connect to it.** | ✅ VERIFIED 2026-08-16 | high |
| We own | **one** BedJet | ✅ VERIFIED 2026-08-16 | high |
| Our firmware/hw rev | "v1.2.0" — **still unresolved**: not in the advertisement, not in `2001`, not identified in the status packet | ❓ | low |

**The advertisement carries no distinguishing information.** Same name, same service UUID, no
manufacturer data, and a host-local address that means nothing on another machine. RSSI is the
*only* thing separating the two units, and RSSI is not identity — it moves with position,
posture, and what is between the radio and the device. An RSSI-sorted scan would have picked
ours here **by luck, not by logic**.

Identification required a physical act only the owner could perform: cutting power and watching
which address left the scan ([RL-006](../research/RESEARCH-LOG.md)). Our unit's address is
recorded in a **gitignored** local registry, and the CLI refuses to connect to anything absent
from it. Expect to need an equivalent physical discriminator for the next device — this class
of hardware does not identify itself.

> ✅ **Ownership CONFIRMED 2026-08-16 (RL-012).** The unit reported back the exact state we had
> set in our own vendor app — independent of any scan behaviour. This supersedes the earlier
> power-test inference, which RL-008 had correctly downgraded after showing that a scan is not a
> census and that our unit's signal swings 22 dB while stationary.

## GATT layout

**Enumerated on our device 2026-08-16** ([RL-007](../research/RESEARCH-LOG.md)). Properties
below are what the device actually reports, not what upstream describes. The service exposes
**seven** characteristics — three more than any upstream source documents.

| UUID | Role | Properties (observed) | Provenance | Confidence |
|---|---|---|---|---|
| `00001000-…` | Service | — | ✅ VERIFIED 2026-08-16 | high |
| `00002000-…` | Status | **`write`, read, notify** | ✅ VERIFIED 2026-08-16 (present + properties) | high |
| `00002001-…` | "Device name" — **but see below** | `write`, read | ✅ VERIFIED present; ❓ role wrong | high / low |
| `00002002-…` | **undocumented** | `write`, read | ✅ VERIFIED present; ❓ purpose unknown | — |
| `00002003-…` | **undocumented** | `write` only | ✅ VERIFIED present; ❓ purpose unknown | — |
| `00002004-…` | Command | `write` only | ✅ VERIFIED present + write-only | high |
| `00002005-…` | Biorhythm sequence fragments | `write`, read | ✅ VERIFIED present; 📖 role from `bedjet-re` | medium |
| `00002006-…` | **undocumented** | `write`, read | ✅ VERIFIED present; ❓ purpose unknown | — |

All share the prefix `…-bed0-0080-aa55-4265644a6574`; the trailing `4265644a6574` is ASCII
`BedJet`.

Two deltas against upstream worth noting:

- **`2000` is writable.** Upstream describes the status characteristic as notify + read. Ours
  accepts writes. Purpose unknown, and **not to be probed** — an unexplained write to the
  characteristic that carries device state is exactly the experiment `SAFETY.md` forbids
  without a hypothesis and a reversal.
- **`2004` is write-only**, with no read property. Consistent with a command sink.

### `2001` is not a device name on our firmware

Reading it returned **4 bytes: `43 4a 65 3a`** (`CJe:` as ASCII). That is not a name, and it is
not a fragment of `BedJet` (`42 65 64 4a 65 74`). ✅ VERIFIED observation; ❓ interpretation
entirely open. Candidate explanations, none tested:

1. The characteristic is a **request/response channel**, not a static value — it is `write`
   *and* `read`, so a read may be returning the residue of whatever was last requested.
2. A user-settable name that has never been set, so we are seeing uninitialised memory.
3. Upstream's "device name" label is simply wrong for this firmware revision.

**Firmware version is still not found.** Not in the advertisement, not in `2001`, and not
identified in the status packet. The best remaining candidate is bytes 19–25, which are
**invariant across every capture and every mode** (`12 01 9a 01 10 ff 00`) — that is what
configuration or identity looks like, not state. Untestable without a second device or a
firmware update.

## Status packet

✅ **Layout verified 2026-08-16** against `tests/fixtures/cool_fan50_target75f.bin`, captured
with the vendor app set to a known state (Cool, fan 50 %, target 75 °F) written down *before*
the capture. Three fields matched that ground truth exactly.

**Observed packet length: 31 bytes.** Upstream's documented layout ends at byte 29.

### Header — bytes 0–3 ✅ VERIFIED

| Offset | Field | Observed | Confidence |
|---|---|---|---|
| 0 | Partial flag | `0x01` | medium — see note |
| 1 | Packet format | `0x56` = V3 home | high |
| 2 | **Payload length** | `0x1b` = 27 | **high** — 27 + 4 = 31 = actual size |
| 3 | Packet type | `0x01` = status | high |

Our original guess put length first and format third. It was wrong, and one capture corrected
it — which is exactly why `StatusPacket.header` retains the raw bytes.

> **The length byte, not the flag, decides completeness.** The partial flag is set on the first
> fragment and therefore remains set after reassembly, so keying off it loops. `is_complete` is
> `len(raw) >= declared_length + 4`. A length the device asserts about itself is better evidence
> than a flag we only half understand.

### Body

| Offset | Field | Encoding | Observed | Provenance |
|---|---|---|---|---|
| 4–6 | Time remaining h/m/s | uint8 ×3 | 9:59:25 | ✅ VERIFIED |
| 7 | Actual temperature | `2 × °C` | `0x2d` → 22.5 °C | ✅ VERIFIED |
| 8 | **Target temperature** | `2 × °C` | `0x30` → 24.0 °C = **75.2 °F** | ✅ VERIFIED — app was set to 75 °F |
| 9 | **Mode** (status enum) | see below | `0x04` = **Cool** | ✅ VERIFIED — app was set to Cool |
| 10 | **Fan** | step index | `0x09` → **50 %** | ✅ VERIFIED — app was set to 50 % |
| 11–12 | **Max runtime for this mode** h/m | uint8 ×2 | 0:00 / 12:00 / 0:10 | ✅ VERIFIED across 3 modes |
| 13–14 | **Permitted target range for this mode** | `2 × °C` | see below | ✅ VERIFIED across 3 modes |
| 15–16 | **Seconds elapsed in turbo** | uint16 **big-endian** | 13 → 14 as remaining fell | ✅ VERIFIED |
| 17 | Ambient temperature | `2 × °C` | 20.5–22.0 °C, tracks the room | ✅ plausible across captures |
| 18 | Shutdown reason | uint8 | `0x00` in all captures | ❓ |
| 19–25 | **invariant across all modes** | — | `12 01 9a 01 10 ff 00` | ❓ not state — config or identity |
| 26 | Update phase | uint8 | `0x15` in all captures | ❓ |
| 27 | Flags bitfield | bitfield | `0x34` in all captures | ❓ bit positions unknown |
| 28 | Sequence step | uint8 | `0x00` | ❓ |
| 29 | Notify code | uint8 | `0x00` | ❓ |
| **30** | **CHECKSUM** | packet sums to 0 mod 256 | ✅ holds for 5/5 packets | ✅ **VERIFIED — undocumented upstream** |

### ✅ Byte 30 is a checksum — documented by no upstream source

`byte30 = (-sum(bytes 0..29)) & 0xFF`, i.e. the whole packet sums to **zero mod 256**. Verified
across five packets and three modes. ESPHome, every Home Assistant integration, and `bedjet-re`
all end their layout at byte 29 and none mentions a checksum.

Beyond integrity checking, this **independently proves reassembly is correct**: a
notification mis-joined to its follow-up read would not sum to zero.

### ✅ Per-mode limits — bytes 11–14 are not device limits

| Mode | Max runtime | Permitted target range |
|---|---|---|
| standby `0x00` | 0:00 | 10.0–40.0 °C (50–104 °F) |
| cool `0x04` | 12:00 | 19.0–26.0 °C (66.2–78.8 °F) |
| turbo `0x02` | **0:10** | fixed **43.0 °C (109.4 °F)** |

⚠️ **Turbo targets 109.4 °F — above the 104 °F that upstream and the manufacturer's marketing
both call the maximum.** A hardcoded ceiling therefore flags a perfectly healthy turbo packet
as anomalous, which ours did. **The device publishes its own limits; ask it rather than assume.**
Any clamp applied in Milestone 2 must come from bytes 13–14.

Bytes 15–16 are **big-endian**, unusual for BLE: two turbo packets read 13 then 14 while
remaining fell 9:47 → 9:46 against a 10:00 limit, and `elapsed + remaining == max runtime`
exactly. Little-endian would give 3328 and 3584. Big-endian-u16 versus a plain u8 at byte 16
cannot be separated until a capture more than 255 s into turbo.

### ⚠️ Status modes are NOT command modes

The single most consequential finding so far (RL-012). With the app set to **Cool**, byte 9
read `0x04` — which every upstream *command* table calls **turbo**. Decoding status with the
command enum produced `mode=turbo` for a unit that was cooling, silently and plausibly.

Upstream command tables describe what you **send**. Nothing we found states that the status
byte uses a different enum. It does.

| Status value | Meaning | Command table calls it | Provenance |
|---|---|---|---|
| `0x00` | **Standby / off** | `0x01` is off | ✅ VERIFIED |
| `0x02` | **Turbo** | *cool* | ✅ VERIFIED |
| `0x04` | **Cool** | *turbo* | ✅ VERIFIED |
| `0x03`, `0x05`, `0x06`… | heat / dry / ext. heat — **unknown** | — | ❓ capture one state each |

**The two enums have cool and turbo swapped.** That is worse than an unrelated mapping: a
status packet decoded with the command table does not produce nonsense, it produces *the other
real mode*. Cooling reads as turbo, turbo reads as cool, and nothing looks broken.

`StatusMode` therefore contains only `COOL`. Any other value decodes to `None` with an anomaly
rather than to a guess, and `Power` stays `UNKNOWN` unless the mode is verified. A
plausible-looking wrong mode is worse than an admitted unknown on a device that makes heat.

### Notification behaviour

Rapid notifications while the unit is **on**; near-silent while **off** (📖 UPSTREAM). Status
arrives split: a notification carrying part of the packet, completed by an explicit read of the
same characteristic. ✅ VERIFIED — 55 notifications in ~60 s, each reassembling to the declared
31 bytes.

## Commands — characteristic `…2004`, write

| Bytes | Command | Provenance | Confidence |
|---|---|---|---|
| `01 <mode>` | Set mode / button | 📖 MQTT bridge | medium |
| `02 <hh> <mm>` | Set timer | 📖 MQTT bridge | medium |
| `03 <temp>` | Set target temperature (`2 × °C`) | 📖 MQTT bridge | medium |
| `07 <step>` | Set fan speed (step index) | 📖 MQTT bridge | medium |
| — | Set clock (`CMD_SET_CLOCK`, `<hh> <mm>`) | 📖 ESPHome — opcode not yet established | low |

### Mode / button values (operand of `0x01`)

| Value | Meaning | Provenance |
|---|---|---|
| `0x01` | Off | 📖 MQTT bridge |
| `0x02` | Cool | 📖 |
| `0x03` | Heat | 📖 |
| `0x04` | Turbo | 📖 |
| `0x05` | Dry | 📖 |
| `0x06` | Extended heat | 📖 |
| `0x10` / `0x11` | Fan up / fan down | 📖 |
| `0x12` / `0x13` | Temp up / temp down | 📖 |
| `0x20` / `0x21` / `0x22` | Preset M1 / M2 / M3 | 📖 |

⚠️ Every command in this section is **unwritten and untested**. Per
[`SAFETY.md`](../SAFETY.md), the first command we ever send is `01 01` (off), and no command
is sent at all until state decoding has been validated against the physical unit.

## Temperature encoding — the resolved contradiction

Upstream sources appear to disagree. They do not.

- ESPHome: `°C = byte / 2`.
- MQTT bridge: `°F = ((b − 0x26) + 66) − ((b − 0x26) / 9)` (integer division).

Evaluate both:

| byte | ESPHome °C | bridge °F | °C→°F |
|---|---|---|---|
| 40 | 20 | (2+66) − 0 = 68 | 68 ✓ |
| 50 | 25 | (12+66) − 1 = 77 | 77 ✓ |
| 60 | 30 | (22+66) − 2 = 86 | 86 ✓ |

The bridge's formula is an integer approximation of `°F = (b/2) × 1.8 + 32`. **The wire format
is `2 × °C`**; the °F polynomial is a derived display conversion, not a competing claim.
Confidence: high. Provenance stays 📖 — two upstreams agreeing is corroboration, not
observation.

Documented operating range is 66–104 °F (≈19–40 °C), i.e. bytes ≈38–80. Bytes outside that
range should decode but be flagged, not clamped silently.

## Fan-speed encoding — ✅ SETTLED

- ESPHome: `percent = 5 + 5 × step` → step 0 = 5 %, step 19 = 100 %.
- MQTT bridge: `percent = step × 5` → step 20 = 100 %.

✅ **ESPHome is correct.** With the vendor app set to **50 %**, byte 10 read `0x09` (step 9):
ESPHome's formula gives 50, the bridge's gives 45. Verified against ground truth, fixture
`cool_fan50_target75f.bin`, [RL-002](../research/RESEARCH-LOG.md).

## Unknowns requiring physical experiments

Carried forward as the experiment backlog. Each becomes a research-log entry.

1. Actual advertised name, service UUIDs, and address form of *our* unit.
2. Real firmware version, and where it is readable (device-name characteristic? a status byte?).
3. Whether the partial-packet + follow-up-read behaviour occurs on our firmware.
4. Byte 10 scaling (fan step base).
5. Mode byte location — `[9]` alone, or `[9]` + `[13..14]` disambiguation.
6. Bytes 19–25: entirely unknown.
7. Byte 27 bitfield positions (°C/°F units flag is the useful one).
8. Whether `0x03 <temp>` accepts values outside the remote's own limits — **not to be tested
   until much later, and only downward.** See `SAFETY.md`.
9. Behaviour when our client connects while the vendor app holds the link, and vice versa.
10. Whether the device advertises while a client is connected (affects rediscovery/reconnect).
