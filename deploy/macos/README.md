# Running the daemon unattended on macOS

`bedjet serve` works when you launch it from Terminal.app and dies instantly under
`launchd`, `cron`, or any agent harness — **SIGABRT, exit 134, no Python traceback**. The
cause is not in this repo's code. macOS attributes CoreBluetooth to the *responsible app
bundle* rather than to the executing binary, and when neither the responsible app nor the
binary carries `NSBluetoothAlwaysUsageDescription`, TCC does not prompt and does not deny —
it kills the process. Terminal.app works only because it ships that key and holds a grant,
which everything it spawns inherits. Full account: **RL-025** in
[`docs/research/RESEARCH-LOG.md`](../../docs/research/RESEARCH-LOG.md).

This directory is the launch arrangement that fixes it: a minimal app bundle that carries
the key, plus a LaunchAgent that starts the daemon *through* it.

> **Status: unverified.** Everything below is built and its signing behaviour is measured,
> but no part of it has yet run against the BedJet or survived a real login. Per
> [`AGENTS.md`](../../AGENTS.md) rule 6, treat the TCC mechanism as ❓ HYPOTHESIS until
> RL-027 records an observation. What *is* verified is the cdhash behaviour in
> "Why configuration lives outside the bundle", which was measured directly.

## What is here

| | |
|---|---|
| `Info.plist` | Carries `NSBluetoothAlwaysUsageDescription`. The reason the bundle exists. |
| `launcher.sh` | The bundle's main executable. Fixed content; reads everything from config. |
| `build-app.sh` | Assembles and ad-hoc signs `BedJetDaemon.app`. Deterministic. |
| `local.bedjet.daemon.plist` | LaunchAgent template. Launches the *app*, not the executable. |

## Why configuration lives outside the bundle

An ad-hoc signed bundle's designated requirement is a bare content hash:

```
# designated => cdhash H"d150d18103231f6fcfa79c192a1322023dc4f5f0"
```

TCC pins its grant to that hash, and the hash covers everything inside the bundle. Measured
on this machine:

- rebuilding an **identical** bundle reproduces the **same** cdhash — a `git pull` and
  rebuild keeps the grant;
- changing **one byte** of `launcher.sh` produces a **different** cdhash — macOS then sees an
  app it has never granted anything to.

The consequence is the design: a device address, a token, a repo path or a port inside the
bundle would mean **reconfiguring the daemon silently revokes its right to use Bluetooth**,
and the symptom would be the SIGABRT this whole directory exists to eliminate — at whatever
hour the machine next rebooted. So `launcher.sh` holds no settings at all. They live in
`~/.config/bedjet/daemon.env`, outside the seal, free to change.

The log path is fixed rather than configurable for the mirror-image reason: a config error has
to land somewhere findable, and that somewhere cannot come from the config.

## First run

**1. Write the config.** Outside the repo, and note that it holds a secret — never move it
into a checkout:

```bash
mkdir -p ~/.config/bedjet && touch ~/.config/bedjet/daemon.env && chmod 600 ~/.config/bedjet/daemon.env
```

Then put this in it, with your own address from `devices.local.toml`:

```sh
BEDJET_REPO=<absolute path to this checkout>
BEDJET_ADDRESS=<the host-local UUID from devices.local.toml>
BEDJET_HOST=127.0.0.1
BEDJET_PORT=8787
# Only needed to bind beyond loopback — see the tailnet section.
# BEDJET_TOKEN=...
# BEDJET_EXTRA_ARGS="--settle 10"
```

`BEDJET_ADDRESS` is host-local on macOS (RL-005) and belongs here rather than in a committed
file. `BEDJET_TOKEN` is read from the environment, not passed as an argument, because
`--token` would be visible in the process list.

**2. Build the bundle.**

```bash
./deploy/macos/build-app.sh
```

It prints the cdhash. Keep it; a changed cdhash on a later build is the one signal that the
grant is about to be lost.

**3. Grant Bluetooth, from a login session.** This step must be done at the keyboard — a
background launch has no way to present the consent prompt.

```bash
open -a ~/Applications/BedJetDaemon.app
```

Approve the Bluetooth prompt. Then confirm the daemon actually came up, rather than assuming
the absence of a prompt means success:

```bash
tail -f ~/Library/Logs/bedjet-daemon.log
```

A `SIGABRT`/exit 134 with no traceback means TCC killed it again; look for `namespace: TCC`
in the crash report rather than debugging the BLE code (RL-025).

**4. Only then, install the LaunchAgent** — see the warning below first.

```bash
sed "s|REPLACE_WITH_APP_PATH|$HOME/Applications/BedJetDaemon.app|" \
  deploy/macos/local.bedjet.daemon.plist > ~/Library/LaunchAgents/local.bedjet.daemon.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.bedjet.daemon.plist
```

To remove it:

```bash
launchctl bootout gui/$(id -u)/local.bedjet.daemon
```

## ⚠️ What `RunAtLoad` actually means here

The BedJet permits **one** BLE client at a time and this unit has **no working physical
remote** ([`docs/SAFETY.md`](../../docs/SAFETY.md)). Starting the daemon at login therefore
takes the heater away from the vendor app at every login, indefinitely, without anyone asking
for it — on a device in a bedroom.

The lease exists for exactly this and is the reason it is part of the API rather than a
convenience (ADR-0004, decision 3):

```bash
curl -X POST http://127.0.0.1:8787/api/v1/link/yield -d '{"seconds": 300}'
```

Do not install the LaunchAgent until something actually needs the daemon to be up
unprompted. Steps 1–3 are useful on their own — they are what make an attended launch work
from outside Terminal.app — and they carry none of this consequence.

## Binding beyond loopback (tailnet)

The fleet's transport is a Tailscale-shaped mesh. Binding to the tailnet address requires a
token and is otherwise refused before the socket opens (ADR-0004, decision 1):

```sh
BEDJET_HOST=<this machine's tailnet address>
BEDJET_TOKEN=<32+ url-safe bytes>
```

`Host` header checks mean a client addressing the daemon by tailnet *name* rather than
address needs that name allowed explicitly:

```sh
BEDJET_EXTRA_ARGS="--allow-host <this machine's tailnet name>"
```

Both are host-local facts and stay in `daemon.env`, which is outside any checkout — the same
convention RL-005 sets for the device address, and for the same reason.

## What this does not solve

- **Before login.** TCC grants are per-user and need a login session, so this is a
  LaunchAgent and there is no LaunchDaemon variant of it. A machine that rebooted and sat at
  the login window has no daemon.
- **Sleep.** A sleeping Mac is not serving anything. The fleet roadmap treats
  Mac-asleep as an open question, and this does not close it.
- **Process supervision.** `open` returns immediately, so `KeepAlive` would spin rather than
  supervise. The daemon supervises its own BLE link (ADR-0004, decision 5); nothing here
  restarts the daemon if it exits.
- **The Pi.** None of this applies to Linux — BlueZ has no TCC. Weighed in
  [ADR-0005](../../docs/decisions/ADR-0005-milestone-4-scope-and-siting.md).
