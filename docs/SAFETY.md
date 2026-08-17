# Safety boundaries

The BedJet is a **mains-powered forced-air heater** that blows into a bed a person sleeps in.
It has thermal protection and operating limits designed by people with more thermal-engineering
context than we have. This document defines what we do and do not do, and it binds agents and
humans equally.

## Hard prohibitions — Phase 1 through 4

Not "ask first". **Do not do these, and do not propose them.**

- Do not open the unit, or remove any panel or cover.
- Do not modify, probe, or measure mains-voltage circuitry.
- Do not touch the PCB — no soldering, no test points, no logic-analyser clips.
- Do not modify, flash, dump, or downgrade firmware.
- Do not attempt to bypass or defeat thermal protection, temperature limits, runtime limits,
  or any hardware safety system.
- Do not send undocumented or randomly-chosen writes "to see what happens".
- Do not leave the device running unattended under software control until Milestone 2 is
  complete and its behaviour has been observed by a human across a full cycle.

If BLE gives us complete functional control — and the survey says it should — that is
**success**, not a consolation prize. The brief is explicit: do not force PCB-level work into
this project. Hardware depth is a later project with the right tools and the right target.

## Unknown writes are experiments, not debugging

Any write whose effect we cannot state in advance is an **experiment** and requires, before it
is sent:

1. **A narrow scope** — one command, one operand, one observation.
2. **A written hypothesis** — a RESEARCH-LOG entry opened *before* the write, stating what we
   expect and why.
3. **A justification** — what question it answers that reading cannot.
4. **A reversal** — the exact command that restores the prior state, known and ready.
5. **A human present**, in the room, with the unit visible and **the plug located**. (This
   said "with the physical remote in reach" until RL-011 — there is no working remote, so the
   plug is the escape hatch that always works.)

"I'll just try it and see" is the failure mode this section exists to prevent. Reading is free;
writing is not.

## Safe bring-up order

Strictly sequential. Do not skip forward because an earlier step "obviously works".

| # | Step | Writes? |
|---|---|---|
| 1 | Discover the device by BLE advertisement | no |
| 2 | Identify: name, address, advertised services, RSSI | no |
| 3 | Inspect advertising data (manufacturer data, service data) | no |
| 4 | Connect | no |
| 5 | Enumerate GATT services | no |
| 6 | Enumerate characteristics and their properties | no |
| 7 | Subscribe to the status characteristic | no |
| 8 | Observe raw status packets, unit **off** then **on** (set via the vendor app, then release the link) | no |
| 9 | Decode state | no |
| 10 | **Validate decoded state against a known state set in the vendor app** | no |
| — | ⬆ **Milestone 1 ends here. Everything above is read-only.** | |
| 11 | ✅ **DONE** — first write ever: **`01 01` — OFF**, via `bedjet off` (RL-019) | ✅ first write |
| 12 | ~~Fan-only mode~~ — **no fan-only mode exists** in this device's status enum; skipped | — |
| 13 | ✅ **DONE** — fan speed changes (RL-020) | ✅ |
| 14 | ✅ **DONE** — Cool mode (RL-021). Dry available, untested | ✅ |
| 15 | Heat mode, low target, short timer, **attended** — requires deliberately unlocking `THERMALLY_SAFE_MODES` | ✅ |
| 16 | Timer and presets | ✅ |

**Heat is last, and it is attended.** Lower-risk commands validate the entire stack — encoding,
transport, state reconciliation, error handling — before we ask the device to make heat. If the
stack is broken, we find out with a fan running, not a heater.

## Every command is verified, never assumed

There is **no acknowledgement in this protocol.** A GATT write that returns cleanly proves only
that the radio accepted the bytes — not that the device understood them, and not that it acted.

And the command table is unverified upstream guesswork from a source already caught conflating
two enums (RL-016). So a command is not "write and hope":

```
read state  →  check the write would be observable  →  write  →  read state back
            →  assert the state actually changed as intended
```

If the state does not change, that is **`CommandUnverified`, not success**, and the operator is
told to use the vendor app or unplug. Commands are verified **one at a time**, starting with
OFF, and `tests/unit/test_layering.py` enforces both that the write path is a single auditable
module and that OFF is the only command it can construct.

Refusing to send is also a feature: `bedjet off` **refuses when the unit is already off**,
because an unobservable write teaches us nothing while still being a write to a heater.

## Operating limits

**The device is the authority on its own limits — not a spec sheet, and not us.**

This section used to say "clamp to the documented range (≈66–104 °F / 19–40 °C)". RL-013
proved that wrong in both directions: the permitted range **moves with the mode** (standby
10–40 °C, cool 19–26 °C, heat 22.5–40 °C, dry 24–31 °C), and **turbo targets 43 °C / 109.4 °F —
above the 104 °F the manufacturer's own marketing calls the maximum.** A hardcoded table would
have refused a temperature the device itself offers, and would have been the wrong shape of
wrong: confidently applying a safety limit derived from a document rather than from the device.

So:

- Bounds come from the **current status packet** (bytes 13–14 for temperature, 11–12 for
  runtime). `encode.set_temperature` and `encode.set_timer` **require** them and refuse without.
- Out-of-range requests **raise, never clamp**. Silently heating to a different temperature
  than the caller asked for is the wrong failure mode for a heater.
- Never send a target the device does not currently report as permitted.
- If the device reports a value outside its own stated range, **surface it as an anomaly**;
  never silently clamp a *decode*. Clamping inputs is safety; clamping observations is lying.
  (Dry does exactly this — see RL-015 — and the anomaly is left visible rather than tidied away.)

## Physical-safety escape hatches

> ⚠️ **Corrected 2026-08-16. This section previously claimed "the physical remote is not a BLE
> client, so the owner can always override us at the device." That is false for this
> installation: we have no working physical remote.** The claim was written from the general
> case and never checked against our actual setup — exactly the upstream-assumed-as-observed
> error this project's provenance rules exist to prevent, applied to a safety claim rather
> than a protocol one.

The override paths that actually exist here, in order of preference:

1. **Unplug it at the wall.** Always available, always correct, needs no software, no radio,
   and no working link. **This is the primary escape hatch, not the last resort** — treat it
   that way when planning any experiment.
2. **The vendor app** (BedJet 3 Smart Remote, Android). Real, but **not** an independent path:
   the BedJet permits one BLE client at a time, so the app can only take over *after our client
   releases the link*. If our software is connected — or wedged while connected — the app
   cannot get in until we disconnect or the connection drops.
3. The unit's own thermal protection remains in force; we do not go near it.

**Consequence for Milestone 2.** With no remote, our software and the app are the only control
paths, and they are mutually exclusive. Every control experiment must therefore:

- run **time-boxed**, so the link is released automatically rather than depending on a clean
  exit (`bedjet watch --seconds N`);
- leave the device in a **safe state before disconnecting** — never end a session with heat
  running and the link dropped;
- treat "unplug it" as the answer to anything unexpected, rather than "let me reconnect and
  fix it", because reconnecting may be exactly what is failing.

The reason this matters more than it sounds: a wedged BLE connection would otherwise lock the
owner out of the only remaining control path for their own heater.

## For agents specifically

See [`AGENTS.md`](../AGENTS.md). The short version: hardware commands have consequences that
`git revert` does not undo, CI never touches the device, and you may not promote an upstream
claim to an observation about our hardware.

## Why heat was unlocked, and what the risk actually was

Recorded because "we were careful, then we stopped being careful" is not a good look in a
safety document, and that is not what happened.

**The risk was never that the device makes heat.** That is its job. It has its own thermostat,
its own hardware thermal protection, and its own runtime limits, none of which this project
touches or can touch over BLE. The manufacturer designed a machine to blow warm air into a bed
all night; software asking it to do so is not the dangerous part.

**The risk was that our stack was unverified.** If the decoder were wrong, we could not tell
what the device was doing — and RL-017 proved that concern justified in the most direct way
available, by reporting a running heater as switched off. Commanding a machine you cannot
reliably observe is the actual hazard, and it is a property of the software, not the appliance.

That concern is now discharged, specifically:

| Precondition | Status |
|---|---|
| Commands verified end-to-end against observed state | ✅ four of them |
| State gated on a checksum, so corrupt packets cannot masquerade as truth | ✅ RL-017 |
| **A proven software stop** | ✅ OFF verified and reproduced |
| Targets bounded by the range the device reports for its current mode | ✅ RL-013 |
| Human present, plug located | ✅ every write is gated on it |

The third row is the one that matters most. **Testing heat without a verified OFF means
creating a state you cannot exit in software.** We now have one, tested twice from different
starting states. That is what changed — not the appetite for risk.

**Still locked:** turbo (a fixed 43 °C / 109 °F, the device's most aggressive setting, and
nothing needs it) and extended heat (status `0x03` has never been observed, so its result
could not be verified even if the command worked — a command whose success is indistinguishable
from its failure should not be sendable).
