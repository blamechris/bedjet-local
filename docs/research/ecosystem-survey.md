# BedJet ecosystem survey

**Date:** 2026-08-16
**Method:** fresh web research + primary-source inspection (upstream source files, GitHub API,
PyPI API). Nothing here is from prior model knowledge alone; every claim below has a source
or a verification command.
**Status of every claim on this page:** `UPSTREAM` — read from someone else's source or docs.
Nothing on this page has been observed on our device. See
[`docs/protocol/PROTOCOL.md`](../protocol/PROTOCOL.md) for the provenance rules.

---

## 1. Official vendor API / SDK / BLE documentation

**Finding: none exists.**

- No developer documentation, SDK, local API, or published BLE specification on
  [bedjet.com](https://bedjet.com/pages/support-center).
- No official GitHub organisation.
- The vendor ships two mobile apps (`com.bedjet` — "BedJet 3 Smart Remote"; plus a legacy V2
  app). Control is app + physical remote; there is no documented programmatic surface.
- **Consequence:** every integration in existence is reverse-engineered. There is no
  first-party contract to depend on, and no vendor commitment to protocol stability across
  firmware revisions. Our protocol layer must be defensive about unknown/changed bytes rather
  than assume a spec.

**Cloud dependency:** the V3 has a WiFi radio, but reporting indicates the app's WiFi setup
exists for firmware updates / cloud features, and that **BLE is the control path**. This is
good news for the local-control goal: the control plane we care about is local by design, not
something we are bypassing.

## 2 & 3. ESPHome BedJet support

**Status: present and current in the `dev` branch as of 2026-08-16.** Not deprecated, not
removed (checked the 2025.2 → 2026.6 changelogs for a removal notice; none found — the
2025.2 removal that comes up in searches is *custom components* generally, unrelated).

| | |
|---|---|
| Component | `esphome/components/bedjet/` — hub + `climate`, `fan`, `sensor` platforms |
| Docs | https://esphome.io/components/climate/bedjet/ |
| Requires | ESP32, `esp32_ble_tracker`, `ble_client` bound to the BedJet's MAC |
| Supported models | **"Only BedJet V3 is supported. BedJet V2 and other devices are not currently supported."** (verbatim, upstream docs) |
| Key files | `bedjet_hub.{h,cpp}`, `bedjet_codec.{h,cpp}` |

**Documented behaviours worth stealing (as knowledge, not code):**

- Status is **notification-driven**. The device pushes rapid updates while it is **on**, and
  generally sends nothing while **off**. So "connected" and "producing status" are independent
  states, and a silent link is not necessarily a broken link.
- The V3 status packet **exceeds the BLE notification MTU**. A notification arrives with
  `is_partial == 1`, and the remainder must be fetched by an **explicit read** of the status
  characteristic. Any implementation that only subscribes and never reads will silently
  truncate state. This is the single most important implementation detail found.
- `MIN_NOTIFY_THROTTLE = 15000 ms` and a status-timeout constant — upstream treats "no status
  within N" as *connection presumed unusable*, which is a reconnect trigger, not just a warning.
- Time sync is a first-class command (`CMD_SET_CLOCK`) — the device has an RTC that drifts and
  drives its own scheduling.

## 4. Home Assistant integrations

**There is no BedJet integration in Home Assistant core.** Verified, not assumed:

```
curl -s -o /dev/null -w "%{http_code}" \
  https://api.github.com/repos/home-assistant/core/contents/homeassistant/components/bedjet
→ 404
```

`https://www.home-assistant.io/integrations/bedjet/` also returns 404.

⚠️ **A search-engine AI summary claimed HA has a core BedJet integration built on a `pybedjet`
library. Both claims are false.** `pybedjet` does not exist on PyPI (`/pypi/pybedjet/json`
returns no `info` object). This is logged because it is the exact failure mode the brief's
provenance rule targets — a confident secondary source inventing a primary one. Custom
integrations only, all HACS-installed.

## 5. Open-source implementations (licence + liveness verified via GitHub API)

| Repo | Licence | Last push | Stars | Notes |
|---|---|---|---|---|
| [`esphome/esphome`](https://github.com/esphome/esphome) (bedjet component) | **Dual: GPLv3 for `.c/.cpp/.h/.hpp/.tcc/.ino`, MIT for Python and everything else** | 2026-08-16 | 11.5k | The reference implementation. The codec is C++ → **GPLv3**. |
| [`asheliahut/ha-bedjet`](https://github.com/asheliahut/ha-bedjet) | MIT | 2025-11-30 | 43 | Most-starred HA custom integration — **ARCHIVED**. |
| [`robert-friedland/ha-bedjet`](https://github.com/robert-friedland/ha-bedjet) | MIT | 2024-08-09 | 14 | Dormant. |
| [`blueharford/ha-bedjet-v3`](https://github.com/blueharford/ha-bedjet-v3) | MIT | 2026-02-04 | 7 | Most recently maintained HA integration. |
| [`pjt0620/Home-Assistant-Bedjet`](https://github.com/pjt0620/Home-Assistant-Bedjet) | **GPL-3.0** | 2021-10-23 | 36 | BLE→MQTT bridge, Pi-tested. Abandoned. |
| [`markus1189/bedjet-re`](https://github.com/markus1189/bedjet-re) | **none (all rights reserved)** | 2025-12-30 | 1 | Biorhythm/sequence protocol docs, derived by decompiling the vendor Android app v1.0.6. |

### Licence conclusion — this is load-bearing

**The best implementation (ESPHome's codec) is GPLv3, and the most detailed sequence
documentation is unlicensed.** We cannot port either into a permissively-licensed library
without either taking the GPL or committing infringement.

Protocol *facts* (a UUID, a byte offset, "mode 0x03 means heat") are not copyrightable
expression; a line-by-line translation of `bedjet_codec.cpp` is a derivative work. The
distinction is real and we will respect it:

- **DO** use upstream as technical evidence and as a hypothesis generator.
- **DO** attribute upstream in `PROTOCOL.md` per field.
- **DO NOT** copy, transliterate, or "port" GPLv3 or unlicensed source into `src/`.
- **DO** write our decoder from the documented facts plus our own captures, and let our own
  fixtures be the authority.

This is recorded as [ADR-0003](../decisions/ADR-0003-licensing.md).

## 6. Known BLE protocol information

Service and characteristic UUIDs — consistent across ESPHome and the independent MQTT bridge,
which is meaningful corroboration (two implementations, different authors, different
languages, same values):

| UUID | Role |
|---|---|
| `00001000-bed0-0080-aa55-4265644a6574` | Service |
| `00002000-bed0-0080-aa55-4265644a6574` | Status (notify + read) |
| `00002001-bed0-0080-aa55-4265644a6574` | Device name |
| `00002004-bed0-0080-aa55-4265644a6574` | Command (write) |
| `…2005…` | Biorhythm/sequence fragment transfer (per `bedjet-re`) |

The last 12 bytes are ASCII: `BedJet` → `42 65 64 4a 65 74`. A vendor vanity UUID, which is
itself weak evidence the whole family is stable across firmware.

Commands, modes, and the status layout are recorded field-by-field with per-field provenance
and confidence in [`docs/protocol/PROTOCOL.md`](../protocol/PROTOCOL.md) rather than
duplicated here.

## 7. Device compatibility

| | Transport | Support |
|---|---|---|
| BedJet V1/V2 | **Bluetooth Classic (SPP)** — not BLE | No BLE work applies. ESPHome explicitly excludes it. |
| BedJet V3 | **BLE GATT** | All of the above. **This is our device.** |
| "BedJet 3 v1.2.0" (our unit, per owner) | assumed BLE | ⚠️ `HYPOTHESIS` — the `1.2.0` string is unverified and may be app version, firmware version, or hardware revision. **Bring-up step 2 must read it off the device.** |

**Firmware differences:** no public matrix exists. Upstream carries an "update phase" byte and
a firmware-upgrade check, implying the vendor does ship firmware updates that could move bytes
under us. Our fixtures must therefore record the firmware version they were captured against.

## 8 & 9. Simultaneous BLE clients / does the app monopolise the link?

**Yes — the connection is exclusive.** Upstream ESPHome documentation states verbatim:

> "Only one client can be connected to the BedJet BLE service at a time, so you cannot use the
> BedJet mobile app" while another client is connected.

Consequences we must design for, not discover later:

1. **Our daemon holding the link locks the owner out of the vendor app**, and vice versa. A
   permanently-connected daemon is a usability decision, not just a technical one.
2. We need a **yield/release path** — a way to drop the link on request so the phone app can
   connect (and, during RE work, so we can watch the app's traffic).
3. **The physical remote is unaffected** (it is not the BLE client), so the owner is never
   locked out of the device itself. This materially lowers the risk of the whole project.
4. Connection-stealing between two of our own hosts (e.g. Mac during dev, Pi in production)
   will be a real failure mode. Single-owner enforcement belongs in the service layer.

## 10. Existing protocol documentation / RE notes

- `markus1189/bedjet-re` — the deepest write-up (Biorhythm sequences, 5-byte steps, fragment
  protocol over char `2005`, clock-time bit encoding). Author states it was verified against
  the decompiled Android app v1.0.6 and that it was produced with Claude Code. **Unlicensed →
  reference only, never vendored.** Also: verified-against-a-decompiler is not
  verified-against-hardware, which is a weaker claim than it sounds.
- ESPHome's codec is itself the de-facto spec for everything except sequences.
- The `reverse-engineering-ble-devices` readthedocs guide is a good generic methodology
  reference for the capture work in Phase 2.

---

## Open contradictions between upstream sources

These are not resolved by more reading. They are resolved by our own capture, and they seed
the [research log](RESEARCH-LOG.md).

1. **Fan-speed step offset.** ESPHome: percent = `5 + 5 × step` (step 9 → 50%). MQTT bridge:
   percent = `step × 5` (step 10 → 50%). A one-step disagreement, trivially settled by setting
   50% on the remote and reading byte 10.
2. **Mode byte location.** ESPHome decodes mode at `[9]`; the MQTT bridge uses `[13]`/`[14]`.
   Hypothesis: `[9]` is the mode and `[13..14]` disambiguate variants that share it (heat vs
   extended-heat vs turbo). Untested.
3. **Temperature encoding — RESOLVED ANALYTICALLY, no hardware needed.** The two sources
   *appear* to conflict (ESPHome: `byte = 2 × °C`; MQTT bridge: an integer °F polynomial
   `((b-0x26)+66) - ((b-0x26)/9)`). They are the same function. For `b = 40`: ESPHome → 20°C;
   bridge → `(2+66) - 0` = 68°F = 20°C. For `b = 60`: 30°C; `(22+66) - 2` = 86°F = 30°C. For
   `b = 50`: 25°C; `(12+66) - 1` = 77°F = 25°C. The bridge's formula is an integer
   approximation of `°F = (b/2)×1.8 + 32`. **Wire format is `2 × °C`**, corroborated by two
   independent implementations. Confidence: high — but still to be confirmed against our
   device, because agreement between two upstreams is not observation.

## Sources

- [ESPHome BedJet component docs](https://esphome.io/components/climate/bedjet/)
- [`bedjet_codec.h`](https://github.com/esphome/esphome/blob/dev/esphome/components/bedjet/bedjet_codec.h) · [`bedjet_hub.h`](https://github.com/esphome/esphome/blob/dev/esphome/components/bedjet/bedjet_hub.h) · [ESPHome LICENSE](https://github.com/esphome/esphome/blob/dev/LICENSE)
- [ESPHome BedJetHub API reference](https://api-docs.esphome.io/classesphome_1_1bedjet_1_1_bed_jet_hub)
- [`pjt0620/Home-Assistant-Bedjet` — `bedjet.py`](https://github.com/pjt0620/Home-Assistant-Bedjet/blob/main/bedjet.py)
- [`markus1189/bedjet-re`](https://github.com/markus1189/bedjet-re)
- [`blueharford/ha-bedjet-v3`](https://github.com/blueharford/ha-bedjet-v3) · [`asheliahut/ha-bedjet`](https://github.com/asheliahut/ha-bedjet) · [`robert-friedland/ha-bedjet`](https://github.com/robert-friedland/ha-bedjet)
- [BedJet support centre](https://bedjet.com/pages/support-center) · [BedJet 3 Smart Remote (Play Store)](https://play.google.com/store/apps/details?id=com.bedjet)
- [ESPHome issue #3807 — "Can't connect to my Bedjet V3"](https://github.com/esphome/issues/issues/3807)
- [Reverse Engineering BLE Devices (methodology)](https://reverse-engineering-ble-devices.readthedocs.io/en/latest/protocol_reveng/00_protocol_reveng.html)
