"""Stopping a daemon releases the link — on SIGTERM, not only on Ctrl-C.

`serve` and `mqtt` reached their cleanup through ``except KeyboardInterrupt``, which covers
the terminal and nothing else. SIGTERM's default action terminates the process without
raising, so the `finally` that releases the BLE link never ran under ``launchctl bootout``,
a supervisor, or a reboot — the three ways the daemon will actually be stopped once it runs
unattended (#12, found stopping the RL-029 verification run).

These send a real SIGTERM to the test process rather than calling the handler directly. A
signal that is delivered but never reaches the event loop is exactly the failure being
guarded against, and only a real one proves the wiring.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from bedjet_local.cli import _release, _stop_signals


async def test_sigterm_reaches_the_event_loop() -> None:
    async with _stop_signals() as stopping:
        assert not stopping.is_set()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopping.wait(), timeout=2.0)


async def test_sigint_takes_the_same_path() -> None:
    """Ctrl-C must keep working, and arrive by the same route rather than as an exception."""
    async with _stop_signals() as stopping:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(stopping.wait(), timeout=2.0)


async def test_handlers_are_removed_on_exit() -> None:
    """Leaving them installed would silently swallow signals for whatever runs next."""
    loop = asyncio.get_running_loop()
    async with _stop_signals():
        pass
    assert loop.remove_signal_handler(signal.SIGTERM) is False
    assert loop.remove_signal_handler(signal.SIGINT) is False


async def test_release_runs_every_closer_in_order() -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    await _release(first, second)
    assert order == ["first", "second"]


async def test_release_is_bounded_when_a_closer_hangs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A graceful release that hangs is worse than an abrupt one: SIGKILL is coming anyway.

    launchd follows SIGTERM with SIGKILL after ~20s, so an unbounded disconnect burns the
    whole window and still dies — having released nothing and reported nothing.
    """
    monkeypatch.setattr("bedjet_local.cli._SHUTDOWN_GRACE_S", 0.05)
    reached = False

    async def hangs() -> None:
        await asyncio.sleep(60)

    async def after() -> None:
        nonlocal reached
        reached = True

    await asyncio.wait_for(_release(hangs, after), timeout=2.0)

    assert reached is False, "a hung closer must not be waited out at the next one's expense"
    out = capsys.readouterr().out
    assert "Shutdown exceeded" in out
    assert "was not told" in out, "the operator must be told the device was not released"


async def test_release_reports_the_link_is_back_when_it_finished(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The owner's cue that their heater is theirs again. It must not print after a timeout."""

    async def clean() -> None:
        return None

    await _release(clean)
    assert "Link released" in capsys.readouterr().out
