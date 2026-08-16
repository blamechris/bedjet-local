"""Device-ownership registry tests.

RL-006: two BedJets were in radio range during bring-up and only one was ours. They were
indistinguishable by advertisement — same name, same service UUID, no manufacturer data.
These tests pin the guard that keeps the other one unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bedjet_local.device.registry import (
    UnknownDeviceError,
    load,
    lookup,
    registry_path,
    require_known,
)

# Synthetic addresses. Real ones live only in the gitignored registry — a device address
# is host-local and private, and this repository is public.
OURS = "00000000-1111-2222-3333-444444444444"
NEIGHBOURS = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    path = tmp_path / "devices.local.toml"
    path.write_text(f'[device.bedroom]\naddress = "{OURS}"\nnotes = "power test 2026-08-16"\n')
    return path


def test_loads_registered_device(registry_file: Path) -> None:
    devices = load(registry_file)
    assert len(devices) == 1
    entry = devices[OURS.lower()]
    assert entry.label == "bedroom"
    assert "power test" in entry.notes


def test_lookup_is_case_insensitive(registry_file: Path) -> None:
    assert lookup(OURS.lower(), registry_file) is not None
    assert lookup(OURS.upper(), registry_file) is not None


def test_require_known_accepts_our_device(registry_file: Path) -> None:
    assert require_known(OURS, registry_file).label == "bedroom"


def test_require_known_refuses_the_neighbours_device(registry_file: Path) -> None:
    """The whole point: a BedJet we did not register is unreachable."""
    with pytest.raises(UnknownDeviceError) as excinfo:
        require_known(NEIGHBOURS, registry_file)
    assert "not in" in str(excinfo.value)


def test_missing_registry_refuses_everything(tmp_path: Path) -> None:
    """A fresh checkout must not connect to whatever it happens to find."""
    with pytest.raises(UnknownDeviceError) as excinfo:
        require_known(OURS, tmp_path / "absent.toml")
    message = str(excinfo.value)
    assert "No device registry" in message
    assert "power test" in message, "the refusal should explain how to identify your unit"


def test_missing_registry_loads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    """`discover` must still work on a fresh checkout — only connecting is gated."""
    assert load(tmp_path / "absent.toml") == {}


def test_registry_path_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BEDJET_DEVICES", str(tmp_path / "elsewhere.toml"))
    assert registry_path() == tmp_path / "elsewhere.toml"


def test_registry_file_is_gitignored() -> None:
    """Addresses are host-local and are nobody else's business. Keep them out of git."""
    root = Path(__file__).resolve().parents[2]
    assert "devices.local.toml" in (root / ".gitignore").read_text()
