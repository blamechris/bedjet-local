# ADR-0002 — Python 3.11+ / `bleak` core, with HTTP+WS and MQTT as thin adapters

**Date:** 2026-08-16
**Status:** Accepted

## Context

The fleet is TypeScript-heavy (chroxy, repo-relay), C# (stock-keep), and Swift (no-it-all).
Choosing Python here is a deliberate break, so it needs justifying rather than assuming.

The owner has chosen "both — MQTT over a core library" for the API surface.

## Language decision: Python 3.11+

**`bleak` is the deciding factor.** It is the only mature, actively maintained,
genuinely cross-platform BLE client library, and it abstracts CoreBluetooth (macOS),
BlueZ/D-Bus (Linux), and WinRT (Windows) behind one API. That maps exactly onto our
dev-on-Mac → deploy-on-Pi plan (ADR-0001) with no rewrite.

The alternatives, and why not:

| | Why not |
|---|---|
| Node + `@abandonware/noble` | Fork-of-a-fork, native build pain, macOS support historically fragile. Choosing it for fleet consistency would trade a real reliability problem for a stylistic one. |
| Rust + `btleplug` | Excellent library, and a fine eventual rewrite target. Wrong for a phase whose main activity is *exploratory protocol work* — iteration speed matters more than throughput for a device that emits a packet every few seconds. |
| C++ on ESP32 | Rejected in [ADR-0001](ADR-0001-architecture.md). |
| Swift + CoreBluetooth | Ties the device layer to macOS, contradicting the Pi deployment path. |

Secondary reasons: BLE analysis tooling is overwhelmingly Python; `bleak-esphome` keeps the
Option C escape hatch open; `bleak-retry-connector` (4.6.3) already encodes the
transient-BLE-failure handling that everyone else learns the hard way.

**Cost accepted:** a fifth language in the fleet, and Python packaging. Mitigated by `uv`
(already installed), a locked `pyproject.toml`, `ruff` + `mypy --strict`, and keeping the
public surface a documented API rather than an importable-everywhere library.

Minimum **Python 3.11** — `bleak-esphome` and `aioesphomeapi` both require it, and we do not
want the Option C door closed by a version floor.

## API decision: core library, adapters on top

```
                 ┌───────────────┐   ┌───────────────┐
   Jarvis ──────▶│  HTTP + WS    │   │     MQTT      │◀────── Home Assistant
   scripts ─────▶│   adapter     │   │   adapter     │◀────── automations
                 └───────┬───────┘   └───────┬───────┘
                         └─────────┬─────────┘
                            api/  (stable local interface)
                            service/ (lifecycle, retries, lease)
                            device/  (BedJetDevice state + commands)
                            protocol/ (encode / decode — pure, no I/O)
                            transport/ (BLE; bleak | esphome-proxy | mock)
```

- **`protocol/` is pure and synchronous.** Bytes in, dataclasses out. No I/O, no async, no
  `bleak` import. This is what makes the whole protocol layer testable without hardware, which
  the brief requires.
- **HTTP + WS** is the primary adapter: REST for commands, WebSocket for live state push. It
  matches the chroxy/chroxy-daemon pattern the fleet already runs, so Jarvis integration is a
  known shape.
- **MQTT** is a peer adapter, not a layer beneath: it publishes retained state topics and
  subscribes to command topics, with optional Home Assistant MQTT-discovery payloads. HA gets
  first-class support without ever sitting in Jarvis's control path.
- Neither adapter may contain protocol knowledge. If an adapter needs to know a byte offset,
  the abstraction has leaked.

**Milestone ordering note:** adapters are Milestone 3. They are designed for now and built
later — the brief is explicit that we stop after discover → connect → read state.
