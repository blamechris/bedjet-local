"""Architectural invariants, enforced rather than documented.

The whole testability story depends on ``protocol/`` being pure and ``bleak`` being
confined to ``transport/``. Both are easy to break with one convenient import, and neither
break would fail any other test — so they get their own.

If a change makes one of these fail, the change is wrong. Do not relax the test.
See AGENTS.md rule 4.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bedjet_local"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_bleak_is_confined_to_the_transport_layer() -> None:
    offenders = [
        path.relative_to(SRC)
        for path in SRC.rglob("*.py")
        if path.parent.name != "transport"
        and any(name.split(".")[0] == "bleak" for name in _imports(path))
    ]
    assert not offenders, f"bleak imported outside transport/: {offenders}"


def test_protocol_layer_is_pure() -> None:
    """No I/O and no async in ``protocol/`` — it must be exercisable with plain bytes."""
    forbidden = {"asyncio", "bleak", "socket", "threading", "aiohttp", "paho"}
    for path in (SRC / "protocol").rglob("*.py"):
        leaked = forbidden & {name.split(".")[0] for name in _imports(path)}
        assert not leaked, f"{path.name} imports {leaked}; protocol/ must stay pure"


def test_protocol_layer_defines_no_coroutines() -> None:
    for path in (SRC / "protocol").rglob("*.py"):
        tree = ast.parse(path.read_text())
        coroutines = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
        ]
        assert not coroutines, f"{path.name} defines async functions: {coroutines}"


def test_device_layer_does_not_import_transport() -> None:
    """``device/`` models the device, not the link. It must not know how bytes arrive."""
    for path in (SRC / "device").rglob("*.py"):
        source = path.read_text()
        assert "transport" not in source, f"{path.name} references the transport layer"


#: The write path is deliberately one module wide. Widening it is a safety decision, not a
#: refactor, so it has to be made here in the open.
ALLOWED_TO_WRITE = {"transport", "commander.py"}


def test_only_the_commander_can_send() -> None:
    """Exactly one module outside ``transport/`` may put bytes on the wire.

    Encoding is pure and safe — it builds byte strings. *Sending* is the physical act, and
    it lives in one auditable place so that "which code can command a heater?" has a
    one-line answer.
    """
    senders = []
    for path in SRC.rglob("*.py"):
        if path.parent.name in ALLOWED_TO_WRITE or path.name in ALLOWED_TO_WRITE:
            continue
        source = path.read_text()
        if ".write(" in source or "write_gatt_char" in source:
            senders.append(str(path.relative_to(SRC)))
    assert not senders, (
        f"a call to transport write appeared in {senders}. The write path is one module "
        f"wide on purpose — widening it is a safety decision, so make it deliberately and "
        f"read docs/SAFETY.md first."
    )


def test_the_write_path_constructs_only_unlocked_commands() -> None:
    """Commands are unlocked one at a time, in increasing order of consequence.

    Unlocked so far: OFF (✅ RL-019), fan (✅ RL-020), mode restricted to the thermally safe
    operands (COOL ✅ RL-021), and temperature — which is bounded by the device's own
    reported range and is therefore safe in a cooling mode. Timer and button presses stay
    locked until the unlocked ones have proven their framing on hardware.
    """
    source = (SRC / "service" / "commander.py").read_text()
    locked = ["set_timer", "press("]
    used = [name for name in locked if name in source]
    assert not used, (
        f"commander.py can now construct {used}. Verify the previously unlocked command on "
        f"hardware first, then unlock this one deliberately, in its own commit."
    )


def test_the_most_aggressive_modes_stay_locked() -> None:
    """Unlocking is deliberate, per mode, with a reason — asserted on the real object.

    HEAT was unlocked once OFF was verified and reproducible: a proven software stop is the
    precondition that makes testing heat reasonable, and we did not have it before. TURBO is
    the device's most aggressive setting (a fixed 43 °C) and nothing needs it.
    """
    from bedjet_local.protocol.constants import CommandMode
    from bedjet_local.service.commander import UNLOCKED_MODES

    for mode in (CommandMode.TURBO, CommandMode.EXTENDED_HEAT):
        assert mode not in UNLOCKED_MODES, f"{mode.name} must stay locked"

    for mode in (CommandMode.OFF, CommandMode.COOL, CommandMode.HEAT):
        assert mode in UNLOCKED_MODES


def test_a_mode_with_no_verified_status_value_cannot_be_verified() -> None:
    """Extended heat is refused twice over: not unlocked, and unverifiable in principle.

    Status ``0x03`` has never been observed, so there is no value to compare a result
    against. A command whose success cannot be distinguished from its failure must not be
    sendable, independently of any allowlist.
    """
    from bedjet_local.protocol.constants import CommandMode
    from bedjet_local.service.commander import _EXPECTED_STATUS_FOR

    assert CommandMode.EXTENDED_HEAT not in _EXPECTED_STATUS_FOR


def test_encoder_is_pure() -> None:
    """The encoder must not acquire I/O along with its purpose."""
    forbidden = {"asyncio", "bleak", "socket"}
    leaked = forbidden & {n.split(".")[0] for n in _imports(SRC / "protocol" / "encode.py")}
    assert not leaked, f"encode.py imports {leaked}"
