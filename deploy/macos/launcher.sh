#!/bin/sh
# Main executable of BedJetDaemon.app. See deploy/macos/README.md.
#
# This file is sealed into the app bundle's code signature, and TCC pins the
# Bluetooth grant to that seal (an ad-hoc bundle's designated requirement is a
# bare `cdhash`). Editing this script therefore revokes the grant, and the
# daemon goes back to dying with SIGABRT under launchd — see RL-025.
#
# So: nothing that varies belongs in here. No device address, no token, no repo
# path, no host, no port. All of it is read at runtime from the config file
# below, which lives outside the bundle precisely so it can change without
# breaking the seal. The log path is fixed for the same reason it is not
# configurable: a config error has to land somewhere findable, and that
# somewhere cannot itself come from the config.

set -eu

CONFIG="${HOME}/.config/bedjet/daemon.env"
LOG="${HOME}/Library/Logs/bedjet-daemon.log"

mkdir -p "$(dirname "${LOG}")"
exec >>"${LOG}" 2>&1

log() { printf '%s bedjet-launcher: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

if [ ! -f "${CONFIG}" ]; then
    log "no config at ${CONFIG} — see deploy/macos/README.md"
    exit 78 # EX_CONFIG
fi

# shellcheck disable=SC1090
. "${CONFIG}"

: "${BEDJET_REPO:?is not set in the config}"
: "${BEDJET_ADDRESS:?is not set in the config}"

if [ ! -d "${BEDJET_REPO}" ]; then
    log "BEDJET_REPO does not exist: ${BEDJET_REPO}"
    exit 78
fi

# BEDJET_TOKEN is deliberately passed through the environment rather than as an
# argument: `serve --token` is visible in the process list. The API refuses a
# non-loopback bind without one on its own (ADR-0004), so this does not check.
[ -n "${BEDJET_TOKEN:-}" ] && export BEDJET_TOKEN

# `uv` is not on launchd's PATH, which is not the login shell's PATH.
PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
export PATH

log "starting: repo=${BEDJET_REPO} host=${BEDJET_HOST:-127.0.0.1} port=${BEDJET_PORT:-8787}"

# BEDJET_EXTRA_ARGS is intentionally unquoted — it is a list of arguments.
# shellcheck disable=SC2086
exec uv run --directory "${BEDJET_REPO}" bedjet serve \
    "${BEDJET_ADDRESS}" \
    --host "${BEDJET_HOST:-127.0.0.1}" \
    --port "${BEDJET_PORT:-8787}" \
    ${BEDJET_EXTRA_ARGS:-}
