"""Pure protocol layer: bytes in, dataclasses out. No I/O, no async, no bleak."""

from . import encode
from .constants import CommandMode, Offset, StatusMode
from .decode import decode_status, reassemble
from .packets import StatusPacket

__all__ = [
    "CommandMode",
    "Offset",
    "StatusMode",
    "StatusPacket",
    "decode_status",
    "encode",
    "reassemble",
]
