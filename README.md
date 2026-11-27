# LDAP Browser for Firefox

A Firefox add-on for browsing Active Directory and other LDAP directories
(OpenLDAP, 389 DS, FreeIPA, and so on) in a browser tab:

- **Tree view** of the directory. Children load as you expand, and large
  containers page in 500 at a time ("Load more…").
- **Entry view** lists every attribute. DN-valued attributes (`member`,
  `memberOf`, `manager`, …) are links. Binary values have hex and base64 views,
  and `jpegPhoto`/`thumbnailPhoto` are shown as images.
- **Active Directory decoding**: `objectSid`, `objectGUID`, FILETIME timestamps
  (`pwdLastSet`, `lastLogonTimestamp`, `accountExpires`, …), password-policy
  intervals, `userAccountControl`/`groupType` flags and `sAMAccountType`.
- **Search** with any LDAP filter, scope, attribute list and limit. It has
  presets for common AD queries (disabled or locked accounts, SPNs, delegation,
  nested group membership, …) and exports to CSV or LDIF.
- **Server tab** shows the Root DSE (vendor, functional levels, SASL
  mechanisms, naming contexts). The **Schema tab** lets you browse object
  classes and attribute types.
- Connections can be **saved** in the add-on's storage. Passwords are never
  stored.
- The add-on is **read-only** and never modifies the directory.

## How it works

Firefox extensions can't open raw TCP connections, so they can't speak LDAP on
ports 389/636 themselves. The add-on uses
[native messaging](https://developer.mozilla.org/docs/Mozilla/Add-ons/WebExtensions/Native_messaging)
to talk to a small local Python helper (`host/ldap_browser_host.py`, built on
[ldap3](https://github.com/cannatag/ldap3)), which makes the LDAP connection:

```
browser.html ──JSON over stdio──▶ ldap_browser_host.py ──LDAP/LDAPS──▶ directory server
```

Each LDAP Browser tab starts its own helper process, so each tab has its own
connection. Only this add-on's ID is allowed to start the helper.

## Install

Requires Firefox 115+ and Python 3.8+.

1. Install the helper. This creates `host/.venv`, installs ldap3 into it, and
   registers the helper with Firefox. No admin rights are needed.

   ```sh
   ./install.sh                 # macOS / Linux
   ```
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1   # Windows
   ```

2. Load the add-on. Open `about:debugging#/runtime/this-firefox`, click
   **Load Temporary Add-on…**, and select `extension/manifest.json`.
   Temporary add-ons are removed when Firefox restarts. For a permanent
   install, sign it with `npx web-ext sign` (an unlisted AMO signature is
   free), or use Firefox Developer Edition/Nightly with
   `xpinstall.signatures.required=false`.

To uninstall the helper, run `./install.sh --uninstall` (or `install.ps1 -Uninstall`).

## Use

- Click the toolbar button to open a new LDAP Browser tab, or
- type `ldap` and a space in the address bar, then a server, for example
  `ldap dc01.corp.example` or
  `ldap ldaps://dc01.corp.example:636/DC=corp,DC=example`.

### Opening servers from links: `ext+ldap://` and `ext+ldaps://`

The add-on registers the `ext+ldap` and `ext+ldaps` URL schemes. Typing
`ext+ldaps://dc01.corp.example:636/DC=corp,DC=example` in the address bar, or
clicking a link like it, opens LDAP Browser with that server, port and base DN
filled in. The first time, Firefox asks which application should handle the
link; choose **LDAP Browser** and tick "Always use".

Plain `ldap://` and `ldaps://` can't be claimed. Firefox only lets extensions
register a fixed list of schemes (`mailto`, `irc`, `ssh`, …) plus custom
`ext+`/`web+` ones, and it sends anything else to the operating system.

### Connecting to Active Directory

| Setting        | Typical value |
|----------------|---------------|
| Server         | a domain controller, e.g. `dc01.corp.example` (port 3268/3269 for the Global Catalog) |
| Security       | **LDAPS** (636) or **StartTLS** (389). Use your corporate root CA file if Python doesn't trust it |
| Authentication | **Simple bind** as `user@corp.example`, or **NTLM** as `CORP\user` |

AD often refuses simple binds over unencrypted connections ("strongerAuthRequired"),
so use LDAPS/StartTLS, or NTLM, which signs the session.

### Certificates

With **Verify the server certificate** turned on, the helper uses Python's
default trust store plus any CA file you give it. Turning verification off
still encrypts the connection, but it no longer proves which server you're
talking to. Only do that on networks you trust.

## Development

```sh
python3 -m venv .venv-dev && .venv-dev/bin/pip install -r host/requirements.txt pytest playwright
.venv-dev/bin/playwright install firefox     # only for the UI tests
.venv-dev/bin/pytest                         # host unit + integration + UI tests
node --test tests/*.test.mjs                 # DN / LDIF / CSV helpers
```

The integration tests start a throwaway `slapd` (bundled on macOS at
`/usr/libexec/slapd`; `apt install slapd` on Linux) seeded with 1,200 entries
to exercise paging. Apple's slapd can't load PEM certificates, so LDAPS is
tested through a small Python TLS proxy, and the StartTLS test is skipped
there. The UI test serves `extension/` over HTTP with a `browser.*` shim that
relays native messages to the real helper, then drives it with Playwright.

Set `UI_SHOTS=/some/dir` to save screenshots, and `UI_SCHEME=dark` to test
dark mode.

### Layout

```
extension/
  manifest.json     MV3 manifest (nativeMessaging, storage, omnibox "ldap",
                    ext+ldap / ext+ldaps protocol handlers)
  background.js     toolbar button + address-bar keyword
  browser.html/.css/.js  the browser UI
  native.js         promise client for the native port (reassembles >1 MB replies)
  ldap-util.js      DN parsing, ldap:// URLs, LDIF and CSV export
host/
  ldap_browser_host.py   the native helper (ldap3)
  requirements.txt
install.sh / install.ps1
tests/
```

### Helper protocol

The extension sends requests as `{"id": n, "op": "...", ...}` and gets back
`{"id": n, "ok": true, "result": ...}` or `{"id": n, "ok": false, "error": "..."}`.
Replies over ~768 KB are split into `{"id", "chunk", "of", "data"}` frames,
because Firefox limits messages from a native host to 1 MB.

| op           | params |
|--------------|--------|
| `hello`      | none |
| `connect`    | `host, port, security (none/ldaps/starttls), verifyCert, caFile, auth (simple/ntlm/anonymous), user, password, timeout` |
| `children`   | `dn, pageSize, cookie` |
| `entry`      | `dn` |
| `search`     | `base, filter, scope (base/one/sub), attributes[], limit` |
| `rootdse`, `schema`, `disconnect` | none |
