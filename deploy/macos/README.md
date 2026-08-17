# Running the daemon unattended on macOS

`bedjet serve` works when you launch it from a terminal you have granted Bluetooth, and dies instantly under
`launchd`, `cron`, or any agent harness — **SIGABRT, exit 134, no Python traceback**. The
cause is not in this repo's code. macOS attributes CoreBluetooth to the *responsible app
bundle* rather than to the executing binary, and when neither the responsible app nor the
binary carries `NSBluetoothAlwaysUsageDescription`, TCC does not prompt and does not deny —
it kills the process. A terminal works only because *it* is allowed to prompt and holds a grant,
which everything it spawns inherits — Terminal.app via an Apple-private entitlement, iTerm2 via
the usage-description key it ships (RL-028). An ad-hoc bundle of ours can only take the second
route, which is what this directory does. Full account: **RL-025** and **RL-028** in
[`docs/research/RESEARCH-LOG.md`](../../docs/research/RESEARCH-LOG.md).

This directory is the launch arrangement that fixes it: a minimal app bundle that carries
the key, plus a LaunchAgent that starts the daemon *through* it.

> **Status: ✅ the grant works under `launchd`. ❓ the login-time *spawn* is unverified, and
> ⛔ there is no working automated stop.**
>
> Double-clicked from Finder, the bundle obtains the Bluetooth grant and the `uv` → Python child
> inherits it (**RL-029**, with RL-025 as the matched control). Started by `launchd` via
> `launchctl kickstart`, the same thing happens — connected on attempt 1 in ~1 s and served state
> (**RL-030**). So the responsible-process trick works when a daemon initiates it, not only a
> human, which was the substance of
> [#9](https://github.com/blamechris/bedjet-local/issues/9) item 3.
>
> Two things are still open, and the second is worse than the first:
>
> 1. **The spawn at login has not been observed.** RL-030 found `bootstrap` registering the job
>    with `RunAtLoad` and never running it (`runs = 0`), for reasons not yet understood. That
>    failure is *silent*, so it cannot be assumed away.
> 2. **`launchctl` cannot stop this daemon** — see the stop section below. A reboot or logout
>    therefore does not release the BLE link gracefully.
>
> Treat step 4 as untested and read both warnings before installing it.

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

## ⛔ Stopping it: `launchctl bootout` does not work, and cannot ([#16](https://github.com/blamechris/bedjet-local/issues/16))

Removing the job unregisters it and **leaves the daemon running with the device still held**:

```bash
launchctl bootout gui/$(id -u)/local.bedjet.daemon   # exits 0, and stops nothing
```

Measured in RL-030: `bootout` returned 0, the job left the domain, and six seconds later the
daemon was still connected with no `Link released` in the log.

The cause is the same design decision that makes Bluetooth work at all, so it is not a bug to
be patched here. `ProgramArguments` runs `/usr/bin/open` so that **LaunchServices**, not
`launchd`, is the responsible process — that is the whole trick (RL-025). But `open` hands off
and exits immediately, which is also why `KeepAlive` is `false`. By the time the daemon is up,
launchd's job has already finished (`runs = 1`, `last exit code = 0`) and the daemon is
reparented to **PID 1**. There is nothing left for launchd to signal.

**To actually stop it, signal the process:**

```bash
pkill -TERM -f "bedjet serve"
```

That produces the clean shutdown — `SIGTERM received — releasing the link.` followed by
`Link released — the vendor app can connect again.` RL-030 verified that sequence against the
real device by signalling the `uv` PID directly; the `pkill` form above was checked separately
for pattern and delivery — it matches **both** the `uv` wrapper and the Python child, and each
receives the signal. Confirm it anyway before walking away, because the point of this section is
that a stop command returning 0 proves nothing:

```bash
tail -5 ~/Library/Logs/bedjet-daemon.log
```

The signal crosses one intermediary: `launcher.sh` `exec`s `uv run`, and `uv` spawns the
interpreter as a child, so SIGTERM arrives at `uv` rather than at Python. **`uv` forwards it** —
measured synthetically in #14 and then against the live link in RL-030, where signalling the
`uv` PID ran Python's handler and released the device. Worth re-checking if the launcher ever
stops going through `uv`.

**The operational consequence, which is the reason this is an issue and not a footnote:** every
automated stop goes through the path that does not work. A reboot, a logout, or any supervisor
will take the daemon down without reaching the #12 handler, so the link is dropped rather than
released. The #12 fix is correct — it simply has no working trigger except a manual signal.

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
from outside an already-granted terminal — and they carry none of this consequence.

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
- **Process supervision — in either direction.** `open` returns immediately, so `KeepAlive`
  would spin rather than supervise, and nothing here restarts the daemon if it exits. RL-030
  showed the same detachment also makes the daemon **unstoppable by `launchctl`**, which is the
  more urgent half: the graceful release exists and no automated path reaches it. The daemon
  supervises its own BLE link (ADR-0004, decision 5); its *process* is supervised by nobody.
- **The Pi.** None of this applies to Linux — BlueZ has no TCC. Weighed in
  [ADR-0005](../../docs/decisions/ADR-0005-milestone-4-scope-and-siting.md).
