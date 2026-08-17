"""MQTT adapter, with optional Home Assistant discovery.

A **peer** of the HTTP adapter, not a layer beneath it (ADR-0002). Home Assistant gets
first-class support without ever sitting in Jarvis's control path, and either adapter can be
run without the other.

Three things about MQTT shape this file:

- **There is no response channel.** A REST client gets 502 back when a command was written
  and the device did not visibly obey; a publisher gets nothing. So every command outcome is
  published to ``.../result`` — an unverified write to a heater must not be able to vanish
  into a broker.
- **Retained state is a promise about the future.** A retained payload is handed to every
  future subscriber as if it were current, so the availability topic is driven by *link and
  freshness together*: a reading we have not been able to refresh is published as offline
  rather than left sitting on the broker looking live.
- **Home Assistant's climate model is narrower than the device.** It has no "turbo" and no
  "unknown", so the mapping to it is lossy, and doing it here — in the adapter, from the
  API's own strings — is the point. The device model does not learn what a climate entity is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import aiomqtt

from ..api import ApiError, BedJetAPI, DeviceSnapshot, Unverified

log = logging.getLogger(__name__)

ONLINE = "online"
OFFLINE = "offline"


class Publisher(Protocol):
    """The one thing this adapter needs from a broker client.

    Narrow on purpose: publishing is the whole outbound surface, so the parts of this file
    that decide *what* to say can be exercised without a broker, and the parts that talk to
    one stay thin enough to read.
    """

    async def publish(
        self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False
    ) -> None: ...


class IncomingMessage(Protocol):
    """A topic and a payload — the whole of what this adapter reads from a message."""

    @property
    def topic(self) -> object: ...

    @property
    def payload(self) -> object: ...


#: Home Assistant's climate modes, and what we publish for each of ours. Turbo maps to
#: ``heat`` because that is what it is — the device's most aggressive heating setting — and
#: HA has no better slot for it. It is lossy on purpose and it is *this* file's loss to take:
#: nothing below ``api/`` knows a climate entity exists.
_HA_MODE_FOR = {
    "standby": "off",
    "cool": "cool",
    "heat": "heat",
    "dry": "dry",
    "turbo": "heat",
}


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """Broker connection and topic layout.

    Args:
        hostname / port: the broker.
        username / password: credentials, if the broker wants them.
        base_topic: prefix for our own topics.
        device_id: this unit's slug, used in topics and as the Home Assistant unique id.
        device_name: what a person should see in Home Assistant.
        discovery: publish Home Assistant MQTT-discovery configs.
        discovery_prefix: HA's discovery prefix, ``homeassistant`` unless changed.
        reconnect_seconds: wait between broker reconnection attempts.
    """

    hostname: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "bedjet"
    device_id: str = "bedjet"
    device_name: str = "BedJet"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"
    reconnect_seconds: float = 5.0

    @property
    def prefix(self) -> str:
        return f"{self.base_topic}/{self.device_id}"

    @property
    def state_topic(self) -> str:
        return f"{self.prefix}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.prefix}/availability"

    @property
    def result_topic(self) -> str:
        return f"{self.prefix}/result"

    @property
    def command_wildcard(self) -> str:
        return f"{self.prefix}/set/+"

    def command_topic(self, name: str) -> str:
        return f"{self.prefix}/set/{name}"


def ha_mode(snapshot: DeviceSnapshot) -> str:
    """The device's mode, in Home Assistant's vocabulary.

    ``off`` whenever the unit is not confirmed running: HA's climate entity has no way to
    say "unknown", and claiming a mode we are not sure of is worse than saying off while the
    availability topic reports the uncertainty honestly.
    """
    if snapshot.power != "on" or snapshot.mode is None:
        return "off"
    return _HA_MODE_FOR.get(snapshot.mode, "off")


def is_available(snapshot: DeviceSnapshot) -> bool:
    """Whether to tell subscribers this device is answering.

    Both halves are required. A live link with a reading we cannot refresh is not
    availability — and because the state topic is retained, a stale payload would otherwise
    be handed to every future subscriber as though it were current.
    """
    return snapshot.available and not snapshot.stale


def state_payload(snapshot: DeviceSnapshot) -> dict[str, Any]:
    """The API snapshot, plus the Home Assistant projections computed from it.

    The extra keys are derived here from the API's own strings — no protocol knowledge is
    involved — so a general MQTT consumer gets the honest view (``mode: "turbo"``) and a
    climate entity gets the one it can render (``ha_mode: "heat"``), from one payload.
    """
    payload = snapshot.to_dict()
    payload["ha_mode"] = ha_mode(snapshot)
    return payload


def discovery_payloads(config: MqttConfig) -> list[tuple[str, dict[str, Any]]]:
    """Home Assistant MQTT-discovery configs: a climate entity, a fan number, a sensor.

    Fan speed is a ``number`` rather than the climate entity's ``fan_mode`` because the
    device's fan is 5-100% in 5% steps (RL-002). Squeezing twenty steps into a handful of
    named modes would mean inventing a mapping, and the invented part is exactly the bit
    that would later be mistaken for a device fact.

    The target temperature range is deliberately **not** published here. It moves with the
    mode (RL-013) and turbo reports 43 °C, above the maximum every public source states —
    so the live ``min_target_c``/``max_target_c`` on the state topic are the authority, and
    a static min/max in a discovery payload would be wrong in at least one mode.
    """
    device = {
        "identifiers": [config.device_id],
        "name": config.device_name,
        "manufacturer": "BedJet",
        "model": "BedJet 3",
    }
    availability = [{"topic": config.availability_topic}]

    climate = {
        "name": None,  # HA uses the device name for the primary entity
        "unique_id": f"{config.device_id}_climate",
        "device": device,
        "availability": availability,
        "modes": ["off", "cool", "heat", "dry"],
        "mode_state_topic": config.state_topic,
        "mode_state_template": "{{ value_json.ha_mode }}",
        "mode_command_topic": config.command_topic("mode"),
        "current_temperature_topic": config.state_topic,
        "current_temperature_template": "{{ value_json.actual_temp_c }}",
        "temperature_state_topic": config.state_topic,
        "temperature_state_template": "{{ value_json.target_temp_c }}",
        "temperature_command_topic": config.command_topic("temperature"),
        "temperature_unit": "C",
        "temp_step": 0.5,
        "precision": 0.5,
    }

    fan = {
        "name": "Fan",
        "unique_id": f"{config.device_id}_fan",
        "device": device,
        "availability": availability,
        "state_topic": config.state_topic,
        "value_template": "{{ value_json.fan_percent }}",
        "command_topic": config.command_topic("fan"),
        "min": 5,
        "max": 100,
        "step": 5,
        "unit_of_measurement": "%",
        "icon": "mdi:fan",
    }

    ambient = {
        "name": "Ambient temperature",
        "unique_id": f"{config.device_id}_ambient",
        "device": device,
        "availability": availability,
        "state_topic": config.state_topic,
        "value_template": "{{ value_json.ambient_temp_c }}",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
    }

    root = config.discovery_prefix
    return [
        (f"{root}/climate/{config.device_id}/config", climate),
        (f"{root}/number/{config.device_id}_fan/config", fan),
        (f"{root}/sensor/{config.device_id}_ambient/config", ambient),
    ]


class MqttBridge:
    """Publishes state, accepts commands, and reconnects to the broker on its own."""

    def __init__(self, api: BedJetAPI, config: MqttConfig) -> None:
        self._api = api
        self._config = config
        # Bounded, newest-wins: the producer is the BLE notification path and must never
        # wait on a broker, and state is a snapshot rather than a log.
        self._outgoing: asyncio.Queue[DeviceSnapshot] = asyncio.Queue(maxsize=16)
        self._unsubscribe: Callable[[], None] | None = None

    def _enqueue(self, snapshot: DeviceSnapshot) -> None:
        while True:
            try:
                self._outgoing.put_nowait(snapshot)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._outgoing.get_nowait()

    async def run(self) -> None:
        """Connect, serve, and keep reconnecting until cancelled."""
        self._unsubscribe = self._api.subscribe(self._enqueue)
        try:
            while True:
                try:
                    await self._session()
                except aiomqtt.MqttError as exc:
                    log.warning(
                        "broker connection lost (%s); retrying in %.0fs",
                        exc,
                        self._config.reconnect_seconds,
                    )
                    await asyncio.sleep(self._config.reconnect_seconds)
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()

    async def _session(self) -> None:
        config = self._config
        will = aiomqtt.Will(config.availability_topic, OFFLINE, qos=1, retain=True)
        async with aiomqtt.Client(
            config.hostname,
            config.port,
            username=config.username,
            password=config.password,
            identifier=f"bedjet-local-{config.device_id}",
            will=will,
        ) as client:
            log.info("connected to broker %s:%d", config.hostname, config.port)
            if config.discovery:
                for topic, payload in discovery_payloads(config):
                    await client.publish(topic, json.dumps(payload), qos=1, retain=True)
                log.info("published Home Assistant discovery for %s", config.device_id)

            await client.subscribe(config.command_wildcard, qos=1)
            await self._publish_state(client, self._api.snapshot())

            publisher = asyncio.create_task(self._publish_loop(client))
            try:
                async for message in client.messages:
                    await self._handle(client, message)
            finally:
                publisher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publisher
                # Say goodbye explicitly. The will covers a crash; a clean exit should not
                # leave a retained "online" behind for the next subscriber to believe.
                with contextlib.suppress(aiomqtt.MqttError):
                    await client.publish(config.availability_topic, OFFLINE, qos=1, retain=True)

    async def _publish_loop(self, client: Publisher) -> None:
        while True:
            snapshot = await self._outgoing.get()
            await self._publish_state(client, snapshot)

    async def _publish_state(self, client: Publisher, snapshot: DeviceSnapshot) -> None:
        config = self._config
        await client.publish(
            config.availability_topic,
            ONLINE if is_available(snapshot) else OFFLINE,
            qos=1,
            retain=True,
        )
        await client.publish(
            config.state_topic, json.dumps(state_payload(snapshot)), qos=1, retain=True
        )

    async def _handle(self, client: Publisher, message: IncomingMessage) -> None:
        """Dispatch one command topic, and publish what happened.

        Every path here reports. MQTT gives a publisher no reply, so a refusal that is only
        logged is a refusal nobody sees — and an *unverified* write is worse than that,
        because the heater may well have obeyed.
        """
        name = str(message.topic).rsplit("/", 1)[-1]
        payload = _decode(message.payload)
        try:
            outcome = await self._dispatch(name, payload)
        except ApiError as exc:
            kind = "unverified" if isinstance(exc, Unverified) else "refused"
            log.warning("%s: %s -> %s", kind, name, exc)
            await self._publish_result(client, {"command": name, "error": kind, "detail": str(exc)})
            return
        except ValueError as exc:
            log.warning("invalid payload on %s: %s", name, exc)
            await self._publish_result(
                client, {"command": name, "error": "invalid_request", "detail": str(exc)}
            )
            return
        await self._publish_result(client, {"command": name, **outcome})

    async def _dispatch(self, name: str, payload: str) -> dict[str, Any]:
        api = self._api
        if name == "mode":
            # HA sends its own vocabulary on the climate entity's command topic, and "off"
            # is a *mode* there while it is a command here. They coincide, which is lucky
            # rather than designed, so the translation is explicit.
            return (await api.set_mode(payload.strip().lower())).to_dict()
        if name == "temperature":
            return (await api.set_temperature(celsius=_number(payload))).to_dict()
        if name == "fan":
            return (await api.set_fan(int(_number(payload)))).to_dict()
        if name == "link_yield":
            return (await api.yield_link(_number(payload))).to_dict()
        if name == "link_resume":
            return (await api.resume_link()).to_dict()
        raise ValueError(
            f"unknown command topic {name!r}; expected one of mode, temperature, fan, "
            f"link_yield, link_resume"
        )

    async def _publish_result(self, client: Publisher, body: dict[str, Any]) -> None:
        # Not retained: a result describes one moment, and handing a months-old refusal to
        # a fresh subscriber as though it had just happened would be its own small lie.
        await client.publish(self._config.result_topic, json.dumps(body), qos=1, retain=False)


def _decode(payload: object) -> str:
    if isinstance(payload, bytes | bytearray):
        return payload.decode("utf-8", errors="replace").strip()
    return str(payload).strip()


def _number(payload: str) -> float:
    try:
        return float(payload)
    except ValueError as exc:
        raise ValueError(f"expected a number, got {payload!r}") from exc
