"""Adapters. Peers of one another, all of them sitting on top of ``api/``.

An adapter translates the stable local interface into somebody else's idiom — HTTP for
Jarvis and scripts, MQTT for Home Assistant. It may not contain protocol knowledge: no byte
offsets, no packet layout, no ``StatusMode``. If an adapter needs one of those, the
abstraction has leaked and the fix belongs in ``api/`` (ADR-0002, and enforced by
``tests/unit/test_layering.py``).

Each adapter's dependency is an extra, so a host that runs only the MQTT bridge does not
have to install an HTTP server.
"""
