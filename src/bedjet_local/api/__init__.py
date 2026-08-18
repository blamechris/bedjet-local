"""The stable local interface (Milestone 3).

``api/`` is the seam ADR-0002 draws between *this device* and *anything that wants to use
it*. Adapters — HTTP, WebSocket, MQTT, Jarvis — depend on the names here and on nothing
below them. When one of them needs something this module does not expose, the answer is to
add it here.
"""

from .contract import CONTRACT_VERSION, agent_contract
from .models import Capabilities, CommandOutcome, DeviceSnapshot
from .service import (
    ApiError,
    BedJetAPI,
    Refused,
    SnapshotListener,
    Unavailable,
    Unverified,
    available_modes,
)

__all__ = [
    "CONTRACT_VERSION",
    "ApiError",
    "BedJetAPI",
    "Capabilities",
    "CommandOutcome",
    "DeviceSnapshot",
    "Refused",
    "SnapshotListener",
    "Unavailable",
    "Unverified",
    "agent_contract",
    "available_modes",
]
