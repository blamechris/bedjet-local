"""Device model: state and capabilities, free of protocol detail."""

from .state import BedJetState, Power

__all__ = ["BedJetState", "Power"]
