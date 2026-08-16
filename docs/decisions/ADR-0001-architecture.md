# ADR-0001 — Transport architecture: host BLE daemon, with an ESP32 proxy as a swappable transport

**Date:** 2026-08-16
**Status:** Accepted (Phase 1); deployment host deferred pending measurement

## Context

The brief asks us to evaluate Option A (host daemon → host BT adapter → BedJet), Option B
(ESP32 running ESPHome → BLE → BedJet, exposed via HA/MQTT), or a justified hybrid.

One physical fact dominates: **BLE is roughly 10 m and degrades badly through walls.** Whatever
speaks BLE must live in the bedroom. That is a constraint on *where a radio sits*, not on where
our logic runs — and conflating the two is what pushes people into Option B prematurely.

A second fact from the survey: **the BedJet permits exactly one BLE client at a time.**
Connection ownership is a first-class design concern regardless of option.

## Options

### Option A — host daemon with the host's own BLE adapter

| | |
|---|---|
| Reliability | Good; depends on host uptime and adapter quality |
| Latency | Lowest — one hop, no network in the control path |
| Dev complexity | Lowest. Edit, run, observe, in one language, with a debugger |
| Portability | High — same code on macOS (dev) and Linux/Pi (prod) via `bleak` |
| BLE range | ✗ **Requires the host to be in the bedroom** |
| Always-on | Needs dedicated hardware (a Pi) to be always-on |
| Observability | Excellent — full packet visibility in our own process |
| Cost | $0 today (existing Mac); ~$20–60 for a Pi later |
| Extensibility | Excellent — this becomes the device-layer pattern |

### Option B — ESP32 running ESPHome, surfaced through Home Assistant / MQTT

| | |
|---|---|
| Reliability | Very good; a dedicated MCU that does nothing else |
| Latency | +1 network hop, plus HA's own event loop if HA is in the path |
| Dev complexity | ✗ High for *our* goals — protocol work in C++ on an MCU, flash-to-test cycles |
| Portability | ✗ Locks the protocol knowledge into ESPHome's component model |
| BLE range | ✓ Excellent — an $8 board sits under the bed |
| Always-on | ✓ Excellent |
| Observability | ✗ Poor — serial logs, not a debugger; the codec is upstream's, not ours |
| Cost | ~$8–15 |
| Jarvis integration | Indirect — Jarvis would talk to HA, not to a device layer we own |
| **Licensing** | ✗ ESPHome's codec is **GPLv3** (see [ADR-0003](ADR-0003-licensing.md)) |

Option B is the fastest route to *a working BedJet*. It is close to the worst route to the
brief's actual objective — "deeply understand the software-to-hardware boundary" and "establish
patterns we can reuse". It makes us a **consumer** of someone else's protocol implementation,
which is precisely the vendor-software relationship we are trying to exit, only with a nicer
vendor.

### Option C — hybrid: our codec, swappable transport

The survey turned up the fact that makes this concrete: **`bleak-esphome` (3.9.7, PyPI)
provides a Bleak backend that talks through an ESPHome Bluetooth Proxy.** An ESP32 can be a
dumb BLE↔WiFi radio while our own Python codec runs unchanged on a hub elsewhere in the house.

That reduces "ESP32 vs host daemon" from an architecture decision to a **transport
selection** — exactly the boundary the brief's `transport/` layer already draws. We get
Option B's range for Option A's development experience, and we can change our minds later
without touching `protocol/` or `device/`.

## Decision

**Option C, staged — and the `transport/` abstraction is what makes the staging free.**

1. **Phase 1 (now, $0):** host BLE daemon on the Mac, using `bleak`'s CoreBluetooth backend.
   Carry the laptop to the bedroom for bring-up sessions. This achieves Milestone 1
   (discover → connect → read state) **today, with hardware we already own**, and it is the
   right rig for RE work anyway: a real debugger, real logging, fast iteration.
2. **Phase 2:** measure before buying. Log RSSI and connection stability from each candidate
   permanent location (bedroom shelf, hallway, existing hub position). Let the data pick the
   deployment host.
3. **Phase 3:** deploy to whichever the measurement selects —
   - *Pi in the bedroom* (Pi Zero 2 W ≈ $15–20, or Pi 4/5 if we want headroom for future
     devices): `bleak` on BlueZ, same code, one systemd unit. Preferred if we expect more
     in-room devices.
   - *ESP32 Bluetooth Proxy* (≈ $8) + daemon on an existing always-on host: swap
     `BleakTransport` for the `bleak-esphome` backend. Preferred if the bedroom should hold
     no more than a radio.

**We do not buy hardware yet.** Phase 1 needs none, and Phase 2 produces the evidence that
picks the right purchase. Deferring the spend until it is informed is the whole point.

## Consequences

- `transport/` must define a narrow interface (`discover`, `connect`, `read`, `write`,
  `subscribe`, `disconnect`) that both a direct `bleak` client and a proxied one satisfy,
  plus a mock for CI. Nothing above `transport/` may import `bleak`. This is enforceable by a
  test and should be.
- macOS's CoreBluetooth **does not expose BLE MAC addresses** — `bleak` returns a
  system-assigned UUID on macOS and a MAC on Linux. Device identity is therefore
  **platform-dependent**, and configuration must not assume a MAC. `INFERRED FROM UPSTREAM` —
  to be confirmed in bring-up step 3.
- Single-client exclusivity means the service layer owns a **connection lease** with an
  explicit yield, so the vendor app and our daemon can take turns rather than fight.
- Home Assistant remains reachable via the MQTT adapter (see ADR-0002) without HA ever being
  in Jarvis's path.
