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


def test_milestone_1_ships_no_command_encoder() -> None:
    """Milestone 1 is read-only by construction (docs/SAFETY.md).

    When Milestone 2 begins, this test is deleted in the same commit that adds
    ``protocol/encode.py`` — deliberately, with the safety review that implies, rather
    than by an encoder quietly appearing.
    """
    assert not (SRC / "protocol" / "encode.py").exists(), (
        "A command encoder has appeared. If Milestone 2 has started, delete this test "
        "explicitly and read docs/SAFETY.md first."
    )
