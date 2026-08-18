"""The contract, described for an agent instead of a person (ADR-0005 decision 1).

``docs/API.md`` tells a person how to call this service. An agent reading a reference
mid-task needs different things, in a different order: when **not** to call, which of three
failure modes it is looking at and what each one permits, and that holding this connection
means holding the only BLE slot of a heater whose physical remote does not work. That
description is part of the served surface — ``GET /api/v1/contract`` — because the consumer
it exists for is exactly the one that cannot read this repository.

This module is deliberately not an MCP server, a tool schema, or a prompt fragment.
ADR-0005 leaves that shape undecided until no-it-all grows tool-calling; whichever it turns
out to be gets *generated from* this document rather than written beside it, so the
semantics have one authoritative statement instead of one per format.

Transport specifics — routes, status codes, authentication — are the adapter's to add on
the way out. This module states only the semantics that survive any transport, the same
split that keeps :class:`Refused` / :class:`Unavailable` / :class:`Unverified` here and
409/503/502 in ``integrations/http_ws.py``.

Nothing enumerable is written twice: modes come from the commander's allowlist via
:func:`available_modes`, granularities from the constants the encoder uses. The route
parity test in ``tests/integration/test_http_ws.py`` keeps the operation list and the
served routes from drifting apart, and ``tests/unit/test_contract.py`` pins the prose
claims that have a testable counterpart.
"""

from __future__ import annotations

from typing import Any

from .models import (
    FAN_PERCENT_MAX,
    FAN_PERCENT_MIN,
    FAN_PERCENT_STEP,
    TEMPERATURE_STEP_C,
)
from .service import available_modes

#: Bumped when the *shape* of this document changes — a consumer that parses it should
#: check this, and prose edits alone do not move it.
CONTRACT_VERSION = 1


def _dry_run_param() -> dict[str, Any]:
    """A fresh dict per use: the document is handed to adapters and callers, and shared
    mutable state inside it would let one consumer's edit leak into the next request."""
    return {
        "name": "dry_run",
        "type": "boolean",
        "required": False,
        "constraints": (
            "when true, the whole path is rehearsed — preconditions, encoding, bounds — "
            "and nothing is sent to the device"
        ),
    }


def agent_contract() -> dict[str, Any]:
    """The agent-facing description of this service, as JSON-safe primitives.

    Static per build: everything here is either prose doctrine or derived from the same
    constants the command path uses. Live facts — the current state, the device's per-mode
    bounds — are deliberately absent; they belong to ``get_state``, and a contract that
    embedded them would teach a consumer to cache exactly the values it must not cache.
    """
    return {
        "service": "bedjet-local",
        "contract_version": CONTRACT_VERSION,
        "device": (
            "A BedJet 3: a mains-powered forced-air heater and cooler attached to a bed. "
            "Commands move a real appliance next to a sleeping person."
        ),
        "read_first": [
            (
                "Commands cause physical action and there is no undo. When intent is "
                "uncertain, turn_off is the safe command, and every command accepts "
                "dry_run, which rehearses everything and sends nothing."
            ),
            (
                "This service holds the device's only BLE client slot, and this unit has "
                "no working physical remote. While the service is connected, the owner's "
                "vendor app cannot reach the heater at all. If a human says they cannot "
                "control the device, call yield_link — do not troubleshoot their app."
            ),
            (
                "The protocol has no acknowledgement. A command succeeds only when the "
                "device's own subsequent status visibly reflects it; 'unverified' means "
                "the write went out — or died in flight — and that confirmation never "
                "came, which may mean the device obeyed and the report was lost. Never "
                "blind-retry an unverified command. The three failure modes below "
                "permit different responses; flattening them into 'it failed' will "
                "eventually retry a write against a heater that already accepted it."
            ),
            (
                "The link and the heater are independent facts. 'available' is about the "
                "radio; 'power' is about the appliance; a stale reading is not evidence "
                "the unit is off. See state_semantics before interpreting any snapshot."
            ),
            (
                "Temperature bounds are per-mode, live, and owned by the device. Read "
                "them from the state at decision time; never cache them, never hardcode "
                "a range."
            ),
        ],
        "failure_modes": {
            "refused": {
                "meaning": (
                    "The request was rejected before anything was sent. The device is untouched."
                ),
                "device_was_written_to": False,
                "response": (
                    "Retrying the identical request will be refused again. Read the "
                    "detail — it says what to change, and a refusal for a locked mode is "
                    "a safety decision to respect, not an obstacle to work around."
                ),
            },
            "unavailable": {
                "meaning": (
                    "The BLE link is not up — the service is reconnecting, or the link "
                    "was deliberately yielded to the owner — so nothing was sent."
                ),
                "device_was_written_to": False,
                "response": (
                    "Safe to retry later. If the detail mentions a yield, the owner has "
                    "the device: wait out the stated time rather than resuming, unless "
                    "the owner asked for the link back."
                ),
            },
            "unverified": {
                "meaning": (
                    "The write may have reached the device and may not have: either the "
                    "bytes went out and the device did not visibly obey, or the link "
                    "failed during the write itself. The command may have taken effect "
                    "with the confirmation lost, or been discarded entirely; there is no "
                    "way to tell from here."
                ),
                "device_was_written_to": True,
                "response": (
                    "Never blind-retry. Read the state, decide from what the device now "
                    "reports, and involve a human if it is still ambiguous. This outcome "
                    "exists so that 'we told a heater something and cannot tell you what "
                    "happened' is never dressed up as an ordinary error."
                ),
            },
        },
        "state_semantics": {
            "available": (
                "Whether the BLE link is up right now. Says nothing about whether the "
                "heater is running."
            ),
            "power": (
                "'on' | 'off' | 'unknown' — what the heater last reported about itself. "
                "'unknown' is a real answer to act on, not an error to retry."
            ),
            "stale": (
                "The reading is old enough to distrust, with its age in reading_age_s. "
                "Stale never means off: the device is near-silent in standby, so quiet "
                "is the normal state of a switched-off heater."
            ),
            "link": (
                "'connected' | 'connecting' | 'lost' | 'yielded' | 'stopped'. 'yielded' "
                "means the link was deliberately released so the owner could use the "
                "vendor app — it is not a fault; do not alarm on it and do not resume "
                "it uninvited."
            ),
            "fan_is_stale": (
                "When true, fan_percent is a memory of the last session, not airflow — "
                "an idle unit reports the speed of the session that just ended."
            ),
            "min_target_c / max_target_c": (
                "The permitted target range for the current mode, live from the device. "
                "It moves when the mode moves. These fields are the final gate a "
                "set_temperature call is checked against; while they are null the "
                "bounds are unknown and set_temperature is refused."
            ),
            "null fields": (
                "Every reading-derived field is null before the device has first "
                "reported. Null is 'unknown', never 'zero' and never 'off'."
            ),
            "anomalies": (
                "Decode oddities, surfaced rather than swallowed. Non-empty is worth "
                "reporting, not worth panicking over."
            ),
        },
        "returns_vocabulary": {
            "state": "the state object described by state_semantics",
            "capabilities": "the build's limits (modes, steps, sanity envelope)",
            "command_results": "the outcome object described by command_results",
            "contract": "this document",
            "liveness": "an is-the-daemon-alive answer that says nothing about the device",
            "state_stream": "a push stream of state objects, one per change",
        },
        "command_results": {
            "ok": "The request is satisfied.",
            "changed": (
                "Whether this call wrote to the device. ok=true with changed=false means "
                "the request was already satisfied, or was a dry run — the detail says "
                "which — and nothing was written."
            ),
            "sent": (
                "The exact bytes put on the wire, or null when nothing was. This is the "
                "audit trail for a physical action: null means nothing was written and "
                "therefore nothing was verified."
            ),
            "detail": "One sentence saying what happened, written to be shown to a human.",
            "dry_run": (
                "Echoes the request's dry_run. On a rehearsal, changed is false and "
                "sent is null — nothing goes to the device."
            ),
            "before / after": "State snapshots taken around the command.",
        },
        "operations": [
            {
                "name": "health",
                "returns": "liveness",
                "kind": "service",
                "physical": False,
                "purpose": "Liveness of the daemon process.",
                "params": [],
                "guidance": (
                    "Answers without a credential and says nothing about the device. A "
                    "healthy daemon with an unavailable device is a normal combination."
                ),
            },
            {
                "name": "get_contract",
                "returns": "contract",
                "kind": "service",
                "physical": False,
                "purpose": "This document.",
                "params": [],
                "guidance": (
                    "Static within a build: re-read it at the start of a session, or "
                    "after any call answers 404 or 405. The transport section is the "
                    "adapter's own and can move without contract_version."
                ),
            },
            {
                "name": "get_state",
                "returns": "state",
                "kind": "read",
                "physical": False,
                "purpose": "The current belief about the device, with its age.",
                "params": [],
                "guidance": (
                    "Cheap and safe to call often — it never touches the radio, so it "
                    "reports belief, not a fresh measurement. Check stale and "
                    "reading_age_s before treating values as current, and read "
                    "state_semantics before interpreting any field."
                ),
            },
            {
                "name": "get_capabilities",
                "returns": "capabilities",
                "kind": "read",
                "physical": False,
                "purpose": "What this build permits: modes, fan steps, wire granularity.",
                "params": [],
                "guidance": (
                    "These are the code's limits, not the device's. The permitted "
                    "temperature range is per-mode and lives on the state; the absolutes "
                    "here are only a sanity envelope."
                ),
            },
            {
                "name": "stream_state",
                "returns": "state_stream",
                "kind": "read",
                "physical": False,
                "purpose": "State pushed on every change, over a one-way stream.",
                "params": [],
                "guidance": (
                    "Each message carries the same state object get_state returns. The "
                    "stream is output-only; commands are not accepted on it."
                ),
            },
            {
                "name": "turn_off",
                "returns": "command_results",
                "kind": "command",
                "physical": True,
                "purpose": "Switch the unit off.",
                "params": [_dry_run_param()],
                "guidance": (
                    "The command to prefer when uncertain. Idempotent by design: asking "
                    "an off heater to switch off is ok=true, changed=false, sent=null — "
                    "satisfied, with nothing written. An unverified turn_off is the one "
                    "outcome to escalate to a human immediately: the heater may still be "
                    "running."
                ),
            },
            {
                "name": "set_mode",
                "returns": "command_results",
                "kind": "command",
                "physical": True,
                "purpose": "Select an operating mode by name.",
                "params": [
                    {
                        "name": "mode",
                        "type": "string",
                        "required": True,
                        "enum": list(available_modes()),
                        "constraints": (
                            "modes outside this list (turbo, extended heat) are locked "
                            "in this build and are refused, not ignored"
                        ),
                    },
                    _dry_run_param(),
                ],
                "guidance": (
                    "A refusal for a locked mode is deliberate — those modes are locked "
                    "in code as a safety decision, and no parameter unlocks them. "
                    "Changing mode moves the permitted temperature range: re-read the "
                    "state before a following set_temperature."
                ),
            },
            {
                "name": "set_temperature",
                "returns": "command_results",
                "kind": "command",
                "physical": True,
                "purpose": "Set the target temperature, in either unit.",
                "params": [
                    {
                        "name": "celsius",
                        "type": "number",
                        "required": False,
                        "constraints": "give exactly one of celsius or fahrenheit",
                    },
                    {
                        "name": "fahrenheit",
                        "type": "number",
                        "required": False,
                        "constraints": "give exactly one of celsius or fahrenheit",
                    },
                    _dry_run_param(),
                ],
                "guidance": (
                    "Refused unless the unit is running (power 'on') — start it with "
                    "set_mode first — and refused until the device has reported bounds "
                    "for the current mode. The wire's granularity is "
                    f"{TEMPERATURE_STEP_C} °C, so the value sent may differ slightly "
                    "from the one asked for. The permitted range is per-mode and live "
                    "on the state (min_target_c / max_target_c); it is checked against "
                    "the device's own report, and a refusal quotes the device's current "
                    "bounds. Do not pre-validate against a cached or assumed range."
                ),
            },
            {
                "name": "set_fan",
                "returns": "command_results",
                "kind": "command",
                "physical": True,
                "purpose": "Set fan speed as a percentage.",
                "params": [
                    {
                        "name": "percent",
                        "type": "integer",
                        "required": True,
                        "constraints": (
                            f"{FAN_PERCENT_MIN}-{FAN_PERCENT_MAX} in steps of {FAN_PERCENT_STEP}"
                        ),
                    },
                    _dry_run_param(),
                ],
                "guidance": (
                    "Refused unless the unit is running — the fan byte is unobservable "
                    "in standby. An in-range value off the 5% grid is snapped to the "
                    "nearest step; the snapped value is what gets sent, verified, and "
                    "reported in the command description, so read the result rather "
                    "than assuming your raw request took effect. "
                    "And remember fan_is_stale when reading the result back: an idle "
                    "unit reports the last session's speed as a memory, not as airflow."
                ),
            },
            {
                "name": "yield_link",
                "returns": "state",
                "kind": "lease",
                "physical": False,
                "purpose": (
                    "Release the device's single BLE slot to its owner for a stated "
                    "number of seconds."
                ),
                "params": [
                    {
                        "name": "seconds",
                        "type": "number",
                        "required": True,
                        "constraints": (
                            "positive seconds; the link returns when it expires. 300 "
                            "is a sensible default when the owner did not name a "
                            "duration"
                        ),
                    },
                ],
                "guidance": (
                    "The deference operation, and the first thing to reach for when a "
                    "human wants their heater back: while this service is connected, "
                    "the vendor app cannot be. During a yield, commands return "
                    "unavailable with the remaining time in the detail. That is the "
                    "owner using their own device, not an outage."
                ),
            },
            {
                "name": "resume_link",
                "returns": "state",
                "kind": "lease",
                "physical": False,
                "purpose": "End a yield early.",
                "params": [],
                "guidance": (
                    "Only when the owner asked for it, or the reason for the yield has "
                    "clearly passed. Resuming uninvited takes the heater away from the "
                    "person in the bed — prefer letting the yield expire."
                ),
            },
        ],
    }
