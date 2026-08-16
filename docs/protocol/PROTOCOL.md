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

**Current state: every row below is 📖 or ❓. We have not yet connected to the device.**

---

## Device identity

| Property | Value | Provenance | Confidence |
|---|---|---|---|
| Our model | BedJet 3 | ❓ owner-reported, unverified | medium |
| Our firmware/hw rev | "v1.2.0" — may be app version, firmware, or hardware rev | ❓ ambiguous, must read from device | low |
| Advertised name | unknown | ❓ | — |
| Advertised service UUIDs | unknown | ❓ | — |
| Address form | MAC on Linux; **system-assigned UUID on macOS** (CoreBluetooth hides MACs) | 📖 bleak docs | medium |

> Bring-up steps 1–6 exist to fill this table in. Until they do, our own device is the least
> documented thing in this repository.

## GATT layout

| UUID | Role | Properties | Provenance | Confidence |
|---|---|---|---|---|
| `00001000-bed0-0080-aa55-4265644a6574` | Service | — | 📖 ESPHome | high |
| `00002000-bed0-0080-aa55-4265644a6574` | Status | notify + read | 📖 ESPHome + MQTT bridge (independent agreement) | high |
| `00002001-bed0-0080-aa55-4265644a6574` | Device name | read | 📖 both | high |
| `00002004-bed0-0080-aa55-4265644a6574` | Command | write | 📖 both | high |
| `00002005-bed0-0080-aa55-4265644a6574` | Biorhythm sequence fragments | write | 📖 `bedjet-re` only | low |

The trailing `4265644a6574` is ASCII `BedJet`.

## Status packet

Layout below is the **ESPHome-documented** structure, adopted as our primary hypothesis
because it is the more complete of the two upstream descriptions and the two agree wherever
both speak (offsets 4–8, 10).

| Offset | Field | Encoding | Provenance | Confidence |
|---|---|---|---|---|
| 0–3 | Header: packet format, type, length, **partial flag** | format `0x56` = V3 home, `0x05` = debug; type `0x1` = status, `0x2` = debug | 📖 ESPHome | medium |
| 4 | Time remaining — hours | uint8 | 📖 both | high |
| 5 | Time remaining — minutes | uint8 | 📖 both | high |
| 6 | Time remaining — seconds | uint8 | 📖 both | high |
| 7 | **Actual** temperature | `2 × °C` | 📖 both (see note) | high |
| 8 | **Target** temperature | `2 × °C` | 📖 both (see note) | high |
| 9 | Mode | enum, see below | 📖 ESPHome | medium ⚠️ contested |
| 10 | Fan speed | step index 0–19 | 📖 both | high ⚠️ scaling contested |
| 11–14 | Max runtime, min/max temperature bounds | uint8 ×4 | 📖 ESPHome | low |
| 13–14 | *(alt.)* mode disambiguation bytes | — | ❓ MQTT bridge reads mode here | low ⚠️ conflicts with 11–14 |
| 15–16 | Turbo time remaining | uint16 | 📖 ESPHome | low |
| 17 | Ambient temperature | `2 × °C` (assumed) | 📖 ESPHome | low |
| 18 | Shutdown reason | enum, values unknown | 📖 ESPHome | low |
| 19–25 | **unknown** | — | ❓ | — |
| 26 | Firmware update phase | uint8 | 📖 ESPHome | low |
| 27 | Flags bitfield — LEDs, units (°C/°F), beeps, connection test | bitfield, bit positions unknown | 📖 ESPHome | low |
| 28 | Biorhythm sequence step | uint8 | 📖 ESPHome | low |
| 29 | Notification code | uint8 | 📖 ESPHome | low |

### Partial packets — the important one

The V3 status packet **exceeds the BLE notification payload size**. The notification sets
`is_partial = 1` in the header, and the remainder must be retrieved by an **explicit read** of
the status characteristic. An implementation that only subscribes will silently see truncated
state. 📖 ESPHome, high confidence — and the first thing to confirm on real hardware, because
it determines the whole read loop's shape.

### Notification behaviour

Rapid notifications while the unit is **on**; generally silent while **off**. 📖 ESPHome.
`MIN_NOTIFY_THROTTLE` upstream is 15 s; upstream also treats "no status within a timeout" as a
dead link. **Consequence: silence is ambiguous** — off, or disconnected. Our device model must
represent `available` (link) separately from `power` (device state) and must not infer one
from the other.

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

## Fan-speed encoding — genuinely contested

- ESPHome: `percent = 5 + 5 × step` → step 0 = 5 %, step 19 = 100 %.
- MQTT bridge: `percent = step × 5` → step 20 = 100 %.

A one-step disagreement. Since the index is documented as 0–19 (20 values), ESPHome's mapping
is the self-consistent one (0→5 %, 19→100 %) and the bridge's would need step 20 to reach
100 %. **We implement ESPHome's and mark it ❓ until measured.** Resolution is trivial: set a
known percentage on the physical remote, read byte 10. Logged as
[RL-002](../research/RESEARCH-LOG.md).

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
