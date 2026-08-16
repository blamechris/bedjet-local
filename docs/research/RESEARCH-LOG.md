# Research log

Append-only. Newest entries at the bottom. One entry per meaningful experiment or resolved
question, in the fixed format below. **This file exists so that future sessions do not
rediscover the same protocol behaviour** — if you learned something about the device, it goes
here before the session ends.

```
## RL-NNN — <short title>
**Date:**
**Question:**
**Setup:**
**Observation:**
**Interpretation:**
**Confidence:** high | medium | low
**Provenance:** VERIFIED (our device) | UPSTREAM | HYPOTHESIS
**Fixture:** tests/fixtures/<name>.bin, or —
**Next question:**
```

---

## RL-001 — Do the two upstream temperature encodings actually conflict?

**Date:** 2026-08-16
**Question:** ESPHome documents the temperature byte as `2 × °C`; the pjt0620 MQTT bridge uses
an integer °F polynomial `((b − 0x26) + 66) − ((b − 0x26) / 9)`. Which is right?
**Setup:** No hardware. Evaluated both functions over the plausible byte range and compared
against the exact conversion `°F = (b/2) × 1.8 + 32`.
**Observation:** b=40 → ESPHome 20 °C, bridge 68 °F (= 20 °C). b=50 → 25 °C / 77 °F (= 25 °C).
b=60 → 30 °C / 86 °F (= 30 °C). Agreement across the range.
**Interpretation:** They are the same function. The bridge's formula is an integer
approximation of the Celsius→Fahrenheit conversion applied to `b/2`; it is a *display*
conversion, not a competing wire format. The wire format is `2 × °C`, corroborated by two
independent implementations in different languages by different authors.
**Confidence:** high
**Provenance:** UPSTREAM — corroborated, **not observed on our device**
**Fixture:** —
**Next question:** Does our unit report in the same units, and does byte 27's flags field
change the *reported* value or only the display on the remote? (Suspect display-only, but a
firmware that reports °F on the wire would break the decoder silently.)

---

## RL-002 — Fan-speed step base: `5 + 5×step` or `5×step`?

**Date:** 2026-08-16 (opened, unresolved)
**Question:** ESPHome maps fan step → percent as `5 + 5 × step`; the MQTT bridge as
`step × 5`. Both agree the field is at offset 10 and that the index range is 0–19.
**Setup:** Not yet run. **Planned:** set the fan to a known percentage using the *physical
remote* (no writes from us), capture the status packet, read byte 10.
**Observation:** —
**Interpretation (prior):** ESPHome's mapping is self-consistent with a 0–19 range
(0 → 5 %, 19 → 100 %); the bridge's would require step 20 to reach 100 %, which is outside the
documented range. Weak prior favouring ESPHome.
**Confidence:** low
**Provenance:** HYPOTHESIS
**Fixture:** — (will be `fan_<pct>_percent.bin`)
**Next question:** Is the step range actually 0–19, or is 0 a distinct "fan off" rather than
5 %?

---

## RL-003 — Mode byte location

**Date:** 2026-08-16 (opened, unresolved)
**Question:** ESPHome decodes mode at offset `[9]`. The MQTT bridge uses `[13]`/`[14]`, which
ESPHome documents as temperature bounds. Both cannot be describing the same layout.
**Setup:** Not yet run. **Planned:** cycle the unit through off → fan → cool → heat using the
physical remote only, capturing a status packet in each state, then diff the packets.
**Observation:** —
**Interpretation (prior):** `[9]` is the mode; `[13..14]` may disambiguate variants that share
a mode value (heat vs extended heat vs turbo), which the bridge may have latched onto because
it produced correct results for the modes it tested. Untested.
**Confidence:** low
**Provenance:** HYPOTHESIS
**Fixture:** — (will be `mode_<name>_status.bin` per mode)
**Next question:** A packet diff across modes will also reveal which of bytes 19–25 move — the
cheapest way to attack the unknown region without sending a single write.

---

## RL-004 — Status packet header layout

**Date:** 2026-08-16 (opened, unresolved)
**Question:** Upstream documents a 4-byte header carrying packet format, packet type, length and
a partial flag — but **not their order within bytes 0–3**. Which byte is which?
**Setup:** Not yet run. **Planned:** capture any status packet and look for `0x56`
(`PACKET_FORMAT_V3_HOME`) and `0x01` (`PACKET_TYPE_STATUS`) in the first four bytes; the length
byte should approximately equal the packet size.
**Observation:** —
**Interpretation (prior):** We have shipped a guess — `[0]` length, `[1]` partial, `[2]` format,
`[3]` type — chosen so the decoder can run at all. It is **flagged, not trusted**:
`decode_status` raises an anomaly when byte 2 is neither `0x56` nor `0x05`, and
`StatusPacket.header` always retains the raw four bytes, so one capture can correct the
interpretation without invalidating the fixture.
**Confidence:** low
**Provenance:** HYPOTHESIS
**Fixture:** —
**Next question:** Does the follow-up read that completes a partial packet repeat the header, or
continue from where the notification stopped? `reassemble()` currently assumes it does not
repeat — a second untested guess riding on this one.

---

## RL-005 — First contact: discovery on macOS

**Date:** 2026-08-16
**Question:** Is our device discoverable over BLE, does it advertise the service UUID the
upstream sources document, and what identity does the advertisement carry?
**Setup:** `uv run bedjet discover` (10 s scan) on macOS Darwin 25.5, MacBook Pro, single
scan position, vendor app not connected. Bluetooth permission granted to the terminal.
**Observation:** Two devices found, **both** named `BEDJET_V3`, both advertising service
`00001000-bed0-0080-aa55-4265644a6574` and nothing else. No manufacturer data on either.
Addresses were CoreBluetooth UUIDs (`947CDF5E-…`, `9713FDE6-…`), not MAC addresses. RSSI
−75 dBm and −95 dBm.
**Interpretation:** Four upstream claims are now confirmed on our hardware:

1. The BedJet service UUID is correct and is **advertised**, not merely present after
   connecting — so discovery can filter on it, which the scanner already does.
2. Our unit is a **V3** (BLE), not a V2 (Bluetooth Classic SPP). The whole approach holds.
3. macOS returns a CoreBluetooth UUID rather than a MAC, exactly as ADR-0001 predicted.
   Confirmed: **addresses are host-local** and config must never assume a MAC. An address
   recorded here will not resolve on a Pi.
4. The advertisement carries **no manufacturer data and no per-unit identity** — same name,
   same service UUID for both units. Identity has to come from after the connection.

Two units is the unexpected result, and it is not self-evidently good news — see RL-006.
RSSI is also weaker than hoped: −75 dBm is workable but not comfortable, and −95 dBm is at
the edge of usability. Neither reading was taken from a known position, so neither is yet
evidence about siting.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** — (advertising data only; no packet captured)
**Next question:** RL-006, and then: does the device-name characteristic hold a user-settable
name that could distinguish units after connecting?

---

## RL-006 — Which of the two `BEDJET_V3` units is ours? (BLOCKING)

**Date:** 2026-08-16 (opened, unresolved — **blocks all connection attempts**)
**Question:** Two BedJet V3s are in range. Are both ours (e.g. a dual-zone setup), or is one
a neighbour's?
**Setup:** Not yet run. Two discriminating tests, in preference order:

- **Power test (definitive).** Unplug our BedJet at the wall, re-scan, and note which address
  disappears. Plug back in, re-scan, confirm it returns. This identifies our unit by an
  action only its owner can take, and needs no connection.
- **Proximity test (weaker).** Scan from directly beside our unit, then from two rooms away.
  Ours should swing sharply; a neighbour's should not. Suggestive, not conclusive — RSSI is
  not identity.

**Observation:** —
**Interpretation (prior):** −95 dBm is roughly what a BLE device reads through a wall or two,
which is consistent with a neighbour's unit — and equally consistent with our own unit under a
mattress at the far end of the house. The reading does not decide it.
**Confidence:** —
**Provenance:** HYPOTHESIS
**Fixture:** —
**Why this blocks:** connecting to a BedJet takes its single BLE connection slot. Doing that to
a device that is not ours would knock a stranger's app off their own heater, and is not ours to
do. It is also self-defeating for the project: a protocol fixture captured from an unknown unit
with unknown firmware is worthless as evidence, which is the whole point of `PROVENANCE.md`.
**Next question:** If both units are ours, the device layer needs multi-device addressing from
the start rather than as a retrofit — and since the advertisement carries no per-unit identity,
we will need our own stable mapping from a host-local address to a human name ("left", "right").
