# The local API

Two adapters over one stable interface. They are peers: run either, or both, or neither and
use the library directly.

```
   Jarvis · scripts ──▶ HTTP + WebSocket ──┐
                                           ├──▶ api/  ──▶ service/ ──▶ device/ ──▶ protocol/ ──▶ transport/
   Home Assistant ────▶ MQTT ──────────────┘
```

Neither adapter contains protocol knowledge. Everything below is strings, numbers and
booleans; nothing here exposes a byte offset, a packet field or a device enum, and
`tests/unit/test_layering.py` fails the build if an adapter ever imports downward.

## Before anything else: this holds the device's only BLE slot

The BedJet permits **one** BLE client at a time, and this unit has **no working physical
remote**. While the daemon runs, the vendor app cannot connect. So the yield endpoint is not
an extra — it is how the owner takes their own heater back:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/link/yield -d '{"seconds": 300}'
```

```bash
mosquitto_pub -t bedjet/bedjet/set/link_yield -m 300
```

Reasoning in [ADR-0004](decisions/ADR-0004-exposing-the-device.md).

---

## HTTP + WebSocket

```bash
uv sync --extra http
uv run bedjet serve <address>
```

Binds `127.0.0.1:8787`. Binding anywhere else **requires** a token and is otherwise refused
before the socket opens:

```bash
BEDJET_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') \
  uv run bedjet serve <address> --host 0.0.0.0
```

The token goes in `Authorization: Bearer <token>` and is **not** accepted as a query
parameter. Requests carrying an `Origin` header are refused, and the `Host` header must name
something recognised — see ADR-0004 for why.

### Reading

| | |
|---|---|
| `GET /healthz` | Liveness. The one route that answers without a credential, and it says nothing about the device. |
| `GET /api/v1/state` | The current snapshot. |
| `GET /api/v1/capabilities` | What this build permits: modes, fan steps, wire granularity. |
| `GET /api/v1/ws` | WebSocket. Pushes `{"type": "state", "state": {…}}` on connect and on every change. One-way. |

### Commanding

| | |
|---|---|
| `POST /api/v1/command/off` | `{"dry_run": false}` |
| `POST /api/v1/command/mode` | `{"mode": "cool"}` — `off`, `cool`, `heat`, `dry` |
| `POST /api/v1/command/temperature` | `{"celsius": 22}` or `{"fahrenheit": 72}` — exactly one |
| `POST /api/v1/command/fan` | `{"percent": 50}` — 5-100 in 5% steps |
| `POST /api/v1/link/yield` | `{"seconds": 300}` |
| `POST /api/v1/link/resume` | End a yield early |

Every command accepts `"dry_run": true`, which rehearses the whole path — preconditions,
encoding, bounds — and sends nothing.

### The state object

```json
{
  "link": "connected",
  "available": true,
  "stale": false,
  "reading_age_s": 0.4,
  "power": "on",
  "mode": "cool",
  "target_temp_c": 25.0, "target_temp_f": 77.0,
  "actual_temp_c": 22.0, "actual_temp_f": 71.6,
  "ambient_temp_c": 21.0, "ambient_temp_f": 69.8,
  "fan_percent": 50, "fan_is_stale": false,
  "time_remaining_s": 1800,
  "min_target_c": 19.0, "max_target_c": 26.0,
  "max_runtime_s": 43200,
  "anomalies": []
}
```

Four fields carry more meaning than their names suggest, and a consumer that ignores them
will be confidently wrong:

- **`available` is about the radio; `power` is about the heater.** They are independent and
  neither may be inferred from the other. A dropped link tells you nothing about whether the
  unit is running, so the last reading is kept — with its age — rather than reinterpreted.
- **`stale` means "we have not heard from it recently". It never means "it is off".** The
  BedJet is near-silent in standby, so quiet is the normal state of a switched-off heater.
- **`fan_is_stale` means the percentage is a memory, not airflow.** The fan byte keeps its
  last-set value in standby (RL-013), so an idle unit will report the speed of the session
  that just ended.
- **`min_target_c` / `max_target_c` are the bounds for the *current mode*, live.** They move
  when the mode moves, and turbo legitimately reports 43 °C — above the 104 °F every public
  source calls the device maximum (RL-013). Do not cache them; do not hardcode a range.

### Command results

```json
{
  "ok": true,
  "changed": true,
  "detail": "mode -> off: verified against the device's own status",
  "sent": "01 01",
  "dry_run": false,
  "before": { }, "after": { }
}
```

`sent` is the audit trail for a physical action: the exact bytes that went on the wire, or
`null` when nothing did. `ok` and `changed` are separate on purpose — asking an already-off
heater to switch off is `ok: true, changed: false, sent: null`, a satisfied request in which
**nothing was verified because nothing was written**.

### Status codes

| | | |
|---|---|---|
| 200 | success, including a satisfied request | — |
| 400 | `invalid_request` | malformed body; nothing was sent |
| 401 | `unauthorized` | token missing or wrong |
| 403 | `forbidden` | `Origin` present, or an unrecognised `Host` |
| 409 | `refused` | **nothing was sent.** Retrying unchanged will refuse again |
| 502 | `unverified` | **bytes went out and the device did not visibly obey** |
| 503 | `unavailable` | the link is down or yielded; nothing was sent |

**409 and 502 must never be handled the same way.** 409 means the device is untouched. 502
means we wrote to a heater and cannot tell you what happened — the write may have taken
effect with the confirmation lost, because this protocol has no acknowledgement (RL-018).
Blind retry is reasonable for one and not the other.

---

## MQTT

```bash
uv sync --extra mqtt
BEDJET_MQTT_PASSWORD=… uv run bedjet mqtt <address> --broker <host> --username <user>
```

Home Assistant discovery is published by default: a climate entity, a `number` for fan
speed, and an ambient temperature sensor.

### Topics

| | | |
|---|---|---|
| `bedjet/<id>/state` | retained | the state object above, plus `ha_mode` |
| `bedjet/<id>/availability` | retained | `online` / `offline`, and the LWT |
| `bedjet/<id>/result` | not retained | what happened to each command |
| `bedjet/<id>/set/mode` | | `off`, `cool`, `heat`, `dry` |
| `bedjet/<id>/set/temperature` | | degrees Celsius |
| `bedjet/<id>/set/fan` | | percent, 5-100 |
| `bedjet/<id>/set/link_yield` | | seconds |
| `bedjet/<id>/set/link_resume` | | any payload |

**Watch `.../result`.** MQTT gives a publisher no reply, so this topic is the only way an
`unverified` outcome reaches you — and unverified means the heater may well have obeyed. It
is deliberately *not* retained: a result describes one moment, and serving a months-old
refusal to a fresh subscriber as though it had just happened would be its own small lie.

**`availability` requires a live link *and* a fresh reading.** Because the state topic is
retained, a payload we cannot refresh would otherwise be handed to every future subscriber
as if it were current.

### `ha_mode`, and what it costs

Home Assistant's climate model has no `turbo` and no `unknown`. The state payload therefore
carries both views: `mode` is what the device said (`"turbo"`), and `ha_mode` is what a
climate entity can render (`"heat"`). The lossy mapping lives in the adapter, computed from
the API's own strings — nothing below `api/` knows a climate entity exists.

For the same reason, **no static temperature range is published in the discovery payload**.
The range moves with the mode, so any fixed `min_temp`/`max_temp` would be wrong in at least
one of them. The live fields on the state topic are the authority.

---

## Using the library directly

```python
from bedjet_local.api import BedJetAPI
from bedjet_local.service.session import DeviceSession
from bedjet_local.transport.ble import BleakTransport

session = DeviceSession(BleakTransport(), address)
await session.start()
api = BedJetAPI(session)

print(api.snapshot().to_dict())
await api.set_mode("cool")
await session.stop()
```

`api.subscribe(listener)` returns the function that unsubscribes it. Listeners are called
from the BLE notification path, so they must not block — and one that raises is logged and
stepped over rather than being allowed to take the device link down.
