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


#: Commands the write path is allowed to construct, in the order they were unlocked. A
#: command joins this list only once the previous one is VERIFIED on hardware — see
#: docs/SAFETY.md's bring-up order. Widening it is a safety decision, made here in the open.
UNLOCKED_COMMANDS = {
    "turn_off",  # ✅ VERIFIED RL-019
    "set_fan_percent",  # 📖 unverified — thermally inert, next in the bring-up order
}


def test_the_write_path_constructs_only_unlocked_commands() -> None:
    """Commands are verified one at a time, in increasing order of consequence.

    Every command byte started as unverified upstream guesswork (RL-016). Temperature and
    heat are the consequential ones and stay locked until the inert ones have proven the
    opcode framing.
    """
    source = (SRC / "service" / "commander.py").read_text()
    locked = ["set_temperature", "set_timer", "set_mode(", "press("]
    used = [name for name in locked if name in source]
    assert not used, (
        f"commander.py can now construct {used}, which is not in UNLOCKED_COMMANDS. Verify "
        f"the previous command on hardware first, then unlock this one deliberately."
    )
    assert "turn_off" in source


def test_encoder_is_pure() -> None:
    """The encoder must not acquire I/O along with its purpose."""
    forbidden = {"asyncio", "bleak", "socket"}
    leaked = forbidden & {n.split(".")[0] for n in _imports(SRC / "protocol" / "encode.py")}
    assert not leaked, f"encode.py imports {leaked}"
