#!/bin/bash
# Build (and ad-hoc sign) BedJetDaemon.app — the wrapper that lets the daemon
# use Bluetooth when it is not launched from Terminal.app. See README.md.
#
# The build is deterministic: identical inputs produce an identical cdhash, and
# TCC's grant is pinned to that cdhash. Rebuilding after `git pull` therefore
# keeps the Bluetooth grant as long as nothing under deploy/macos/ changed —
# and silently revokes it if something did. The script prints the cdhash on
# every run so that is visible rather than discovered at 2am.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${1:-${HOME}/Applications}"
app="${dest}/BedJetDaemon.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "build-app.sh: macOS only — this exists to satisfy macOS TCC (RL-025)" >&2
    exit 1
fi

mkdir -p "${dest}"
rm -rf "${app}"
mkdir -p "${app}/Contents/MacOS"

cp "${here}/Info.plist" "${app}/Contents/Info.plist"
cp "${here}/launcher.sh" "${app}/Contents/MacOS/BedJetDaemon"
chmod +x "${app}/Contents/MacOS/BedJetDaemon"

# Ad-hoc is enough: TCC records a grant against the resulting cdhash, and this
# bundle is never distributed, so there is nothing for a Developer ID to buy.
codesign --sign - --force "${app}"
codesign --verify --strict "${app}"

hash="$(codesign -dvvv "${app}" 2>&1 | sed -n 's/^CDHash=//p')"

cat <<EOF

built:  ${app}
cdhash: ${hash}

The Bluetooth grant is pinned to that cdhash. If it differs from the one in your
last successful run, macOS will treat this as a new app: expect a fresh consent
prompt, and grant it from a login session — not from a background launch, which
has no way to ask.

Next: deploy/macos/README.md, "First run".
EOF
