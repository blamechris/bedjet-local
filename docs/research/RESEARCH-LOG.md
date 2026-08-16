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
Addresses were CoreBluetooth UUIDs, not MAC addresses. RSSI −75 dBm and −95 dBm.

(Addresses are not recorded here. They are host-local, meaningless on another machine, and one
of the two units turned out not to be ours — see RL-006. Ours lives in the gitignored device
registry; the other is nobody's business but its owner's.)
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

## RL-006 — Which of the two `BEDJET_V3` units is ours? ✅ RESOLVED

**Date:** 2026-08-16 (opened and resolved same day)
**Question:** Two BedJet V3s are in range. Are both ours (e.g. a dual-zone setup), or is one
a neighbour's?
**Setup:** Not yet run. Two discriminating tests, in preference order:

- **Power test (definitive).** Unplug our BedJet at the wall, re-scan, and note which address
  disappears. Plug back in, re-scan, confirm it returns. This identifies our unit by an
  action only its owner can take, and needs no connection.
- **Proximity test (weaker).** Scan from directly beside our unit, then from two rooms away.
  Ours should swing sharply; a neighbour's should not. Suggestive, not conclusive — RSSI is
  not identity.

**Observation:** The power test was run. With our BedJet unplugged at the wall, the −74/−75 dBm
unit **disappeared from the scan** and the −95 dBm unit remained. On restoring power it
returned, still around −74 dBm.
**Interpretation:** The stronger unit is ours. **The −95 dBm unit is not ours** — almost
certainly a neighbour's, and it must never be connected to. We own exactly one BedJet, so the
device layer does not need multi-device addressing yet.

The wider lesson is the one worth carrying to the next device: **this device class advertises
no identity at all.** Same name, same service UUID, no manufacturer data, a host-local address.
Any BedJet within radio range is indistinguishable from ours by advertisement alone, and RSSI
ordering would have picked the right one here only by luck. Identification required a physical
act (cutting power) that only the owner could perform. Expect to need an equivalent for the
next physical device rather than assuming discovery yields identity.
**Confidence:** ~~high~~ → **medium, downgraded 2026-08-16 by RL-008.** The original wording
here ("about as unambiguous as a test gets") was overstated. It assumed a scan is a complete
census of what is in range. RL-008 shows it is not: our own unit was absent from a scan one
minute before it connected successfully. Two correlated observations (vanished on unplug,
returned on replug) still make the conclusion likely, but "probable" is the honest word, and
the conclusive test is RL-008's remote correlation.
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Consequence:** our unit's address now lives in a **gitignored** local device registry
(`devices.local.toml`), and `bedjet identify` / `bedjet watch` refuse any address not in it.
The neighbour's unit is not merely un-preferred, it is unreachable through this tool without a
deliberate `--force` (`src/bedjet_local/device/registry.py`).
**Next question:** Does the device-name characteristic hold a user-settable name? If so, it is
a better identity anchor than an address, and it would survive moving the daemon to a Pi — where
the macOS address will not resolve at all. **Answered by RL-007: no.**

---

## RL-007 — GATT enumeration: seven characteristics, three undocumented

**Date:** 2026-08-16
**Question:** Does our device expose the GATT layout upstream describes, and does the "device
name" characteristic give us a stable identity anchor and a firmware version?
**Setup:** `uv run bedjet identify <ours>`, connected for ~2 s, read-only. Vendor app closed.
**Observation:** Connected first try, ~1 s. The service exposes **seven** characteristics:

| UUID | Properties |
|---|---|
| `…2000` | write, read, notify |
| `…2001` | write, read |
| `…2002` | write, read |
| `…2003` | write |
| `…2004` | write |
| `…2005` | write, read |
| `…2006` | write, read |

Reading `…2001` returned **4 bytes: `43 4a 65 3a`**.

**Interpretation:** Upstream's four known characteristics are all present, which corroborates
the survey. Three — `2002`, `2003`, `2006` — appear in **no upstream source we found**, including
the ESPHome codec and `bedjet-re`. `2005` exists, corroborating `bedjet-re`'s otherwise
single-sourced claim about sequence transfer.

Two properties differ from upstream's description:

- `2000` (status) is **writable**. Upstream treats it as notify + read. Purpose unknown.
- `2004` (command) is **write-only** — no read property. Consistent with a command sink.

`…2001` is **not a device name** on this firmware. `43 4a 65 3a` is not a plausible name and is
not a fragment of `BedJet` (`42 65 64 4a 65 74`). Three untested explanations: it is a
request/response channel rather than a static value (it is writable as well as readable, so a
read may return the residue of the last request); a never-set user name showing uninitialised
memory; or upstream's label is simply wrong for this revision. **No firmware version was found**
— not in the advertisement, not here.
**Confidence:** high for the observations; the interpretation of `2001` is open
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** — (characteristic read, not a status packet)
**Next question:** Where is the firmware version? Candidates: the three undocumented
characteristics, or the status packet's `update_phase` / flags region — which costs nothing to
check, because watching status is the next bring-up step anyway. **The undocumented
characteristics are NOT to be probed by writing.** Reading `2002`/`2006` is defensible (they are
readable and a read does not change state); `2003` is write-only and therefore untouchable until
we have a hypothesis, per `SAFETY.md`.

---

## RL-008 — A scan is not a census (and RSSI is not even stable)

**Date:** 2026-08-16
**Question:** How reliable is BLE discovery as evidence about what is in range? This matters
because RL-006 identified our unit by *absence* from a scan.
**Setup:** Two consecutive `bedjet discover` runs (10 s each), same position, ~1 minute apart,
followed immediately by a successful `bedjet identify` against our unit.
**Observation:**

| Run | Ours | The other unit |
|---|---|---|
| Earlier (RL-005) | −75 dBm | **−95 dBm** |
| Scan A | −74 dBm | **−75 dBm** |
| Scan B | **absent** | −76 dBm |
| `identify`, ~1 min later | **connected first try** | — |

**Interpretation:** Two things that the earlier reasoning quietly assumed are false.

1. **A scan is not a census.** Our unit was missing from Scan B and connected successfully
   about a minute later. A device can be absent from a 10 s window while perfectly present and
   healthy — advertising is intermittent, and macOS's scan results are a sample, not an
   inventory.
2. **RSSI is not even stable, let alone identifying.** The second unit moved 20 dB (−95 → −75)
   between sessions. 20 dB is two orders of magnitude in power. Whatever caused it — a door, a
   person, the laptop's position, interference — it means an RSSI reading carries almost no
   information about which device is which, and not much about siting either.

This **weakens RL-006**, which concluded ownership from a device disappearing when we cut its
power. That inference needed "absent from the scan ⇒ not powered", and (1) says the implication
does not hold. The conclusion is still *likely* — vanishing on unplug and returning on replug is
two correlated events, not one — but it is no longer near-certain, and RL-006's confidence has
been downgraded from high to medium accordingly.

Consequence for Phase 2 siting (ADR-0001): **a single RSSI reading is worthless as survey
data.** Siting must be judged on many samples over time, and on connection success and
disconnect rate, not on a dBm number from one scan.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question — the conclusive ownership test, and it is read-only:** connect, subscribe to
status, and operate our BedJet with **its physical remote**. If decoded state changes in step
with our button presses, the unit is ours, with no inference from scan behaviour at all. This is
bring-up steps 7–10 regardless, so it costs nothing extra. Run it before trusting RL-006.
