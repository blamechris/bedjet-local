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
