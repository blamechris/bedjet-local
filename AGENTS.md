# Agent instructions — bedjet-local

This repository controls a **physical mains-powered heating device**. The usual assumption that
a mistake costs a `git revert` does not hold here. Read [`docs/SAFETY.md`](docs/SAFETY.md)
before any task that touches hardware.

## The six rules

### 1. Hardware commands have consequences

A BLE write changes the state of a real appliance in a real bed. There is no undo, no dry-run,
and no staging environment. Before writing code that sends a command, know what the command
does; before *running* it, know how to reverse it.

### 2. Unknown writes are experiments, not debugging

Any write whose effect you cannot state in advance requires a RESEARCH-LOG entry opened
**before** the write, a stated hypothesis, a known reversal, and a human present. You may not
send a speculative byte to narrow down a hypothesis. Reading is free; writing is not.
Full conditions: `docs/SAFETY.md`.

### 3. Hardware tests require explicit invocation

Tests that touch the device live in `tests/hardware/`, are marked `@pytest.mark.hardware`, and
are **deselected by default** by `pyproject.toml`. They run only via `make test-hardware`
(equivalently `pytest -m hardware`), only with the device powered and a human present.

Never add a hardware test to the default selection. Never make a unit or integration test open
a BLE adapter.

### 4. Normal CI uses mocks and fixtures — never hardware

CI has no Bluetooth adapter and no BedJet, and must never attempt to reach one. `tests/unit/`
and `tests/integration/` must pass on a machine that has never seen the device. If a change
makes them require hardware, the change is wrong, not the test setup.

`protocol/` is pure: bytes in, dataclasses out. No I/O, no async, no `bleak` import. This is
what keeps the protocol layer fully testable without the device, and it is enforced by
`tests/unit/test_layering.py` — do not weaken that test to make an import work.

### 5. Protocol discoveries must be documented

If you learn something about the device, it goes in `docs/research/RESEARCH-LOG.md` (append-only,
fixed format) and the affected row in `docs/protocol/PROTOCOL.md` is updated **before the
session ends**. A discovery that lives only in a session transcript will be rediscovered by
someone else at full cost. That is the specific waste this repo is built to avoid.

Captured packets become fixtures in `tests/fixtures/` with provenance recorded in
`tests/fixtures/PROVENANCE.md` — date, firmware version, device state, how it was captured.

### 6. Upstream assumptions are not observations about our device

`docs/protocol/PROTOCOL.md` has three provenance tags — ✅ `VERIFIED` (observed here),
📖 `UPSTREAM`, ❓ `HYPOTHESIS`. **You may not promote a row to `VERIFIED` without a fixture from
our device.** Not because two upstream projects agree. Not because the code ran without an
exception. Not because it "clearly must be right".

This applies to prose too: write "ESPHome documents the packet as partial-then-read", not "the
BedJet sends a partial packet", until we have watched ours do it.

## Licensing constraint (non-negotiable)

ESPHome's BedJet codec is **GPLv3**; `markus1189/bedjet-re` is **unlicensed**. This repo is
**MIT**. Use upstream as *evidence* — a source of hypotheses to verify — never as *source*. Do
not copy, transliterate, or port upstream code into `src/`. Facts (a UUID, a byte offset) are
fine; a structure-for-structure translation of someone's parser is not. See
[ADR-0003](docs/decisions/ADR-0003-licensing.md).

## Layering

```
transport/  BLE discovery, connection lifecycle, GATT ops     (bleak lives here, and only here)
protocol/   packet definitions, encode, decode, constants     (pure; no I/O, no async)
device/     BedJetDevice state model, commands, capabilities  (safety clamps live here)
service/    lifecycle, retries, reconnection, connection lease
api/        stable local interface                            (strings out; the translation seam)
integrations/ HTTP+WS, MQTT, Home Assistant, Jarvis           (adapters, peers of one another)
```

Dependencies point downward only. An adapter that needs to know a byte offset means the
abstraction has leaked — fix the abstraction, not the adapter.

**`integrations/` may import `api/` and nothing else in this package.** Not `protocol/`, not
`device/`, not `service/`, not `transport/`. "No protocol knowledge in an adapter" is easy to
agree with and easy to violate with one convenient import that breaks nothing, so
`tests/unit/test_layering.py` checks it. If an adapter needs a field the API does not expose,
add it to `api/` — that is the prescribed fix, not a shortcut around it.

## Fleet conventions

Global conventions in `~/.claude/CLAUDE.md` apply: status blocks, session boundaries, the
follow-on protocol, explicit-path staging, worktree-by-default, no agent attribution in commits
or PRs. Repo specifics in [`CLAUDE.md`](CLAUDE.md).
