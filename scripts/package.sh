#!/usr/bin/env bash
# Build release artifacts into dist/:
#
#   ldap-browser-<ver>.xpi              the Firefox add-on (unsigned)
#   ldap-browser-helper-<ver>.tar.gz    native helper + installers (macOS/Linux)
#   ldap-browser-helper-<ver>.zip       the same, for Windows
#   *.sha256                            checksums
#
# Usage: scripts/package.sh [VERSION]
# VERSION defaults to the one in extension/manifest.json. The source tree is
# never modified; the version is stamped into a staged copy of the manifest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-}"
VERSION="${VERSION#v}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(python3 -c 'import json; print(json.load(open("extension/manifest.json"))["version"])')"
fi
# Firefox add-on versions are 1-4 dot-separated integers (no pre-release tags).
if ! [[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){0,3}$ ]]; then
  echo "error: '$VERSION' is not a valid add-on version (e.g. 1.2.3)" >&2
  exit 1
fi

DIST="$ROOT/dist"
STAGE="$DIST/stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/extension"
rm -f "$DIST"/ldap-browser-*

# --- add-on ------------------------------------------------------------------
cp -R extension/. "$STAGE/extension/"
python3 - "$STAGE/extension/manifest.json" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1:]
manifest = json.load(open(path))
manifest["version"] = version
with open(path, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PY
XPI="$DIST/ldap-browser-$VERSION.xpi"
# Fixed timestamps and file order make the archive reproducible.
find "$STAGE/extension" -exec touch -t 198001010000 {} +
(cd "$STAGE/extension" && find . -type f ! -name '.*' | LC_ALL=C sort | sed 's|^\./||' | zip -X -q "$XPI" -@)

# --- helper bundle -----------------------------------------------------------
NAME="ldap-browser-helper-$VERSION"
B="$STAGE/$NAME"
mkdir -p "$B/host"
cp install.sh install.ps1 README.md "$B/"
cp host/ldap_browser_host.py host/requirements.txt "$B/host/"
[[ -f LICENSE ]] && cp LICENSE "$B/"
chmod 755 "$B/install.sh"
find "$B" -exec touch -t 198001010000 {} +
tar -C "$STAGE" -czf "$DIST/$NAME.tar.gz" "$NAME"
(cd "$STAGE" && find "$NAME" -type f | LC_ALL=C sort | zip -X -q "$DIST/$NAME.zip" -@)

# --- checksums -----------------------------------------------------------------
cd "$DIST"
for f in ldap-browser-"$VERSION".xpi "$NAME".tar.gz "$NAME".zip; do
  shasum -a 256 "$f" > "$f.sha256"
done
ls -l "$DIST"/ldap-browser-*
