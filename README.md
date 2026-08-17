# bedjet-local

> Local BLE control of a BedJet 3 — no vendor cloud, no Home Assistant required — behind a
> device abstraction clean enough that the rest of the stack never learns what GATT is.

**Status: Milestone 3 — local API.** Five commands are ✅ verified on our own hardware, and
a daemon exposes them over HTTP+WebSocket and MQTT.

| | | |
|---|---|---|
| Off | `01 01` | ✅ RL-019 |
| Cool | `01 02` | ✅ RL-021 |
| Heat | `01 03` | ✅ RL-023 |
| Set target | `03 <2×°C>` | ✅ RL-022 |
| Set fan | `07 <step>` | ✅ RL-020 |

Verified means *observed on our device*, not "the write returned cleanly" — which it does
whether or not anything happens (RL-018). Every claim in this repository still carries its
provenance tag, and the rule below has not been relaxed.

```
    Jarvis · scripts · Home Assistant · automations
                        │
    integrations/       │  HTTP + WebSocket · MQTT           ── adapters, peers
              api/      │  stable local interface
              service/  │  lifecycle, retries, the lease
              device/   │  BedJetDevice state + commands
              protocol/ │  encode / decode (pure, no I/O)
              transport/│  BLE: bleak | ESPHome proxy | mock
                        │
                   physical BedJet
```

Dependencies point downward only. `protocol/` is pure — bytes in, dataclasses out, no I/O,
no async, no `bleak` — which is what lets the entire protocol layer be tested without
hardware. An adapter may not import below `api/`. `tests/unit/test_layering.py` enforces
both rather than trusting them.

## ⚠️ This drives a mains-powered heater

Read [`docs/SAFETY.md`](docs/SAFETY.md) before running anything that writes to the device.
The short version:

- Every write is verified by reading the device's own status back. A command that produces
  no observable change has not succeeded — it has merely not errored.
- Commands were unlocked one at a time, in increasing order of consequence, each after the
  previous was proven on hardware. **Turbo and extended heat remain locked in code**, and
  unlocking one is a deliberate edit to `service/commander.py` with a stated reason.
- Unknown writes are experiments requiring a written hypothesis, a known reversal, and a
  human present — not debugging.
- **There is no working physical remote on this unit**, and the BedJet permits one BLE
  client at a time. Anything that holds the link must be able to give it back: see the
  yield endpoint in [`docs/API.md`](docs/API.md). Unplugging is the escape hatch.

## Quick start

```bash
uv sync --extra dev
```

Then, with the laptop **in the same room as the BedJet** (BLE is ~10 m and hates walls):

```bash
uv run bedjet discover
```

### Establish which BedJet is yours first

`discover` will list every BedJet in radio range, and **they are indistinguishable**: same
`BEDJET_V3` name, same service UUID, no manufacturer data, and a host-local address. During
our own bring-up two units showed up and only one was ours. An RSSI-sorted list would have
picked the right one by luck.

Identify yours with a physical test only its owner can perform — unplug it, re-scan, and see
which address disappears — then record it:

```toml
# devices.local.toml  (gitignored; addresses are host-local and private)
[device.bedroom]
address = "..."
notes = "identified by power test, <date>"
```

`identify` and `watch` refuse any address that is not in that file. Connecting takes a
BedJet's single BLE slot, so connecting to a stranger's unit would knock them off their own
heater — that is not something to leave to sort order. Full account in
[RL-006](docs/research/RESEARCH-LOG.md).

```bash
uv run bedjet identify <address-from-discover>
```

```bash
uv run bedjet watch <address> --raw
```

Both of these are read-only. `watch` prints decoded state alongside raw hex so the two can
be compared against the unit's own display — which is the actual test, and the only thing
that promotes a protocol claim to ✅ verified.

**The vendor app must not be connected.** The BedJet permits exactly one BLE client at a
time; ours and theirs cannot coexist.

### Commanding it

Each of these writes to the device, asks for a typed confirmation first, and verifies the
result against the device's own status rather than assuming it. `--dry-run` rehearses
everything except the write.

```bash
uv run bedjet off <address>
uv run bedjet mode <address> cool
uv run bedjet temp <address> 72
uv run bedjet fan <address> 50
```

### Running it as a service

```bash
uv run bedjet serve <address>          # HTTP + WebSocket on 127.0.0.1:8787
uv run bedjet mqtt <address> --broker <host>
```

Unlike every other subcommand, these **hold the BLE link for as long as they run**, so the
vendor app cannot connect meanwhile. That is why the API has a yield endpoint, and why both
commands print it on startup:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/link/yield -d '{"seconds": 300}'
```

Full surface in [`docs/API.md`](docs/API.md); exposure and authentication decisions in
[ADR-0004](docs/decisions/ADR-0004-exposing-the-device.md).

## Testing

```bash
make test
```

Unit and integration tests run with no Bluetooth adapter and no BedJet, and CI never
attempts to reach hardware. Hardware-in-the-loop tests are marked, deselected by default,
and run only on explicit invocation:

```bash
make test-hardware
```

## Documentation

| | |
|---|---|
| [`docs/research/ecosystem-survey.md`](docs/research/ecosystem-survey.md) | What already exists: no vendor API, ESPHome's implementation, HA integrations, licences, and the contradictions between upstream sources |
| [`docs/API.md`](docs/API.md) | The local API: HTTP+WebSocket and MQTT, the state object, and what its fields actually mean |
| [`docs/protocol/PROTOCOL.md`](docs/protocol/PROTOCOL.md) | Field-by-field protocol reference, every row tagged ✅ verified / 📖 upstream / ❓ hypothesis |
| [`docs/research/RESEARCH-LOG.md`](docs/research/RESEARCH-LOG.md) | Append-only experiment journal — so no future session rediscovers the same byte twice |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Hard prohibitions, experiment protocol, bring-up order |
| [`docs/decisions/`](docs/decisions/) | ADRs: transport architecture, language and API shape, licensing, network exposure |
| [`AGENTS.md`](AGENTS.md) | Physical-device rules for agents working in this repo |

### The provenance rule

Protocol documentation carries three tags, and they are not interchangeable:

- ✅ **VERIFIED** — observed on *our* device, with a fixture, a date, and a firmware version.
- 📖 **UPSTREAM** — read from someone else's source. Plausible; not observed here.
- ❓ **HYPOTHESIS** — our inference, or an unresolved conflict between upstream sources.

An upstream claim is never promoted because two projects agreed or because the code ran
without crashing. It is promoted by a packet captured from our BedJet. Silently converting
someone else's behaviour into a claim about our hardware is the failure this whole scheme
exists to prevent.

## Attribution and licensing

MIT — see [`LICENSE`](LICENSE). **No third-party code is included.** The BedJet protocol is
undocumented by its manufacturer, and this project's understanding of it stands on prior
reverse-engineering by the ESPHome project and several Home Assistant integration authors.
ESPHome's codec is GPLv3 and `bedjet-re` is unlicensed, so both were read as *technical
evidence* and neither was copied or ported. Credits in [`NOTICE`](NOTICE); reasoning in
[ADR-0003](docs/decisions/ADR-0003-licensing.md).

Not affiliated with or endorsed by BedJet LLC.
