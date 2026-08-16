# bedjet-local

> Local BLE control of a BedJet 3 — no vendor cloud, no Home Assistant required — behind a
> device abstraction clean enough that the rest of the stack never learns what GATT is.

**Status: Milestone 1, pre-bring-up.** The protocol layer, transport abstraction, device
model, test harness, and read-only CLI exist. **We have not yet connected to the device**,
so every protocol claim in this repository is 📖 *upstream* or ❓ *hypothesis* — none is
✅ *verified*.

```
    Jarvis · scripts · Home Assistant · automations
                        │
              api/      │  stable local interface        ── Milestone 3
              service/  │  lifecycle, retries, lease
              device/   │  BedJetDevice state + commands
              protocol/ │  encode / decode (pure, no I/O)
              transport/│  BLE: bleak | ESPHome proxy | mock
                        │
                   physical BedJet
```

Dependencies point downward only. `protocol/` is pure — bytes in, dataclasses out, no I/O,
no async, no `bleak` — which is what lets the entire protocol layer be tested without
hardware. `tests/unit/test_layering.py` enforces it rather than trusting it.

## ⚠️ This drives a mains-powered heater

Read [`docs/SAFETY.md`](docs/SAFETY.md) before running anything that writes to the device.
The short version:

- **Milestone 1 is read-only by construction.** The CLI has no way to send a command, and
  a test asserts that no command encoder exists yet.
- Bring-up order is fixed: discover → connect → enumerate → subscribe → decode → *validate
  against the physical unit* → only then, the first write, which is **off**. Heat is last
  and attended.
- Unknown writes are experiments requiring a written hypothesis, a known reversal, and a
  human present — not debugging.
- The physical remote is not a BLE client and always works. The owner is never locked out.

## Quick start

```bash
uv sync --extra dev
```

Then, with the laptop **in the same room as the BedJet** (BLE is ~10 m and hates walls):

```bash
uv run bedjet discover
```

```bash
uv run bedjet identify <address-from-discover>
```

```bash
uv run bedjet watch <address> --raw
```

Every one of these is read-only. `watch` prints decoded state alongside raw hex so the two
can be compared against the unit's own display — which is the actual test, and the only
thing that promotes a protocol claim to ✅ verified.

**The vendor app must not be connected.** The BedJet permits exactly one BLE client at a
time; ours and theirs cannot coexist.

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
| [`docs/protocol/PROTOCOL.md`](docs/protocol/PROTOCOL.md) | Field-by-field protocol reference, every row tagged ✅ verified / 📖 upstream / ❓ hypothesis |
| [`docs/research/RESEARCH-LOG.md`](docs/research/RESEARCH-LOG.md) | Append-only experiment journal — so no future session rediscovers the same byte twice |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Hard prohibitions, experiment protocol, bring-up order |
| [`docs/decisions/`](docs/decisions/) | ADRs: transport architecture, language and API shape, licensing |
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
