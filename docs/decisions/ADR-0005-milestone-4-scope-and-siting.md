# ADR-0005 — What "Jarvis integration" means, and where the daemon lives

**Date:** 2026-08-17
**Status:** Accepted

## Context

Milestone 4 has been one line in `CLAUDE.md` since the repository was created: *"Jarvis
integration."* It was gated on the device layer being reliable, and
[#4](https://github.com/blamechris/bedjet-local/issues/4) discharged that gate — the
Milestone 3 connection stack has now run against the physical device and behaved as designed
(RL-024). So the milestone is unblocked, and the first honest act of starting it is
discovering that its scope was never written down.

Three things have changed underneath it since that line was typed, and each pulls the
milestone away from the obvious reading.

**The consumer does not exist.** The obvious reading is "no-it-all calls this API." But
`no-it-all` has no tool-calling of any kind: no `tools` in its Anthropic request, no
`tool_use`, no `tool_result`, nothing that could dispatch an intent to a heater. It has
speech capture and transcription — phase 0 of the voice pivot — and stops there. Writing an
adapter now would mean writing it against a caller that cannot call, and shaping our side to
an interface nobody has designed.

**The fleet acquired a topology.** The 2026-08-14 amendment to the voice pivot settled that
each machine runs its own daemon as sole authority, that one aggregator holds the roster, and
that the transport between personal machines and thin clients is a Tailscale-shaped mesh.
"Jarvis" is not one process to be linked against; it is a fleet with a routing layer.

**RL-025 introduced a constraint that ADR-0001 did not have.** That ADR chose the transport
architecture and deferred the Pi-versus-proxy question to an RSSI survey — a single axis.
macOS attributing CoreBluetooth to the responsible app bundle, and killing rather than
prompting when no usage description is present, is a second axis, and it bears specifically
on unattended operation, which is the entire premise of a daemon a voice assistant can reach
at 2am.

## Decision 1 — Milestone 4 delivers a contract, not a client

This repository's Milestone 4 deliverable is a **served surface an agent can call**: reachable,
authenticated, and described in terms an agent can act on. It is **not** code that lives inside
no-it-all, and it is not an `integrations/jarvis.py`.

The layering already says this — `integrations/` are adapters and peers of one another, and
`docs/API.md` has named Jarvis as an HTTP+WS consumer since Milestone 3. The substantive point
is that the HTTP+WS adapter **is** the integration, and the gap is not a missing adapter but a
missing description: the API is documented for a person reading a reference, and an agent
needs to know when *not* to call it, which of three failure modes it is looking at, and that
the thing it is holding is a lease on a device with no working remote.

Splitting it the other way — a Jarvis-shaped adapter here — would put knowledge of the caller
inside the callee, which is the direction this repository's one big rule spends all its effort
preventing.

## Decision 2 — No adapter is written until there is a caller to write it against

`integrations/jarvis.py` is deliberately not created by this milestone. `AGENTS.md` rule 6
forbids promoting a protocol row to VERIFIED on the strength of what upstream says rather
than what our device does; the same discipline applies to interfaces. An adapter written
against an imagined tool-calling surface would be an unverified claim about a caller, in code,
and it would be discovered to be wrong at exactly the moment the caller became real.

The dependency runs the other way and belongs in the other repository: no-it-all needs
tool-calling before any of this is reachable by voice. That is a no-it-all milestone, and this
ADR does not schedule it.

## Decision 3 — The daemon's home is the Mac provisionally, and the Pi question now has two axes

ADR-0001 deferred the Pi purchase pending a siting survey, on RSSI alone. That deferral stands,
but the decision it defers is no longer one-dimensional:

- **RSSI** — unchanged, still unmeasured, still the survey described in ADR-0001 and RL-010.
- **Permission** — a macOS host requires a signed wrapper bundle and a login session for the
  daemon to use Bluetooth unattended at all. A Pi has no such requirement; BlueZ has no TCC.

The Mac is the provisional home because the wrapper now exists (`deploy/macos/`), which makes
the permission axis a bounded, paid cost rather than an open risk. It is provisional because
the arrangement has real limits that are properties of the platform and not of our
implementation: nothing runs before login, nothing runs while the Mac sleeps, and the
Bluetooth grant is pinned to a content hash that any edit to the wrapper invalidates.

The tie-break, when the survey happens: if the Mac's position is adequate on RSSI, the
permission cost is already paid and it stays. If it is not, the Pi wins on both axes at once
and the purchase is no longer a close call.

## Decision 4 — Reachability is the tailnet with a token, never the LAN

ADR-0004 refuses a non-loopback bind without a token before the socket opens. That decision
plus the fleet's mesh-VPN transport settles this one: the daemon binds loopback by default and,
when something off-box must reach it, binds its **tailnet** address with a token — not a LAN
interface, not a tunnel.

This is the same move the fleet made in the 2026-08-14 amendment for the same reason, and it
matters more here than elsewhere: the thing behind this port switches on a heater in a bedroom.
A token on a home LAN is a token on a network segment shared with every unmaintained device in
the house.

## Decision 5 — Starting at login is opt-in, and is not part of setting this up

The wrapper's LaunchAgent is documented but deliberately left uninstalled. The BedJet permits
one BLE client at a time and this unit has no working physical remote, so a daemon that starts
at every login takes the owner's heater away at every login until they find and stop a process.

ADR-0004 decision 3 made the lease part of the API for precisely this reason. Automatic start
is a real requirement for a voice assistant and will eventually be turned on — but it is a
consequence to be accepted deliberately, at the point where something actually needs the daemon
up unprompted, not a step in a setup guide that gets followed without being read.

## Consequences

- Milestone 4 is now buildable in this repository without waiting on another one: what remains
  here is reachability, unattended operation, and an agent-facing description of the contract.
- The voice path is blocked on no-it-all tool-calling, and that blockage is now recorded rather
  than discovered later by someone writing an adapter.
- ADR-0001's deferred Pi decision keeps its deferral but gains a second input, so the eventual
  siting survey answers a different, better-posed question than it was going to.
- Anyone reconfiguring the daemon on macOS pays attention to the wrapper's content hash, because
  the alternative is a silent loss of Bluetooth permission at the next reboot.

## Not decided here

- **The shape of the agent-facing description.** Whether the contract is expressed as an MCP
  server, a tool-schema document, or a prompt fragment depends on what no-it-all's tool-calling
  turns out to look like, and deciding it now would repeat the mistake Decision 2 avoids.
- **Intent vocabulary.** What someone actually says to a heater — and what "warm the bed" means
  in modes, targets and fan steps — is a product question that wants the device's own per-mode
  behaviour (RL-023) in front of it.
- **Mac asleep.** The fleet roadmap holds this open for phase 2/3 and nothing here closes it.
- **Multi-device.** Still one session, one device, as ADR-0004 said.
