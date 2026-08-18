"""The agent-facing contract document (ADR-0005 decision 1).

Most of the document is prose, and prose cannot be proven by a test — the review gate owns
it. What a test *can* hold is everything a consumer would parse: the structure, the derived
facts, the JSON-safety, and the promise that the document never restates something the code
already knows. The route parity half of the story lives with the HTTP adapter in
``tests/integration/test_http_ws.py``, next to the table it checks.
"""

from __future__ import annotations

import copy
import json

from bedjet_local.api import CONTRACT_VERSION, agent_contract, available_modes

REQUIRED_OPERATION_FIELDS = {"name", "kind", "physical", "purpose", "params", "guidance"}
REQUIRED_PARAM_FIELDS = {"name", "type", "required", "constraints"}


def test_the_document_is_json_safe_and_deterministic() -> None:
    """A consumer gets the same document on every call, and nothing in it needs an
    encoder — it is the wire shape, not a precursor to one."""
    first, second = agent_contract(), agent_contract()
    assert first == second
    assert json.loads(json.dumps(first)) == first


def test_mutating_a_served_document_cannot_bleed_into_the_next_one() -> None:
    """The adapter decorates the document on the way out, and a consumer may do anything
    to its copy. Shared substructure would turn one caller's edit into another's contract."""
    tampered = agent_contract()
    tampered["extra"] = True
    for operation in tampered["operations"]:
        for param in operation["params"]:
            param["constraints"] = "tampered"
    assert agent_contract() == copy.deepcopy(agent_contract())
    assert "tampered" not in json.dumps(agent_contract())


def test_every_operation_carries_the_fields_an_agent_navigates_by() -> None:
    operations = agent_contract()["operations"]
    assert operations, "an empty contract describes nothing"
    for operation in operations:
        missing = REQUIRED_OPERATION_FIELDS - set(operation)
        assert not missing, f"{operation.get('name')} is missing {missing}"
        assert operation["kind"] in {"service", "read", "command", "lease"}
        for param in operation["params"]:
            missing = REQUIRED_PARAM_FIELDS - set(param)
            assert not missing, f"{operation['name']}.{param.get('name')} is missing {missing}"


def test_exactly_the_commands_are_physical() -> None:
    """``physical`` is the flag an agent gates confirmation on, so it must mean one thing:
    this operation can move the appliance. Reads and the lease never can; commands always
    can."""
    operations = agent_contract()["operations"]
    physical = {op["name"] for op in operations if op["physical"]}
    commands = {op["name"] for op in operations if op["kind"] == "command"}
    assert physical == commands == {"turn_off", "set_mode", "set_temperature", "set_fan"}


def test_every_command_offers_dry_run() -> None:
    """The rehearsal path is part of the safety story; a command without it would leave an
    agent no way to validate intent short of acting on a heater."""
    for operation in agent_contract()["operations"]:
        if operation["kind"] != "command":
            continue
        names = {param["name"] for param in operation["params"]}
        assert "dry_run" in names, f"{operation['name']} offers no rehearsal"


def test_modes_are_derived_from_the_commanders_allowlist_not_restated() -> None:
    """Unlocking a mode is a deliberate edit to ``service/commander.py``; the contract
    must follow that edit automatically rather than hold a second copy to forget."""
    set_mode = next(op for op in agent_contract()["operations"] if op["name"] == "set_mode")
    mode_param = next(p for p in set_mode["params"] if p["name"] == "mode")
    assert mode_param["enum"] == list(available_modes())


def test_the_failure_modes_are_exactly_the_apis_three() -> None:
    """One name per exception class in ``api/service.py``, and the one distinction that
    justifies the whole section — whether the device was written to — stated per mode."""
    modes = agent_contract()["failure_modes"]
    assert set(modes) == {"refused", "unavailable", "unverified"}
    assert modes["refused"]["device_was_written_to"] is False
    assert modes["unavailable"]["device_was_written_to"] is False
    assert modes["unverified"]["device_was_written_to"] is True


def test_the_document_carries_no_live_state() -> None:
    """The contract is static per build. A live value in it — a temperature, a bound, an
    address — would teach a consumer to cache exactly what the state exists to serve
    fresh."""
    document = agent_contract()
    assert document["contract_version"] == CONTRACT_VERSION
    flattened = json.dumps(document)
    for live_only in ('min_target_c":', 'reading_age_s":', 'address":'):
        assert live_only not in flattened


def test_read_first_teaches_the_load_bearing_moves() -> None:
    """The preamble is prose, but its cross-references are structural: it must point at
    the yield operation, the rehearsal parameter, and the safe command by name."""
    read_first = " ".join(agent_contract()["read_first"])
    assert "yield_link" in read_first
    assert "dry_run" in read_first
    assert "turn_off" in read_first
