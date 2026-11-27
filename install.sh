#!/usr/bin/env bash
# Installs the LDAP Browser native helper for Firefox (macOS and Linux).
#
#   ./install.sh            install / update
#   ./install.sh --uninstall
#
# It creates a private Python virtualenv in host/.venv, installs ldap3 into
# it, and registers host/ldap_browser_host with Firefox via a native
# messaging manifest in your home directory. No root access is needed.
set -euo pipefail

NAME="ldap_browser_host"
EXT_ID="ldap-browser@ericwb.github.io"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_DIR="$ROOT/host"
VENV="$HOST_DIR/.venv"
LAUNCHER="$HOST_DIR/$NAME"

case "$(uname -s)" in
  Darwin) MANIFEST_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts" ;;
  Linux)  MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts" ;;
  *) echo "Unsupported OS: $(uname -s). On Windows run install.ps1." >&2; exit 1 ;;
esac
MANIFEST="$MANIFEST_DIR/$NAME.json"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$MANIFEST" "$LAUNCHER"
  rm -rf "$VENV"
  echo "Removed $MANIFEST and the helper's virtualenv."
  exit 0
fi

# Oldest Python release that is still supported upstream (3.10 reached
# end-of-life on 2026-10-01). Bump this as releases age out:
# https://devguide.python.org/versions/
MIN_MINOR=11

supported() {
  "$1" -c "import sys; sys.exit(not (sys.version_info[0] == 3 and sys.version_info[1] >= $MIN_MINOR))" 2>/dev/null
}

PYTHON="${PYTHON:-}"
if [[ -n "$PYTHON" ]]; then
  supported "$PYTHON" || { echo "$PYTHON is older than Python 3.$MIN_MINOR (end-of-life). Install a supported Python." >&2; exit 1; }
else
  # Prefer the newest installed release.
  for p in python3.15 python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$p" >/dev/null && supported "$p"; then
      PYTHON="$(command -v "$p")"; break
    fi
  done
fi
[[ -n "$PYTHON" ]] || {
  echo "Python 3.$MIN_MINOR or newer is required; older releases are end-of-life." >&2
  echo "Install a current Python (e.g. 'brew install python' or python.org), or set PYTHON=/path/to/python3." >&2
  exit 1
}

echo "Using $("$PYTHON" --version) at $PYTHON"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$HOST_DIR/requirements.txt"

# Firefox starts the helper with a minimal environment, so the launcher uses
# absolute paths and runs Python isolated from the user's site/env settings.
cat > "$LAUNCHER" <<SH
#!/bin/sh
exec "$VENV/bin/python" -I "$HOST_DIR/ldap_browser_host.py" "\$@"
SH
chmod 755 "$LAUNCHER"

mkdir -p "$MANIFEST_DIR"
"$VENV/bin/python" - "$MANIFEST" "$LAUNCHER" "$EXT_ID" <<'PY'
import json, sys
manifest, launcher, ext = sys.argv[1:]
with open(manifest, "w") as f:
    json.dump({
        "name": "ldap_browser_host",
        "description": "LDAP Browser native helper",
        "path": launcher,
        "type": "stdio",
        "allowed_extensions": [ext],
    }, f, indent=2)
    f.write("\n")
PY

# Smoke test: send a "hello" message through the launcher.
"$VENV/bin/python" - "$LAUNCHER" <<'PY'
import json, struct, subprocess, sys
msg = json.dumps({"id": 1, "op": "hello"}).encode()
out = subprocess.run([sys.argv[1]], input=struct.pack("@I", len(msg)) + msg,
                     capture_output=True, timeout=30).stdout
(n,) = struct.unpack("@I", out[:4])
reply = json.loads(out[4:4 + n])
assert reply["ok"], reply
print("Helper OK: ldap3 %s on Python %s" % (reply["result"]["ldap3"], reply["result"]["python"]))
PY

echo "Registered $MANIFEST"
echo
echo "Next: load the extension in Firefox:"
echo "  about:debugging#/runtime/this-firefox → Load Temporary Add-on… → $ROOT/extension/manifest.json"
