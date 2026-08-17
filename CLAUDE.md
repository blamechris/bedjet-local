# Claude Development Notes — bedjet-local

## Project overview

Local control of a **BedJet 3** over BLE, with no vendor cloud, behind a device abstraction
clean enough that Jarvis never learns what GATT is. This is the fleet's first physical-device
integration and is intended to become the pattern for the next one.

**Read [`AGENTS.md`](AGENTS.md) before any hardware-touching task**, and
[`docs/SAFETY.md`](docs/SAFETY.md) before any task that writes to the device. This repo drives
a mains-powered heater; the usual "revert it" safety net does not exist.

## Architecture (the one big rule)

**Dependencies point downward, and `protocol/` is pure.**

```
transport/ → protocol/ → device/ → service/ → api/ → integrations/
```

`protocol/` takes bytes and returns dataclasses. No I/O, no async, no `bleak`. That is what
makes the entire protocol layer testable with zero hardware, which is the difference between a
project we can iterate on and one that needs a bedroom visit per change.
`tests/unit/test_layering.py` enforces it.

Rationale: [ADR-0001](docs/decisions/ADR-0001-architecture.md) (transport),
[ADR-0002](docs/decisions/ADR-0002-language-and-api.md) (Python + adapters),
[ADR-0003](docs/decisions/ADR-0003-licensing.md) (MIT; upstream as evidence, not source).

## Build & test

```bash
uv sync                 # install
make test               # unit + integration — the CI merge gate, no hardware
make test-unit
make test-integration
make test-hardware      # ⚠️ real device, human present, never in CI
make lint               # ruff + mypy --strict
```

`make test` must pass on a machine that has never seen a BedJet. If a change breaks that, the
change is wrong.

## Documentation is load-bearing here

Three documents are part of the deliverable, not commentary on it:

- `docs/protocol/PROTOCOL.md` — every field carries ✅ `VERIFIED` / 📖 `UPSTREAM` /
  ❓ `HYPOTHESIS`. **Never promote a row to `VERIFIED` without a fixture captured from our
  device.**
- `docs/research/RESEARCH-LOG.md` — append-only, fixed format. Every experiment, before and
  after. A discovery that lives only in a transcript gets rediscovered at full cost.
- `tests/fixtures/PROVENANCE.md` — where each captured packet came from, and against which
  firmware.

## Milestones

1. ✅ **Discover → connect → read state.** Read-only.
2. ✅ **Safe control**: off, fan, cool, target, then heat (attended, last). Five commands
   verified on hardware; turbo and extended heat deliberately still locked.
3. ✅ **Stable local API**: `api/` plus HTTP+WS and MQTT adapters over the core library.
   Exposure decisions in [ADR-0004](docs/decisions/ADR-0004-exposing-the-device.md).
   ← *we are here*
4. Jarvis integration.

Do not race ahead. Milestone 4 does not begin until the device layer is reliable —
**and one thing is outstanding**: the reconnection path added for Milestone 3 has never run
against the physical device, because it cannot be exercised without one. Give it an attended
run before anything is left running unattended.

## Device facts to remember

- **One BLE client at a time.** Our daemon holding the link locks the owner out of the vendor
  app. The service layer owns a connection lease with an explicit yield.
- **Silence is ambiguous** — the device barely notifies while off. `available` (link) and
  `power` (device state) are independent and must never be inferred from each other.
- **The status packet is larger than a notification.** Expect partial-then-read.
- **The physical remote always wins** and is not a BLE client — the owner is never locked out
  of their own bed.

## Commit format

`type(scope): description` — types: feat, fix, refactor, docs, chore, test, research.
`research(protocol): ...` is for RESEARCH-LOG and PROTOCOL.md updates.

## Session lifecycle

Global conventions (`~/.claude/CLAUDE.md`) govern status blocks, session boundaries, seeds, and
the follow-on protocol. Handoff seeds go to `$CLAUDE_HANDOFF_DIR/NEXT-bedjet-local.md` via
`session-seed.py write` — never inside this working tree.
