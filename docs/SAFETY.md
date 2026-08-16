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
5. **A human present**, with the physical remote in reach and the unit visible.

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
| 8 | Observe raw status packets, unit **off** then **on** via the physical remote | no |
| 9 | Decode state | no |
| 10 | **Validate decoded state against the physical unit and the vendor app** | no |
| — | ⬆ **Milestone 1 ends here. Everything above is read-only.** | |
| 11 | First write ever: **`01 01` — OFF** | ✅ first write |
| 12 | Fan-only mode at low speed | ✅ |
| 13 | Fan speed changes | ✅ |
| 14 | Cool mode | ✅ |
| 15 | Heat mode, low target, short timer, **attended** | ✅ |
| 16 | Timer and presets | ✅ |

**Heat is last, and it is attended.** Lower-risk commands validate the entire stack — encoding,
transport, state reconciliation, error handling — before we ask the device to make heat. If the
stack is broken, we find out with a fan running, not a heater.

## Operating limits

Use the manufacturer's own limits as hard bounds in software:

- Target temperature clamped to the device's documented range (≈66–104 °F / 19–40 °C), and the
  clamp is enforced in `device/`, above the protocol layer, so no adapter can route around it.
- Never send a target above what the physical remote itself permits.
- Runtime/timer values stay within the device's documented maxima.
- If the device reports a value outside its own documented range, **surface it as an anomaly**;
  never silently clamp a *decode*. Clamping inputs is safety; clamping observations is lying.

## Physical-safety escape hatches

- The **physical remote is not a BLE client** and is unaffected by anything we do over
  Bluetooth. The owner can always override us at the device.
- The unit's own thermal protection remains in force; we do not go near it.
- Unplugging is always available and always correct if anything is behaving unexpectedly.

## For agents specifically

See [`AGENTS.md`](../AGENTS.md). The short version: hardware commands have consequences that
`git revert` does not undo, CI never touches the device, and you may not promote an upstream
claim to an observation about our hardware.
