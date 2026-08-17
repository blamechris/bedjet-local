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
**Provenance:** ✅ **VERIFIED 2026-08-16** (upgraded by RL-012: byte 8 = 0x30 = 48 → 24.0 °C =
75.2 °F with the app's target set to **75 °F**. Confirmed against independent ground truth, not
merely corroborated between upstreams.)
**Fixture:** `tests/fixtures/cool_fan50_target75f.bin`
**Next question:** Does our unit report in the same units, and does byte 27's flags field
change the *reported* value or only the display on the remote? (Suspect display-only, but a
firmware that reports °F on the wire would break the decoder silently.)

---

## RL-002 — Fan-speed step base: `5 + 5×step` or `5×step`? ✅ RESOLVED

**Date:** 2026-08-16 (opened and resolved same day)
**Question:** ESPHome maps fan step → percent as `5 + 5 × step`; the MQTT bridge as
`step × 5`. Both agree the field is at offset 10 and that the index range is 0–19.
**Setup:** Not yet run. **Planned:** set the fan to a known percentage using the *physical
remote* (no writes from us), capture the status packet, read byte 10.
**Observation:** ✅ **RESOLVED by RL-012.** With the vendor app set to **50 %**, byte 10 read
`0x09` (step 9). ESPHome's `5 + 5 × step` gives 50 ✓. The bridge's `5 × step` gives 45 ✗.
**Interpretation:** ESPHome is correct; the MQTT bridge's mapping is off by one step. Prior
reasoning below was right for the right reason.
**Interpretation (prior):** ESPHome's mapping is self-consistent with a 0–19 range
(0 → 5 %, 19 → 100 %); the bridge's would require step 20 to reach 100 %, which is outside the
documented range. Weak prior favouring ESPHome.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** `tests/fixtures/cool_fan50_target75f.bin`
**Next question:** Is the step range actually 0–19, or is 0 a distinct "fan off" rather than
5 %?

---

## RL-003 — Mode byte location ✅ RESOLVED (with a twist — see RL-012)

**Date:** 2026-08-16 (opened and resolved same day)
**Question:** ESPHome decodes mode at offset `[9]`. The MQTT bridge uses `[13]`/`[14]`, which
ESPHome documents as temperature bounds. Both cannot be describing the same layout.
**Setup:** Not yet run. **Planned:** cycle the unit through off → fan → cool → heat using the
physical remote only, capturing a status packet in each state, then diff the packets.
**Observation:** ✅ **RESOLVED by RL-012.** Byte 9 IS the mode. But it read `0x04` while the
unit was **cooling**, and `0x04` is *turbo* in the command table — so the status enum is a
**different enum from the command enum**. Byte 13 (`0x26` → 66.2 °F) does look like the minimum
temperature bound, consistent with ESPHome rather than the bridge.
**Interpretation (prior):** `[9]` is the mode; `[13..14]` may disambiguate variants that share
a mode value (heat vs extended heat vs turbo), which the bridge may have latched onto because
it produced correct results for the modes it tested. Untested.
**Confidence:** high for the location; the value table is barely started (only COOL verified)
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** `tests/fixtures/cool_fan50_target75f.bin`
**Next question:** A packet diff across modes will also reveal which of bytes 19–25 move — the
cheapest way to attack the unknown region without sending a single write.

---

## RL-004 — Status packet header layout ✅ SOLVED

**Date:** 2026-08-16 (opened and solved same day)
**Question:** Upstream documents a 4-byte header carrying packet format, packet type, length and
a partial flag — but **not their order within bytes 0–3**. Which byte is which?
**Setup:** Not yet run. **Planned:** capture any status packet and look for `0x56`
(`PACKET_FORMAT_V3_HOME`) and `0x01` (`PACKET_TYPE_STATUS`) in the first four bytes; the length
byte should approximately equal the packet size.
**Observation:** ✅ **SOLVED by RL-012.** Header is `01 56 1b 01`:
`[0]` partial flag, `[1]` format `0x56`, `[2]` **payload length** (`0x1b` = 27), `[3]` type
`0x01`. Proof: 27 + 4 = 31 = the exact size of the reassembled packet.
**Interpretation:** Our guess had the fields in the wrong order. Retaining the raw header meant
one capture corrected it without invalidating anything — the design worked as intended.
**Interpretation (prior):** We have shipped a guess — `[0]` length, `[1]` partial, `[2]` format,
`[3]` type — chosen so the decoder can run at all. It is **flagged, not trusted**:
`decode_status` raises an anomaly when byte 2 is neither `0x56` nor `0x05`, and
`StatusPacket.header` always retains the raw four bytes, so one capture can correct the
interpretation without invalidating the fixture.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** `tests/fixtures/cool_fan50_target75f.bin`
**Next question:** ✅ Answered too — the remainder continues the packet and does **not** repeat
the header. The reassembled length matches the declared length exactly.

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
**Confidence:** ✅ **high — CONFIRMED 2026-08-16 by RL-012**, which settles it independently
of any scan behaviour: the unit reported back the exact state we set in our own app. The
downgrade note below is kept because the reasoning error it records was real.

~~medium, downgraded 2026-08-16 by RL-008.~~ The original wording
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

---

## RL-009 — Our unit stopped advertising entirely (OPEN)

**Date:** 2026-08-16
**Question:** Why did `bedjet watch` fail 30 minutes after a successful `identify`?
**Setup:** `identify` succeeded at 15:35 (connected in ~1 s, clean disconnect). `watch`
attempted at 16:05 against the same address; a `discover` followed immediately.
**Observation:** `watch` failed after the full 20 s timeout with bleak's
`BleakDeviceNotFoundError` — *"Device with address … was not found"*. The following scan found
**only the other unit**, at −83 dBm. Ours appeared in **no** scan. RSSI history for the other
unit is now −95, −75, −76, −83.
**Interpretation:** Two distinct things happened, and the tooling initially hid one of them.

1. **The failure was a discovery failure, not a refused connection.** Bleak resolves an
   address by scanning before it connects, so "not found" means no connection was ever
   attempted. Our error message said *"the BedJet allows only one BLE client at a time — check
   the vendor app"*, which describes a refusal and points at the wrong cause entirely. Fixed:
   `BleakDeviceNotFoundError` is now caught separately with an accurate message.
2. **Our unit is not advertising.** Candidate causes, cheapest to test first:
   - **Another client holds the link.** Many BLE peripherals stop advertising while
     connected, and the BedJet permits exactly one client. The vendor app on a phone
     reconnecting in the background would produce exactly this. *Most likely.*
   - **The unit lost power or entered a deep idle.** It was found in earlier scans while
     off, so merely being off does not stop advertising — but a longer idle might.
   - **Our own earlier session left it in a bad state.** The disconnect looked clean, but a
     peripheral that fails to resume advertising after a client leaves is a known BLE bug
     class. If a power cycle restores it, this is the answer.
   - **BLE address rotation.** On macOS the address is a host-local CoreBluetooth UUID
     derived from the peripheral's identity. If the device rotated to a new random address,
     macOS could know it under a *different* UUID — and our registry, keyed on the old one,
     would never match again. This would also explain RL-008's oddities, including two
     entries at once and the 20 dB swing, if the "other" unit were ever ours under a second
     identity.
**Confidence:** high that it is not advertising; **the cause is untested**
**Provenance:** ✅ VERIFIED (observation) / ❓ HYPOTHESIS (cause)
**Fixture:** —
**Consequence for the architecture:** if address rotation is real, **address-keyed identity is
unusable on macOS**, and the registry needs a different anchor. This strengthens the case for
moving to a Pi sooner than ADR-0001's Phase 3 planned: BlueZ exposes real MAC addresses, so a
Linux host would settle the rotation question outright and give us a stable key.
**Next question:** the diagnostic ladder, in order — (a) is the vendor app open on the phone?
(b) `bedjet discover --repeat 5`, since one scan is not a census; (c) power-cycle the unit and
re-scan immediately; (d) if a **new** UUID appears where ours used to be, rotation is confirmed
and the registry design changes.

`bedjet discover --repeat N --interval S` was added for exactly this: it reports presence as
"seen in K/N scans" with an RSSI span, and warns when a *registered* device is absent from all
of them. It also gives ADR-0001's Phase 2 siting survey the many-samples basis RL-008 says it
needs.

**UPDATE 2026-08-16, largely resolved by RL-010.** A 5-round survey found our unit in **5/5
scans under the same address**, `947CDF5E…`. Two of the four candidate causes are now dead:

- ❌ **BLE address rotation — ruled out.** The address came back unchanged. Address-keyed
  identity is safe on macOS after all, and the registry design stands.
- ❌ **Power loss / deep idle — ruled out.** Nothing was power-cycled between the failure and
  the survey; the unit recovered on its own.
- ✅ **Marginal link — now the leading explanation.** RL-010 measured a **22 dB swing** on our
  unit while stationary, with a floor of −95 dBm. A connect attempt landing in a fade fails
  exactly as observed: bleak cannot resolve the address, and reports "device not found" — a
  weak link is indistinguishable from a dead device at that layer.
- ➖ **A client holding the link** remains possible and untestable after the fact.

The lesson worth keeping: **"not found" on a marginal link is the expected failure mode, not an
anomaly.** Intermittent connect failures at this signal level should be retried, not diagnosed.

---

## RL-010 — Our unit's signal swings 22 dB while stationary

**Date:** 2026-08-16
**Question:** How stable is the radio link to our BedJet from the working position, and is the
location good enough to run a daemon from?
**Setup:** `bedjet discover --repeat 5` — five 10 s scans, 5 s apart, laptop stationary.
**Observation:**

| Device | Seen | RSSI range | Spread |
|---|---|---|---|
| ours (`947CDF5E…`) | 5/5 | −95 … −73 dBm | **22 dB** |
| the other unit | 5/5 | −86 … −80 dBm | 6 dB |

**Interpretation:** Both units advertise reliably — presence is not the problem. **Signal
stability is.** A 22 dB spread is a ~160× swing in received power, on a stationary laptop, over
about a minute. −73 dBm is comfortable; −95 dBm is at the edge of usability. Our link crosses
that whole range repeatedly.

The inversion is the interesting part: the **nearer** unit is four times more variable than the
**further** one. That points at a multipath/obstruction problem rather than distance — a BedJet
sits low, under or beside a bed, with a metal frame, a mattress and a moving human in the path.
The other unit's steadier signal suggests a cleaner path, probably through a single wall with
nothing moving in it.

Consequences:

1. **RL-009's failure is explained** without invoking address rotation or a phantom client.
2. **Retry logic is not optional.** `bleak-retry-connector` is already a dependency
   (ADR-0002) and must be wired into the transport before any daemon runs unattended.
3. **This position is not a deployment site.** It is fine for bring-up, where a human retries.
   It is not fine for something expected to hold a link overnight.
4. **ADR-0001's Phase 2 siting survey now has a metric**: not "what is the RSSI" but "what is
   the *spread*, and how often does it dip below −90". `discover --repeat` reports both and
   warns at ≥10 dB.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Also fixed here — a reporting bug of ours.** The per-round output printed
`BEDJET_V3 -74dBm, BEDJET_V3 -85dBm` with no indication of *which* device each reading belonged
to. Results are sorted by RSSI, so when the two units cross over the columns swap silently, and
the rounds read as though one device's numbers were the other's. Reconstructing which was which
required working backwards from the aggregate spans. Per-round readings are now labelled by
registry name or address stub. A display that quietly attributes one device's measurements to
another is worse than no display in a project whose entire discipline is provenance.
**Next question:** Does the link hold well enough *while connected* to sustain a notification
stream, or does it drop mid-session? Connected-mode behaviour is a different question from
advertising visibility, and `watch` answers it directly.

---

## RL-011 — No working physical remote: the validation method has to change

**Date:** 2026-08-16
**Question:** Every bring-up plan so far assumed a physical remote as the independent control
path — for the ownership test, for setting known states, and as the safety escape hatch. The
remote is broken. What replaces it?
**Setup:** Owner-reported constraint: control is via the **BedJet 3 Smart Remote** app
(Android) only.
**Observation / consequences:**

**1. A safety claim in `SAFETY.md` was wrong and has been corrected.** It stated "the physical
remote is not a BLE client and is unaffected by anything we do over Bluetooth — the owner can
always override us at the device." That was written from the general case and never checked
against this installation. It is the same error class the provenance rules exist to prevent —
upstream/general knowledge asserted as an observation — applied to a *safety* claim, which is
worse than getting a byte offset wrong.

The real override paths are now, in order: **unplug it at the wall** (primary — always
available, needs no radio), then the vendor app (**not independent**: one BLE client at a time,
so it can only take over after we release the link).

**2. Simultaneous observation is impossible.** The planned "operate the remote while watching
the stream" test cannot be run: the app and our client cannot both hold the link.

**3. But it does not need to be simultaneous — the device holds its own state.** Set a known
state in the app, release the app's link, connect ours, read it back. That is *better* evidence
than live correlation for fixtures, because the ground truth is exact and recorded in advance
rather than inferred from a button press at an approximate moment.

**4. The ownership test is replaced by an app-correlation test.** While the app is connected to
our BedJet, that unit should refuse our connection — and, if peripherals here stop advertising
while connected (RL-009's open sub-question), may vanish from scans entirely. Whichever address
responds to the app's presence is the one the app controls, and the app is paired to our unit.

**Interpretation:** The constraint costs us live correlation and gains us exact, pre-recorded
ground truth. Net effect on fixture quality is positive. The safety cost is real, though, and is
the reason `bedjet watch` now defaults to **releasing the link after 120 s** (`--seconds`): a
hung session would otherwise lock the owner out of the only remaining control path to their own
heater.
**Confidence:** high
**Provenance:** ✅ VERIFIED (owner-reported installation fact)
**Fixture:** —
**Next question:** the revised step 8–10 procedure, run once per state:

1. App: set an exact, written-down state (mode, fan %, target).
2. **Force-close the app** to release the link.
3. `bedjet watch <ours> --raw --seconds 60 --save tests/fixtures/<name>.bin`
4. Compare decoded output against what was set; record in `PROVENANCE.md`.
5. Repeat for a different fan % and a different mode.

Two states at different fan percentages settle RL-002; two at different modes settle RL-003;
every capture contributes to RL-004.

---

## RL-012 — First real packet: four questions closed, one new one opened

**Date:** 2026-08-16
**Question:** Does the decoder read a real status packet correctly, and what do the contested
bytes actually contain?
**Setup:** Vendor app set to **Cool, fan 50 %, target 75 °F**, written down first, then
force-closed to release the link. `bedjet watch <ours> --raw --seconds 60 --save`. 55
notifications over ~60 s. Fixture: `tests/fixtures/cool_fan50_target75f.bin`.
**Observation:**

```
      [0]  [1]  [2]  [3]  [4]  [5]  [6]  [7]  [8]  [9] [10] ...
      01   56   1b   01   09   3b   19   2d   30   04   09  ...  (31 bytes)
```

**Interpretation — four open questions closed at once:**

**✅ RL-004, the header, is SOLVED — and our guess was wrong.** Actual layout is
`[0]` partial flag, `[1]` format `0x56`, `[2]` **payload length**, `[3]` type `0x01`. We had
guessed length/partial/format/type. The proof is arithmetic the device asserts about itself:
`0x1b` = 27 payload bytes + 4 header = **31**, exactly the size received. This is why the raw
header was always retained — one capture corrected the interpretation without invalidating
anything.

**✅ RL-001, temperature, VERIFIED on our device.** Byte 8 = `0x30` = 48 → 24.0 °C → 75.2 °F,
against a target of **75 °F** set in the app. `2 × °C` confirmed against independent ground
truth, not just against two agreeing upstreams.

**✅ RL-002, fan scaling, SETTLED.** Byte 10 = `0x09` = step 9, with the app set to **50 %**.
ESPHome's `5 + 5 × step` → 50 ✓. The MQTT bridge's `5 × step` → 45 ✗. ESPHome is right.

**✅ RL-003, mode location, RESOLVED — with a twist that matters.** Byte 9 *is* the mode, as
ESPHome says. But byte 9 read `0x04` while the unit was **cooling**, and `0x04` is **turbo** in
the command table — so our first run cheerfully displayed `mode=turbo` for a unit that was
cooling. **The status enum and the command enum are different enums.** Every upstream command
table circulating for this device describes what you *send*, not what you *read*, and nothing
we found says so.

This is the most consequential finding yet, because it is the kind that stays silent: the wrong
mode decodes without an error, looks reasonable, and would have propagated into the device
model, the API, and eventually Jarvis. Only ground truth caught it.

Consequence: `StatusMode` now contains **only** `COOL = 0x04`. Anything else decodes to `None`
with an anomaly rather than to a guess, and `Power` stays `UNKNOWN` unless the mode is verified.
A plausible-looking wrong mode is worse than an admitted unknown on a device that makes heat.

**✅ The partial-packet mechanism is real, and completeness is now decided properly.** The
capture arrived as a notification plus a follow-up read and reassembled to exactly the declared
length. But our reader keyed off the partial *flag*, which is set on the first fragment and
therefore **stays set after reassembly** — and our own flag detection was accidentally reading
byte 1 (`0x56`, truthy) rather than byte 0. Both are fixed: completeness is now
`len(raw) >= declared_length + 4`. A length the device asserts about itself beats a flag we half
understand.

**✅ RL-006, ownership, CONFIRMED.** The unit reported back exactly the state we set in our own
app. No inference from scan behaviour required. Upgraded from medium to high.

**Also observed, unresolved:**

- Byte 13 = `0x26` → 19.0 °C → 66.2 °F, matching the documented **minimum** target. Consistent
  with upstream's "temperature bounds", ❓ unconfirmed.
- Byte 14 = `0x34` → 26.0 °C → 78.8 °F. If this is a maximum bound it is **not** the documented
  104 °F — possibly a Cool-mode-specific limit. ❓
- **The packet is 31 bytes; upstream's layout ends at byte 29.** Byte 30 = `0x31` is
  unaccounted for by any public source.
- Bytes 19–25 (`12 01 9a 01 10 ff 00`) remain opaque.
- Still no firmware version anywhere.

**Confidence:** high
**Provenance:** ✅ VERIFIED (our device, against pre-recorded ground truth)
**Fixture:** `tests/fixtures/cool_fan50_target75f.bin` — 11 assertions in
`tests/unit/test_real_fixtures.py`
**Next question:** capture **one state per mode** from the app — off/standby first, then heat,
dry, turbo — and diff. Each fills one row of `StatusMode` and lights up part of bytes 19–25 and
27. The off capture is the most valuable single one: it names the standby value, which is what
`Power` needs to stop saying UNKNOWN.

---

## RL-013 — Three modes captured: a checksum nobody documented, and per-mode limits

**Date:** 2026-08-16
**Question:** What are the remaining status-mode values, and what do the still-unknown bytes
do? Capture one state per mode from the vendor app and diff.
**Setup:** Three further captures (`off_standby`, `off_after_heating`, `turbo`) plus the
existing `cool`, each set in the app and the app force-closed before connecting. Five packets
total, three distinct modes.
**Observation:**

| | mode | remaining | max runtime | permitted range | target | byte 30 |
|---|---|---|---|---|---|---|
| off | `0x00` | 0:00:00 | 0:00 | 10.0–40.0 °C | 24.0 °C | `0x8e` |
| off (after heat) | `0x00` | 0:00:00 | 0:00 | 10.0–40.0 °C | 31.5 °C | `0x66` |
| cool | `0x04` | 9:59:25 | 12:00 | 19.0–26.0 °C | 24.0 °C | `0x31` |
| turbo | `0x02` | 0:09:47 | 0:10 | 43.0–43.0 °C | 43.0 °C | `0xb3` |

**Interpretation:**

**✅ Byte 30 is a CHECKSUM — and no upstream source mentions one.** The whole packet sums to
zero mod 256, i.e. byte 30 = `(-sum(bytes 0..29)) & 0xFF`. It holds across all five packets and
all three modes. ESPHome, every HA integration, and `bedjet-re` all stop their layout at byte 29
and none describes a checksum.

This is worth more than a field. It is real integrity checking on an undocumented protocol, and
it **independently proves our reassembly is correct** — a mis-joined notification-plus-read
would not sum to zero. Our confidence in the partial-packet handling stops resting on plausible
arithmetic and starts resting on the device's own verification.

**✅ Status modes: `0x00` standby, `0x02` turbo, `0x04` cool.** And note what that means
against the command table, which calls `0x02` *cool* and `0x04` *turbo*: **the two enums have
cool and turbo swapped.** That is a far nastier defect than an unrelated mapping — a
command-decoded status packet does not produce nonsense, it produces the *other real mode*.
Cooling reads as turbo; turbo reads as cool. Nothing would look broken.

**✅ Bytes 11–14 are per-mode limits, not device limits.** Max runtime and permitted target
range both move with the mode: standby 0:00 / 10.0–40.0 °C, cool 12:00 / 19.0–26.0 °C, turbo
0:10 / a fixed 43.0 °C.

**And that exposed a bug of ours.** Turbo's 43.0 °C is **109.4 °F — above the 104 °F that
upstream and the manufacturer's own marketing call the maximum.** Our hardcoded 19–40 °C range
flagged a perfectly healthy turbo packet as anomalous. The device publishes its own limits;
validating against a constant we read in a spec sheet was the wrong instinct. The decoder now
checks the target against the device-reported bounds, and the constants survive only as an
absolute sanity envelope for catching a decode that has genuinely gone wrong.

This has a safety consequence too, and it points the same way: any clamp we apply in
Milestone 2 must come from bytes 13–14, not from a hardcoded table. The device is the authority
on what it will accept.

**✅ Bytes 15–16 count seconds elapsed in turbo.** Two consecutive turbo packets read 13 then
14 while remaining fell 9:47 → 9:46 against a 10:00 limit. `elapsed + remaining == max runtime`
exactly. The field is **big-endian**, which is unusual for BLE — little-endian would give 3328
and 3584, which fit nothing. What the data cannot yet separate is big-endian-u16 from a plain
u8 at byte 16; a capture more than 255 s into turbo settles it, and the 600 s turbo limit makes
that reachable.

**✅ Bytes 19–25 are invariant across every capture and every mode** (`12 01 9a 01 10 ff 00`).
They are therefore **not state** — configuration or identity. Given we have failed to find the
firmware version in the advertisement, in characteristic `2001`, or anywhere else, this region
is the leading candidate for it. ❓ Untestable without a second device or a firmware update.

**⚠️ We still have no heat capture.** The capture labelled "heat" decoded as `mode=0x00` with
the timer at 0:00:00 — the unit was **off**, with an outlet temperature of 32.0 °C and a target
of 88.7 °F still set. That reads as *shortly after heating*, not *heating*. It is a useful
fixture (it shows a cooldown and confirms standby) but it is not heat, and it is filed under
its true state rather than its intended one. `HEAT`, `DRY` and `EXT_HEAT` remain unknown.

Related and reassuring: the turbo capture was taken with the app closed and our client
connected, and the unit kept running its timer throughout. **The device does not stop when the
controlling BLE client disconnects** — one fewer safety worry for unattended operation.

**Also:** the fan byte holds its **last-set** value in standby (an idle unit reported 50 %
straight after a 50 % cool session). Reporting that as live airflow would be wrong, so
`describe()` now marks it "(last set)" when the unit is off.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device, 5 packets, 3 modes)
**Fixtures:** `off_standby.bin`, `off_after_heating.bin`, `turbo_fan100_target109f.bin`
**Next question:** a real **heat** capture — set heat in the app, confirm it is actually
running (timer counting down) before force-closing the app. Then `dry`. Two more captures
complete the status-mode table. A turbo capture past the 4-minute mark would also settle the
byte 15–16 endianness question for free.

---

## RL-014 — Heat is `0x01`, and the mode table now makes a falsifiable prediction

**Date:** 2026-08-16
**Question:** What is heat's status-mode value? (The previous attempt captured a stopped unit.)
**Setup:** Heat set in the vendor app and **confirmed running** — timer counting down — before
force-closing the app. `bedjet watch --raw --packets 3 --seconds 60`.
**Observation:**

```
01 56 1b 01 00 1d 28 3f 3f 01 13 0c 00 2d 50 00 00 2c 00 12 01 9a 01 10 ff 00 15 34 00 00 fb
                     ^^ mode 0x01     ^^ ^^ range 22.5-40.0C
```

Mode `0x01`. Target 31.5 °C / 88.7 °F, actual equal to it (at temperature). Fan 100 %.
Remaining 0:29:40 of a 12:00 maximum. Permitted range **22.5–40.0 °C (72.5–104.0 °F)** — a
fourth distinct per-mode range. Checksum valid; the second packet's checksum incremented by
exactly 1 as the seconds byte decremented by 1, which is a small extra confirmation of RL-013.

**Interpretation:** The verified status-mode table is now:

| Value | Mode | Command table calls it |
|---|---|---|
| `0x00` | standby | `0x01` = off |
| `0x01` | **heat** | cool |
| `0x02` | turbo | heat |
| `0x04` | cool | turbo |

Four values, a conspicuous gap at `0x03`, and they fit the ordering
`standby, heat, turbo, extended-heat, cool, dry, …`. **That is a prediction:** `0x03` is
extended heat and `0x05` is dry.

It is recorded in `STATUS_MODE_PREDICTION` and deliberately **kept out of `StatusMode`**. A
prediction that decodes silently is indistinguishable from a fact, and the whole discipline
here is that those two must never be confusable — four numbers fitting a pattern is not
evidence about a heater. The decoder surfaces the prediction in its anomaly message so the
next capture can *refute* it. A `dry` capture is now a real experiment with a stated
expectation rather than a data-gathering chore.

Note also how badly the command table misleads: it calls `0x01` *cool*, and status `0x01` is
*heat*. Decoding a heating unit with the command enum would have reported cooling. This is the
third distinct way that conflation produces a confidently wrong answer.

**Confidence:** high for `0x01` = heat; the prediction is explicitly low
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** `tests/fixtures/heat_fan100_target89f.bin`
**Next question:** capture `dry` — it either confirms `0x05` and the ordering, or refutes both
in one shot. Either outcome is worth more than the mode value itself, because it tells us
whether the ordering hypothesis can be trusted for `0x03`, which we would otherwise have to
reach by selecting extended heat.

**Observability follow-up applied here:** the reader was logging an identical anomaly warning
for every packet — several per second — and an INFO line per split packet, which made a real
session unreadable and forced the operator to hold Ctrl-C. Anomalies are now logged only when
the set changes, and the split-packet notice is logged once then dropped to debug. Combined
with `watch`'s repeat collapsing and `--packets N`, a capture is now a short, readable run
that ends on its own.

---

## RL-015 — Dry is `0x05`: the prediction held, and dry reports an impossible target

**Date:** 2026-08-16
**Question:** RL-014 predicted, from the ordering `standby, heat, turbo, extended-heat,
cool, dry`, that dry would be `0x05`. Does it hold?
**Setup:** Dry set in the vendor app, app force-closed, `bedjet watch --packets 3 --save`.
**Observation:**

```
01 56 1b 01 09 3b 21 39 2c 05 13 0c 00 30 3e 00 00 2c 00 12 01 9a 01 10 ff 00 15 34 00 00 ff
                     ^^ mode 0x05  ^^ ^^ range 24.0-31.0C
```

Mode **`0x05`** — as predicted. Fan 100 %, remaining 9:59:33 of a 12:00 maximum, actual
28.5 °C, checksum valid.

**Interpretation: the prediction was correct.** The status-mode ordering is
`standby(0), heat(1), turbo(2), ?(3), cool(4), dry(5)`, and the verified table is now five
values deep with a single gap.

`0x03` is *still* not added to `StatusMode`. One correct prediction raises the odds; it does
not convert a guess into an observation, and the gap is the one value we would most want to be
sure about (extended heat, on a heater). It stays in `STATUS_MODE_PREDICTION` until captured.
The test enforcing this now says exactly that.

**But dry reports a target below its own minimum.** Permitted range `24.0–31.0 °C`, target
`22.0 °C`. Every other mode's target sits inside its range:

| Mode | Range | Target | Inside? |
|---|---|---|---|
| heat | 22.5–40.0 | 31.5 | ✓ |
| cool | 19.0–26.0 | 24.0 | ✓ |
| turbo | 43.0–43.0 | 43.0 | ✓ |
| **dry** | **24.0–31.0** | **22.0** | **✗** |

Two hypotheses, neither tested:

1. **Dry does not use a target.** Dehumidifying has no setpoint in the usual sense, so the
   byte may simply be stale or meaningless in this mode.
2. **The target tracks ambient in dry.** In this packet `target == ambient == 22.0 °C`
   exactly. That is either a real relationship or a coincidence, and one packet cannot tell
   the difference.

A second dry capture at a different room temperature separates them cleanly: if the target
follows ambient, hypothesis 2; if it stays at 22.0 °C, hypothesis 1.

**The decoder flagged this correctly and the anomaly is asserted rather than suppressed.** It
is the first time the anomaly machinery has caught something real about the device rather than
about our own assumptions, and the test that pins it explains why the packet is "unclean" on
purpose.
**Confidence:** high for `0x05` = dry; the target anomaly is unexplained
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** `tests/fixtures/dry_fan100.bin`
**Next question:** a second dry capture at a different ambient temperature, to separate the
two hypotheses. Low priority — it does not block anything.

---

## RL-016 — Command encoder built; nothing can send it

**Date:** 2026-08-16
**Question:** What does Milestone 2 need before the first write, and what does the protocol
work so far tell us about how much to trust the command table?
**Setup:** No hardware. `protocol/encode.py` written as a pure module.
**Interpretation — the reason this needs saying out loud:**

**The command table is on weaker ground than it looks.** It comes from the same upstream
sources that also implied a status-mode enum — and that implication was *wrong* in a
particularly bad way (RL-012/13/14): the two enums are offset, so every overlapping value
decodes as another real mode rather than as nonsense. Cooling read as turbo, turbo as heat,
heating as cool.

That does not make the command values wrong; they were presumably derived from watching the
vendor app's writes, which is decent evidence. It does mean **the source has demonstrably
conflated two tables once already**, and we have zero independent verification of any command
byte. So Milestone 2's rule is: **every command is verified by reading the status back.** A
write that does not produce an observable state change has not succeeded, it has merely not
errored.

Design consequences already applied:

- `set_temperature` **requires** the device-reported bounds (`min_temp_c`/`max_temp_c` from the
  current status packet) and refuses without them. RL-013 showed the range moves with the mode
  and that turbo's 43 °C exceeds the "documented" 104 °F maximum, so a hardcoded table would be
  both wrong and unsafe.
- `set_timer` likewise takes the device-reported `max_runtime_s` — turbo's is 10 minutes.
- Out-of-range values **raise rather than clamp**. Silently heating to a different temperature
  than the caller asked for is the wrong failure mode for a heater.
- `set_mode` rejects a `StatusMode` by type. Passing one would select a plausible, wrong mode
  with no symptom.

**Nothing can send any of this.** The old "no encoder may exist" guard was deleted deliberately
along with this change, and replaced by a stronger one: `test_no_code_path_sends_a_command`
asserts that **no module outside `transport/` calls a write at all**. The invariant that
mattered was never the file's absence — it was that no code path can put bytes on the wire.
The first write remains a deliberate, attended event.
**Confidence:** the design is sound; the byte values are unverified
**Provenance:** 📖 UPSTREAM for every command byte
**Fixture:** —
**Next question:** the first write: `01 01` (off), with a human present, the unit visible, and
status read back immediately to confirm the mode became `0x00` standby. That single exchange
verifies the opcode, the operand, the characteristic, and the write path all at once — and off
is the one command whose failure mode is a device that stays on.

---

## RL-017 — ❌ FALSE VERIFICATION: the tool reported a heater off while it was running

**Date:** 2026-08-16
**Status:** the OFF command is **NOT verified**. The claim in RL-016's plan is retracted.
**Question:** Does `01 01` turn the device off?
**Setup:** Unit running in Cool via the vendor app, app closed, `bedjet off`. Write sent,
read-back performed.
**Observation:** The tool printed:

```
✅ verified: the device did what we asked.
    after:  power=off  mode=standby  ...  remaining=255:00:21  anomalies=4
```

**The BedJet did not turn off.** The "after" state came from this, saved as
`tests/fixtures/corrupt_tail_fragment.bin`:

```
01 9a 01 10 ff 00 15 34 00 00 22      ← 11 bytes, format byte 0x9a, checksum fails
```

That is **bytes 20–30 of a real status packet** — a tail fragment, delivered as if it were a
whole packet. Its byte 9 is really byte 29 (the notify code), which happened to be `0x00`,
and `0x00` is standby. `remaining=255:00:21` should have been the giveaway to a human, but
nothing in the code was looking.

**Interpretation — four defects, all of them mine, that had to line up:**

1. **Root cause: concurrent GATT reads corrupted each other.** Every status arrives split and
   the device notifies several times a second, so the reader span a follow-up read *per
   notification*. Bleak's CoreBluetooth backend keys pending reads by characteristic handle,
   so overlapping reads of one characteristic collide — visible in the log as
   `KeyError: 41` and `CancelledError` storms. One read's remainder was delivered as another's
   notification.
2. **The reader decoded a tail fragment as a fresh packet.** Nothing checked that a
   notification *began* a packet, so garbage was parsed at offsets that mean something else.
3. **The reader published a packet that failed its own checksum.** This is the galling one.
   **We had discovered the checksum (RL-013), implemented it, tested it, and then did not
   consult it at the one point where trusting a corrupt packet is dangerous.** A finding that
   is not wired into the decision it protects is decoration.
4. **Verification gated on a field, not on the packet's integrity.** `send_off` checked
   `mode is STANDBY` and nothing else — so noise that happened to contain a zero byte was
   accepted as proof about a physical device.

**The deeper lesson.** The read-back loop was built precisely because "the write returned
cleanly" proves nothing. It then failed in exactly the same shape one level up: **"the
read-back said standby" also proves nothing unless the read-back is trustworthy.** Verifying
an unverified thing with an unverified thing is not verification. Every layer that turns bytes
into a claim needs its own integrity gate, and this project already had the gate built.

**Fixes, all with regression tests that replay the real corrupt bytes:**

- `BleakTransport` serialises GATT operations behind a lock — the backend limitation is real
  and belongs to the layer that owns the backend.
- `StatusReader` allows **one** in-flight follow-up read; notifications arriving during one
  are skipped and counted, which is safe because the device repeats itself constantly.
- `looks_like_packet_start()` rejects notifications that do not begin a packet.
- `StatusPacket.is_trustworthy` (checksum passed **and** complete) — and the reader **never
  publishes** a packet that fails it.
- `Commander` requires a trustworthy packet for both the baseline and the confirmation, and
  refuses to send at all without a trustworthy baseline.
- `stop()` cancels in-flight reads, ending the teardown error storm; read failures log once,
  not with a full traceback per packet.

**What we still do not know: what `01 01` actually did.** The unit did not switch off. It may
have been ignored, or it may have done something else. Note the shape of RL-014's finding:
status and command enums are offset, and **if commands in fact use the same values as status,
`01 01` means *heat*, not *off*** — status `0x01` is heat. That is a hypothesis, not a
conclusion, but it is the first thing the next experiment should test, and it argues for
checking what the unit was doing after the write rather than assuming nothing happened.
**Confidence:** high on the defects; the command's effect is unknown
**Provenance:** ✅ VERIFIED (our device) for the failure; ❓ for what `01 01` does
**Fixture:** `tests/fixtures/corrupt_tail_fragment.bin`
**Next question:** re-run `bedjet off` with the fixes in place. Three outcomes, all
informative: verified (the command table is right and only our plumbing was broken);
`CommandUnverified` (the device genuinely ignores `01 01`, and the command table is wrong —
test `01 00` next, matching the status enum); or refused (the link is too poor to command
safely, which is its own answer).

---

## RL-018 — `01 01` was never sent: wrong BLE write type, silently dropped

**Date:** 2026-08-16
**Question:** With RL-017's fixes in place, `bedjet off` failed honestly —
`CommandUnverified`, the unit stayed in Cool, its timer ticking down normally. Is the command
table wrong, or is something else?
**Setup:** Unit running Cool / fan 100 % / target 66 °F via the app. `bedjet off`, retry after
the RL-017 fixes. 81 packets, 43 split, **1 rejected**, 38 skipped while busy — the reader is
now healthy, versus the corruption storm before.
**Observation:** The write returned cleanly. Absolutely nothing changed: mode stayed `cool`,
and the remaining timer went 9:59:43 → 9:59:23, i.e. the device carried on exactly as before.
Not a refusal, not an error, not a partial effect — **no effect at all**.

**Interpretation.** "No effect at all" is a strong clue, and it points away from the operand.

Consider both readings of the byte we sent. Under the upstream *command* table, `01 01`
means off. Under the *status* enum (RL-014), `0x01` means heat. **Neither happened.** A wrong
mode value would have produced the *other* mode — that is precisely what makes the offset
enums dangerous. Producing no change under either interpretation says the command never
reached the device at all.

And it did not. **BLE has two distinct write types, and they are separate GATT properties:**
`write` means *write-with-response*; `write-without-response` is its own property. From
RL-007, our command characteristic declares exactly:

```
char 00002004-bed0-0080-aa55-4265644a6574  [write]
```

Only `write`. No `write-without-response`. And `BleakTransport.write` defaulted to
`response=False` — write-*without*-response. **CoreBluetooth silently discards a
without-response write to a characteristic that does not support it.** No error, no
exception, nothing on the wire. Our own log even said `WRITE … <- 01 01`, because the
transport dutifully reported the call it made.

So the command table is **not** exonerated or convicted; it was never tested. The bytes never
left the Mac.

**This is the same failure shape as RL-017 one layer down.** There, the read-back looked like
verification but was not, because nothing checked the packet's integrity. Here, the write
looked like a write but was not, because nothing checked the transport's own success. In both
cases the code reported what it *did* rather than what *happened* — and the information needed
to tell the difference was already in hand. RL-007 recorded `[write]` for this characteristic
weeks of work ago; nothing consulted it.

**Fix.** The transport now selects the write type from the characteristic's **declared
properties** rather than a default, and callers pass `response=None` to mean "ask the device".
This is the same principle as the temperature bounds in RL-013: the device publishes what it
accepts, so ask it instead of assuming. `Commander` pins nothing, and a test asserts it never
does.
**Confidence:** high that the write type was wrong; the command table remains **untested**
**Provenance:** ✅ VERIFIED (our device) for the no-op; ❓ for what `01 01` does when it
actually arrives
**Fixture:** —
**Next question:** re-run `bedjet off`. This is the first time `01 01` will genuinely reach
the device. If the unit switches off, the upstream command table is right and our transport
was the whole problem. If it does not, the command table is finally, properly falsified — and
the next candidate is `01 00`, matching the status enum's standby value.

**Also worth recording — the RL-017 fixes worked.** One rejected packet instead of a
corruption storm, no teardown tracebacks, and an honest `CommandUnverified` where the previous
run produced a false "✅ verified". The failure was reported accurately, which is the only
reason the real cause was findable.

---

## RL-019 — ✅ FIRST VERIFIED COMMAND: `01 01` turns the BedJet off

**Date:** 2026-08-16
**Question:** With the write type corrected (RL-018), does `01 01` actually turn the device
off?
**Setup:** Unit running Cool / fan 100 % / target 66 °F via the vendor app, app force-closed.
`bedjet off`. The log now reports the write type explicitly:
`WRITE … <- 01 01 (with response)`.
**Observation:**

```
before: power=on   mode=cool     actual=22.5C  target=19.0C  fan=100%  remaining=9:55:23
after:  power=off  mode=standby  actual=23.0C  target=19.0C  fan=100% (last set)
```

The unit switched off. Reader health: 66 packets, 3 split, **1 rejected**, 63 skipped while
busy — no corruption storm, and the confirming packet passed its checksum.

**Interpretation.** ✅ **`01 01` = OFF is VERIFIED on our hardware.** The first command this
project has actually proven, and the first protocol claim we have promoted from upstream
guesswork by *doing* it rather than by reading it.

One result settles four things at once:

1. The command characteristic (`…2004`) is right.
2. The `[opcode, operand]` framing is right.
3. The opcode `0x01` is right.
4. **The command mode enum is real** — and that is the interesting one. RL-014 found that
   status and command enums disagree on every value they share, which left open whether
   upstream's command table was simply *wrong*. It is not. **Command `0x01` is off and status
   `0x01` is heat: two genuinely different enums, both correct.** The finding is now confirmed
   from both sides, and the remaining command values become decent bets rather than guesses —
   though each still gets verified on its own.

**And the last two failures were worth having.** RL-017's false "✅ verified" and RL-018's
silently-dropped write were both cases of code reporting *what it did* rather than *what
happened*. Fixing them is what makes this result mean anything: the same tool that now says
"verified" said "unverified" twenty minutes ago on the same command, and was right both times.
A verification system that has only ever passed is not evidence of anything.

**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question:** command #2, and the bring-up order (`SAFETY.md`) says the next-safest
observable one. There is no fan-only mode in the status enum, so the natural step is
**fan speed** (`07 <step>`): thermally inert, directly observable in status byte 10, and it
exercises a **different opcode**, which OFF did not. Then Cool (`01 02`), then Heat last and
attended.

**Minor follow-on observed here:** 63 of 66 notifications were skipped while a follow-up read
was in flight. Harmless — the device repeats constantly and we still verified in under a
second — but it suggests the notification-plus-read dance is doing more work than needed. The
status characteristic is directly readable, so polling a whole packet may be simpler and
cheaper than reassembling one. Worth measuring, not urgent.

---

## RL-020 — Opcode `0x07` verified; the fan mapping confirmed from the *write* side

**Date:** 2026-08-16
**Question:** Does `07 <step>` set fan speed, and does a second opcode behave like the first?
**Setup:** Unit running Cool at fan 50 % via the app, app force-closed.
`bedjet fan … 100` → wrote `07 13` (step 19). Then `bedjet off` again, to check reproducibility.
**Observation:**

```
fan:  before fan=50%   → after fan=100%   (4 s later, timer undisturbed)
off:  before mode=cool → after mode=standby
```

Both verified. The operator also **heard** the fan change — a second channel of confirmation
the software cannot fabricate.

**Interpretation:**

**✅ Opcode `0x07` (set fan) is VERIFIED.** Two opcodes now proven, which matters more than
one: it establishes that `[opcode, operand]` is a general framing rather than a coincidence
that happened to work for a single command.

**✅ The fan-step mapping is now confirmed from the write side too.** RL-002 established
`percent = 5 + 5 × step` by *reading* byte 10 with the app set to 50 %. Here we *sent* step 19
and the device reported 100 %. Read-side and write-side agree, using the same formula in
opposite directions — a much stronger result than either alone, and the kind of round-trip
that catches an off-by-one that a one-directional test would miss.

**✅ OFF is reproducible.** Second independent confirmation, on a different starting state
(fan 100 % rather than 50 %). A command that has worked once could be a coincidence of timing;
twice, from different states, is a behaviour.

**Also observed:** the fan change did **not** reset the run timer (9:59:40 → 9:59:36, i.e. it
just kept counting). Changing fan speed is not treated as starting a new session. Worth knowing
before anything builds scheduling on top.

Each run discarded 1–2 untrustworthy packets, quietly and correctly. That the checksum gate
fires routinely — and that nothing downstream ever sees those packets — is the RL-017 fix
earning its keep in normal operation rather than only in the failure that produced it.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question:** command #3 by the bring-up order: **mode** (`01 <mode>`) with a non-OFF
operand. OFF proved the opcode and that the command enum exists; it did not test a single
other value in that enum. Cool (`01 02`) and Dry (`01 05`) are the thermally safe ones and go
first. Heat, turbo and extended heat stay locked — they are the commands that make heat, and
they come last and attended, per `SAFETY.md`.

---

## RL-021 — Command `01 02` starts the unit cooling; the enum offset demonstrated live

**Date:** 2026-08-16
**Question:** Does a mode operand other than OFF work, and does it produce the *status* value
RL-014 predicts rather than the command value?
**Setup:** Unit in standby. `bedjet mode … cool` → wrote `01 02`.
**Observation:**

```
before: power=off  mode=standby  target=19.0C  fan=100% (last set)
after:  power=on   mode=cool     target=19.0C  fan=100%   remaining=10:00:00
```

**Interpretation:**

**✅ Command `0x02` = COOL is VERIFIED**, and this is the first time we have *started* the
device rather than stopped or adjusted it.

**The enum offset is now demonstrated rather than inferred.** We sent command `0x02`; the
device reported status `0x04`. Both mean cool. Every earlier statement about the two enums came
from comparing captures against what the vendor app was set to — this is the first time a
*command* and its resulting *status* were observed in the same exchange, and they differ
exactly as predicted. A verifier that compared the command byte to the status byte would have
failed a device that did precisely the right thing.

**✅ Starting a mode sets a fresh 10:00:00 timer.** The unit went from standby to a full
10-hour session. Pair that with RL-020's finding that a fan change leaves the timer alone, and
the rule appears to be: **changing the mode starts a session; adjusting within a session does
not.** Note also that cool's *maximum* runtime is 12:00 (RL-013) while its default session is
10:00 — the two are different numbers and both are reported.

**Three opcodes' worth of the command table is now proven:** `01 01` off, `01 02` cool,
`07 <step>` fan. The remaining gaps are temperature (`03`), timer (`02`), presets, and the
heating modes — the last deliberately so.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question:** temperature (`03 <temp>`), the last substantial non-heating command. It is
safe to test **in cool mode**, where the device reports a permitted range of 19.0–26.0 °C and
enforces it — setting a cool target cannot make the bed hotter. It is also the first command
whose operand is not a small enum but an encoded *value*, so it tests the temperature encoding
from the write side, exactly as RL-020 did for the fan mapping.

---

## RL-022 — Opcode `0x03` verified; the command table is proven for normal operation

**Date:** 2026-08-16
**Question:** Does `03 <temp>` set the target, and does the temperature encoding hold from the
write side?
**Setup:** Unit cooling, target 19.0 °C. `bedjet temp … 72` → 72 °F → 22.22 °C → rounded to
22.0 °C → wrote `03 2c` (44).
**Observation:** `target 19.0C → 22.0C`. The device reported its permitted range as
19.0–26.0 °C (66–79 °F) beforehand, and 22.0 °C sits inside it.

**Interpretation:**

**✅ Opcode `0x03` is VERIFIED**, and with it the temperature encoding **in both directions**.
RL-001 established `byte = 2 × °C` by reading (byte `0x30` = 48 with the app set to 75 °F);
here we *sent* `0x2c` = 44 and the device reported 22.0 °C. Same formula, opposite directions —
the same round-trip that made the fan mapping solid in RL-020.

This is the third such round-trip (fan, mode, temperature), and the pattern is worth naming:
**an encoding verified only by reading is half-verified.** A read-side test confirms our
interpretation of what the device says; a write-side test confirms the device agrees with our
interpretation. An off-by-one or an inverted scale can survive the first and not the second.

**Milestone 2's core is complete.** The verified command set now covers everything needed for
normal non-heating operation:

| Command | Bytes | Verified |
|---|---|---|
| Off | `01 01` | ✅ RL-019, reproduced RL-020 |
| Cool | `01 02` | ✅ RL-021 |
| Set target | `03 <2×°C>` | ✅ RL-022 |
| Set fan | `07 <step>` | ✅ RL-020 |

Every one was sent by software we wrote, to a device whose protocol has no public
specification, and every one was confirmed by observed state change rather than by the write
returning cleanly — which, per RL-018, it does whether or not anything happens.

**Still unproven:** timer (`02 <hh> <mm>`), presets/buttons (`0x10`–`0x22`), dry (`01 05`), and
the heating modes. The heating modes are refused in code rather than merely untested.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question:** heat (`01 03`) is the one remaining capability that matters functionally —
a bed climate device that cannot warm the bed is half a device — and `SAFETY.md` has always
put it last and attended. It requires unlocking `THERMALLY_SAFE_MODES`, which is a deliberate
edit, and running it with a low target, a short timer, and a human watching.

---

## RL-023 — Heat verified; targets and session lengths are **per-mode**

**Date:** 2026-08-16
**Question:** Does `01 03` select heat, and does the target carry over from the previous mode?
**Setup:** Unit cooling, target 22.0 °C, 9:50:17 remaining. `bedjet mode … heat`, then
`bedjet off` about 45 s later.
**Observation:**

```
cool → heat:  target 22.0C → 31.5C     remaining 9:50:17 → 0:30:00
45s of heat:  actual 22.0C → 30.0C     (outlet air, +8 °C)
heat → off:   mode heat → standby      target 31.5C retained
```

**Interpretation — three findings, one of which corrects a claim I made before the run:**

**✅ Command `0x03` = HEAT is VERIFIED**, producing status `0x01`. The offset enums hold for a
fourth mode.

**1. Targets are remembered per mode, not carried across.** The target did not stay at cool's
22.0 °C; it became **31.5 °C**, which is *exactly* the value the heat capture in RL-014
recorded (`heat_fan100_target89f.bin`, byte 8 = `0x3f` = 63 = 31.5 °C). The device keeps a
target per mode and restores it on selection.

This also gives RL-015's oddity a likely explanation. Dry reported a target of 22.0 °C below
its own 24.0–31.0 °C minimum — if targets are per-mode and remembered, dry's was simply never
set within its range, and the device stores it without clamping. Testable: set a dry target
inside its range, switch away, switch back, and see whether it returns.

**2. Session length is per-mode too — and heat's default is 30 minutes, not 10 hours.** Cool
started a 10:00:00 session (RL-021); heat started **0:30:00**. Both are well inside their
respective 12:00 maxima, so these are *defaults*, not limits.

**⚠️ This corrects something I told the operator before the run.** I said heat would run on a
10-hour default and that `off` was therefore the only stop. That was wrong: I generalised
cool's default to heat without checking, which is precisely the upstream-assumed-as-observed
error this project's provenance rules exist to prevent — committed here in a safety briefing
rather than in a document. **The device self-limits heat to 30 minutes**, which is a
meaningfully safer default than I described.

**3. The outlet reached 30.0 °C from 22.0 °C in about 45 seconds** with the target at 31.5 °C —
the unit heats fast and was approaching its setpoint. Note `actual` is outlet air, not bed
temperature.

**✅ OFF verified from a heating state** — its third confirmation, and the one that matters
most. A software stop proven only against a cooling unit would not have earned the confidence
placed in it.

Incidentally, ambient fell 22.5 → 18.5 °C across the cooling session, then held during heat.
The ambient sensor evidently sits in the unit's own airflow, so it tracks what the BedJet is
doing rather than the room. Worth knowing before anything treats it as a room thermometer.
**Confidence:** high
**Provenance:** ✅ VERIFIED (our device)
**Fixture:** —
**Next question:** confirm the per-mode target hypothesis directly — set a distinct target in
each of cool and heat, switch between them, and check each returns. It costs two commands we
have already verified and would settle RL-015's dry anomaly as a side effect.
