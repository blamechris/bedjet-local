"""The MQTT adapter, against a recording publisher. No broker, no device, no network.

MQTT gives a publisher no reply, which moves the burden of honesty onto what gets published:
a refusal nobody can see is a refusal nobody acts on, and a retained payload is handed to
every *future* subscriber as though it were current. Most of what follows is about those two
properties rather than about topic strings.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from bedjet_local.api import BedJetAPI, DeviceSnapshot
from bedjet_local.integrations.mqtt import (
    MqttBridge,
    MqttConfig,
    discovery_payloads,
    ha_mode,
    is_available,
    state_payload,
)
from bedjet_local.protocol.constants import COMMAND_UUID, STATUS_UUID, StatusMode
from bedjet_local.service.session import DeviceSession
from bedjet_local.transport.mock import MockTransport
from tests.unit.test_decode import build_status

RUNNING = build_status(mode=StatusMode.COOL, target=50)
STANDBY = build_status(mode=StatusMode.STANDBY, target=50, min_temp=20, max_temp=80)


class RecordingBroker:
    """Records publishes. Satisfies the adapter's ``Publisher`` protocol and nothing more."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    async def publish(
        self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False
    ) -> None:
        self.published.append((topic, str(payload), retain))

    def payloads_for(self, topic: str) -> list[Any]:
        return [json.loads(body) for name, body, _ in self.published if name == topic]

    def last_for(self, topic: str) -> str:
        matching = [body for name, body, _ in self.published if name == topic]
        assert matching, f"nothing was published to {topic}"
        return matching[-1]


class FakeMessage:
    """Stands in for an ``aiomqtt.Message``: a topic and a payload is all the adapter reads."""

    def __init__(self, topic: str, payload: str) -> None:
        self.topic = topic
        self.payload = payload.encode()


async def _bridge(
    transport: MockTransport, initial: bytes | None = RUNNING
) -> tuple[MqttBridge, DeviceSession, MqttConfig]:
    session = DeviceSession(transport, "mock", settle_timeout=0.3, supervise_interval=0.02)
    await session.start()
    if initial is not None:
        transport.emit(STATUS_UUID, initial)
    config = MqttConfig()
    return MqttBridge(BedJetAPI(session), config), session, config


def _snapshot(**overrides: Any) -> DeviceSnapshot:
    base = DeviceSnapshot(
        link="connected",
        available=True,
        stale=False,
        reading_age_s=1.0,
        power="on",
        mode="cool",
        target_temp_c=25.0,
        target_temp_f=77.0,
        actual_temp_c=22.0,
        actual_temp_f=71.6,
        ambient_temp_c=21.0,
        ambient_temp_f=69.8,
        fan_percent=50,
        fan_is_stale=False,
        time_remaining_s=1800,
        min_target_c=19.0,
        max_target_c=26.0,
        max_runtime_s=43200,
        anomalies=(),
    )
    return replace(base, **overrides)


# ── projections ─────────────────────────────────────────────────────────────────────────


def test_home_assistant_modes_are_mapped_here_and_nowhere_below() -> None:
    assert ha_mode(_snapshot(mode="cool")) == "cool"
    assert ha_mode(_snapshot(mode="dry")) == "dry"
    # Turbo is heat as far as a climate entity is concerned. Lossy on purpose, and it is
    # this file's loss to take.
    assert ha_mode(_snapshot(mode="turbo")) == "heat"
    assert ha_mode(_snapshot(power="off", mode="standby")) == "off"


def test_an_unknown_power_state_is_published_as_off_not_guessed() -> None:
    """HA's climate entity cannot say "unknown". Saying off while the availability topic
    reports the uncertainty is honest; inventing a mode is not."""
    assert ha_mode(_snapshot(power="unknown", mode=None)) == "off"


def test_availability_requires_a_live_link_and_a_fresh_reading() -> None:
    """The state topic is retained, so a stale payload would be served to every future
    subscriber as if it were current. Freshness is half of what availability means here."""
    assert is_available(_snapshot()) is True
    assert is_available(_snapshot(available=False)) is False
    assert is_available(_snapshot(stale=True)) is False


def test_the_state_payload_keeps_the_honest_view_alongside_the_ha_one() -> None:
    payload = state_payload(_snapshot(mode="turbo"))
    assert payload["mode"] == "turbo", "a general consumer gets the truth"
    assert payload["ha_mode"] == "heat", "a climate entity gets what it can render"
    json.dumps(payload)


def test_discovery_does_not_publish_a_static_temperature_range() -> None:
    """The permitted range moves with the mode and turbo reports 43 C, above the maximum
    every public source states (RL-013). A static min/max would be wrong in some mode, so
    the live fields on the state topic are the authority."""
    payloads = dict(discovery_payloads(MqttConfig()))
    climate = payloads["homeassistant/climate/bedjet/config"]
    assert "min_temp" not in climate
    assert "max_temp" not in climate


def test_discovery_covers_climate_fan_and_ambient() -> None:
    topics = [topic for topic, _ in discovery_payloads(MqttConfig())]
    assert topics == [
        "homeassistant/climate/bedjet/config",
        "homeassistant/number/bedjet_fan/config",
        "homeassistant/sensor/bedjet_ambient/config",
    ]


def test_discovery_never_offers_a_locked_mode() -> None:
    climate = dict(discovery_payloads(MqttConfig()))["homeassistant/climate/bedjet/config"]
    assert "turbo" not in climate["modes"]


# ── publishing ──────────────────────────────────────────────────────────────────────────


async def test_state_and_availability_are_retained_together() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._publish_state(broker, bridge._api.snapshot())

        assert broker.last_for(config.availability_topic) == "online"
        state = json.loads(broker.last_for(config.state_topic))
        assert state["ha_mode"] == "cool"
        assert all(retain for _, _, retain in broker.published), "state must be retained"
    finally:
        await session.stop()


async def test_a_lost_link_publishes_offline_rather_than_leaving_state_looking_live() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        transport.refuse_next_connects(50)
        transport.drop()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not bridge._api.snapshot().available:
                break

        await bridge._publish_state(broker, bridge._api.snapshot())
        assert broker.last_for(config.availability_topic) == "offline"
    finally:
        await session.stop()


# ── commanding ──────────────────────────────────────────────────────────────────────────


async def test_a_command_is_dispatched_and_its_outcome_published() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()

    async def respond() -> None:
        await asyncio.sleep(0.05)
        transport.emit(STATUS_UUID, STANDBY)

    task = asyncio.create_task(respond())
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("mode"), "off"))
        await task

        assert transport.writes == [(COMMAND_UUID, bytes([0x01, 0x01]))]
        result = json.loads(broker.last_for(config.result_topic))
        assert result["command"] == "mode"
        assert result["sent"] == "01 01"
    finally:
        await session.stop()


async def test_an_unverified_write_is_published_not_merely_logged() -> None:
    """The reason this adapter has a result topic at all. MQTT has no reply, so an
    unverified write to a heater would otherwise vanish into the broker — and unverified
    means the device may well have obeyed."""
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("mode"), "off"))

        result = json.loads(broker.last_for(config.result_topic))
        assert result["error"] == "unverified"
        assert transport.writes, "the bytes went out — that is what makes it unverified"
    finally:
        await session.stop()


async def test_a_refusal_is_published_and_distinguishable_from_an_unverified_write() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("mode"), "turbo"))

        result = json.loads(broker.last_for(config.result_topic))
        assert result["error"] == "refused"
        assert transport.writes == []
    finally:
        await session.stop()


async def test_results_are_not_retained() -> None:
    """A result describes one moment. Handing a months-old refusal to a fresh subscriber as
    though it had just happened is its own small lie."""
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("mode"), "turbo"))
        retained = [retain for topic, _, retain in broker.published if topic == config.result_topic]
        assert retained == [False]
    finally:
        await session.stop()


async def test_a_junk_payload_is_reported_rather_than_crashing_the_bridge() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("fan"), "quite fast"))
        result = json.loads(broker.last_for(config.result_topic))
        assert result["error"] == "invalid_request"

        await bridge._handle(broker, FakeMessage(config.command_topic("nonsense"), "1"))
        assert json.loads(broker.last_for(config.result_topic))["error"] == "invalid_request"
        assert transport.writes == []
    finally:
        await session.stop()


async def test_the_yield_command_hands_the_device_back() -> None:
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("link_yield"), "300"))
        assert session.link.value == "yielded"
        assert transport.is_connected is False
    finally:
        await session.stop()


@pytest.mark.parametrize("payload", ["OFF", " off ", "Off"])
async def test_mode_payloads_are_forgiving_about_case_and_space(payload: str) -> None:
    """Whatever publishes to this topic is somebody else's automation, and a trailing
    newline from a shell should not be a failed command against a heater."""
    transport = MockTransport()
    bridge, session, config = await _bridge(transport)
    broker = RecordingBroker()

    async def respond() -> None:
        await asyncio.sleep(0.05)
        transport.emit(STATUS_UUID, STANDBY)

    task = asyncio.create_task(respond())
    try:
        await bridge._handle(broker, FakeMessage(config.command_topic("mode"), payload))
        await task
        assert transport.writes == [(COMMAND_UUID, bytes([0x01, 0x01]))]
    finally:
        await session.stop()
