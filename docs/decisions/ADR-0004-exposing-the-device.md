# ADR-0004 — A daemon that can switch on a heater: exposure, authentication, and the lease

**Date:** 2026-08-17
**Status:** Accepted

## Context

Milestones 1 and 2 produced a CLI. A CLI is a *borrower*: it connects, does one thing,
releases the link, and exits. Its blast radius is the person typing.

Milestone 3 produces a daemon, and three properties change at once:

1. **It listens.** Something other than the owner can now cause a physical action.
2. **It holds the device's only BLE slot**, continuously. The BedJet permits one client at
   a time, and this unit has **no working physical remote** (`docs/SAFETY.md`), so the
   owner's only other control path is the vendor app — which cannot connect while we are
   connected.
3. **It runs unattended.** Nobody is watching the terminal when the link dips 22 dB
   (RL-010) or the broker restarts.

Each of those is a decision, and the defaults are the whole decision: a safe option that
must be opted into is not a safe default.

## Decision 1 — Loopback by default, and non-loopback refuses to start without a token

`ServerConfig` binds `127.0.0.1`. Binding any other interface without a token raises
`ConfigurationRefused` **before the socket opens**.

Refusing rather than warning is the point. A warning about a heater API answering the LAN
would be read once, during setup, and scrolled past thereafter. The failure it prevents is
not exotic: a service bound `0.0.0.0` on a home network is reachable by every device on that
network, including the ones nobody is maintaining.

The token is compared with `secrets.compare_digest`, must be at least 16 characters, and is
accepted **only** in `Authorization: Bearer`. It is not accepted as a query parameter,
because URLs are logged by proxies, retained in shell history and stored in browser history
— a credential in a URL is a credential you have published somewhere you have not thought
about.

## Decision 2 — Browser-shaped requests are refused outright

Two checks, both cheap, both aimed at the same attack:

- Requests carrying an **`Origin`** header are rejected. Only browsers send it.
- The **`Host`** header must name something we recognise (loopback, the bind address, or a
  name explicitly allowed).

Together they close DNS rebinding. A page in an unrelated tab can point a hostname it
controls at `127.0.0.1` and then issue requests to this port *from inside the machine*,
where "loopback only" provides no protection at all. Such a request carries the attacker's
name in `Host` and an `Origin`; a request genuinely addressed to us has neither.

Our own clients — Jarvis, `curl`, a script, an automation — send neither header, so this
costs nothing. Browser access is not a use case being traded away; there is no browser UI,
and if one is ever built it will need a deliberate CORS decision of its own rather than
inheriting one made silently here.

## Decision 3 — The link is leased, and the lease is part of the API

`POST /api/v1/link/yield` (and the MQTT `set/link_yield` topic) releases the BLE link for a
stated number of seconds and blocks reconnection until it expires.

This is not a convenience. Without it, starting the service takes the owner's heater away
from them until they find and kill a process — on a device whose physical remote does not
work, in a bedroom, possibly at night. Both `bedjet serve` and `bedjet mqtt` print the yield
command on startup for that reason: the person who most needs it is the least likely to have
read the API reference.

The link is held, not owned. Every design choice downstream follows from that ordering.

## Decision 4 — Three failure modes, never flattened

`Refused` (409), `Unavailable` (503) and `Unverified` (502) stay distinct all the way to the
wire, and the MQTT bridge publishes the same distinction to `.../result`.

`Unverified` is the one that justifies the effort. It means bytes were written and the
device did not visibly obey — which may mean it obeyed and the confirmation was lost, since
this protocol has no acknowledgement (RL-018). A client that cannot tell that from "nothing
was sent" will retry the wrong one, and retrying a command against a heater that may have
already accepted it is exactly the case worth being careful about.

A satisfied request is not a failure: asking an off heater to switch off returns
`ok=true, changed=false` with `sent: null`. Automations need idempotence; the null says
plainly that nothing was written and therefore nothing was verified.

## Decision 5 — The first connection fails loudly; later ones retry quietly

`DeviceSession.start()` awaits the first connection and raises the transport's own
diagnosis. A wrong address, an unregistered device, or a vendor app holding the link should
stop the daemon at boot with a message, not disappear into a retry loop that reports
nothing. Every *subsequent* drop is supervised and retried with backoff, because after the
first success we know the device exists and a dip is a dip.

Under a process supervisor, `Restart=on-failure` gives the retry-at-boot behaviour for
anyone who wants it — as an explicit choice, at the layer that owns process lifecycle.

## Consequences

- There is no way to expose this service on a network without stating a secret, and no way
  to do it by accident.
- A browser cannot drive it. That is a deliberate limitation, revisitable with its own ADR.
- The owner can always reclaim the device without stopping the service.
- Two extras (`http`, `mqtt`) instead of one dependency set: a Pi running only the MQTT
  bridge does not install an HTTP server.

## Not decided here

- **TLS.** Loopback does not need it and the LAN case is served by a token over a trusted
  network. A deployment that needs transport security should terminate it in front.
- **Multi-device.** One session, one device. The topic layout and the `device_id` leave room
  for more, but nothing here has been designed for it and pretending otherwise would be the
  kind of unverified claim this repository exists to avoid.
