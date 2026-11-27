"""End-to-end UI test: the real extension page, the real native host, and a
real slapd, driven with Playwright's Firefox.

The page is served over HTTP with a tiny `browser.*` shim whose
connectNative() relays messages to a host subprocess through /native, so
everything except Firefox's own native-messaging plumbing is exercised.

Run with:  pytest tests/test_ui.py   (needs `pip install playwright` and
`playwright install firefox`). Set UI_SHOTS=dir to keep screenshots.
"""

import json
import os
import struct
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
HOST = ROOT / "host" / "ldap_browser_host.py"

SHIM = """
(() => {
  const data = {};
  window.browser = {
    runtime: {
      getURL: (p) => new URL(p, location.href).href,
      connectNative() {
        const onMsg = [], onDisc = [];
        return {
          onMessage: { addListener: (f) => onMsg.push(f) },
          onDisconnect: { addListener: (f) => onDisc.push(f) },
          postMessage(msg) {
            fetch("/native", { method: "POST", body: JSON.stringify(msg) })
              .then((r) => r.json())
              .then((frames) => frames.forEach((fr) => onMsg.forEach((f) => f(fr))));
          },
          disconnect() {},
        };
      },
    },
    storage: { local: {
      async get(k) { return { [k]: data[k] }; },
      async set(o) { Object.assign(data, o); },
    } },
  };
})();
"""


class Bridge:
    """Relays framed messages to one native host subprocess."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(HOST)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self.lock = threading.Lock()

    def send(self, msg):
        data = json.dumps(msg).encode()
        with self.lock:
            self.proc.stdin.write(struct.pack("@I", len(data)) + data)
            self.proc.stdin.flush()
            frames = []
            while True:
                (n,) = struct.unpack("@I", self.proc.stdout.read(4))
                frame = json.loads(self.proc.stdout.read(n))
                frames.append(frame)
                if "chunk" not in frame or len(frames) == frame["of"]:
                    return frames

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def make_handler(bridge):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            out = json.dumps(bridge.send(json.loads(body))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    return partial(Handler, directory=str(EXT))


@pytest.fixture
def page(slapd):
    bridge = Bridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bridge))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with pw.sync_playwright() as p:
        browser = p.firefox.launch()
        ctx = browser.new_context(viewport={"width": 1360, "height": 860}, color_scheme=os.environ.get("UI_SCHEME", "light"))
        ctx.add_init_script(SHIM)
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{server.server_address[1]}/browser.html")
        pg.errors = errors
        yield pg
        browser.close()
    server.shutdown()
    bridge.close()


def shot(page, name):
    d = os.environ.get("UI_SHOTS")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"))


def test_browse_entry_and_search(page, slapd):
    page.fill("input[name=host]", f"ldap://127.0.0.1:{slapd['port']}")
    page.select_option("select[name=security]", "none")
    page.fill("input[name=user]", slapd["admin"])
    page.fill("input[name=password]", slapd["password"])
    page.fill("input[name=profileName]", "Test slapd")
    page.click("#btn-save")
    assert page.locator("#profile-list li").count() == 1
    assert page.text_content("#profile-list .phost") == f"ldap://127.0.0.1:{slapd['port']}"
    assert page.locator("#plain-warning").is_visible()
    shot(page, "1-connect")

    page.click("#btn-connect")
    page.wait_for_selector("#view-browse:not([hidden])")
    assert "127.0.0.1" in page.text_content("#status-server")
    assert page.text_content("#status-vendor") == "OpenLDAP"

    # Root is auto-expanded and selected.
    page.wait_for_selector("#tree .row:has-text('People')")
    page.click("#tree .row:has-text('People')")
    page.wait_for_selector("#tree .row:has-text('alice')")
    page.click("#tree .row:has-text('alice')")
    page.wait_for_selector("#entry-rdn:has-text('uid=alice')")
    page.wait_for_selector("#attr-body td.name:has-text('mail')")
    assert "Álice in Wönderland" in page.text_content("#attr-body")
    assert "binary · 13 bytes" in page.text_content("#attr-body")  # jpegPhoto (not a real image)
    assert not page.locator("#attr-body tr[data-name=createtimestamp]").is_visible()
    page.check("#show-operational")
    assert page.locator("#attr-body tr[data-name=createtimestamp]").is_visible()
    shot(page, "2-entry")

    # Paging: Bulk has 1200 children -> "Load more…" after the first 500.
    page.click("#tree .row:has-text('Bulk')")
    page.wait_for_selector("#tree li.more")
    assert page.locator("#tree .row:has-text('user0')").count() == 500
    page.click("#tree li.more .row")
    page.wait_for_function("document.querySelectorAll('#tree li.more').length === 1 && document.querySelectorAll('#tree .row[title^=\"uid=user\"]').length === 1000")
    page.click("#tree li.more .row")
    page.wait_for_function("document.querySelectorAll('#tree .row[title^=\"uid=user\"]').length === 1200")
    assert page.locator("#tree li.more").count() == 0

    # Group member values are DN links that navigate the tree.
    page.fill("#goto-dn", f"cn=Admins,ou=Groups,{slapd['base']}")
    page.press("#goto-dn", "Enter")
    page.wait_for_selector("#entry-rdn:has-text('cn=Admins')")
    page.click("#attr-body button.link:has-text('uid=bob')")
    page.wait_for_selector("#entry-rdn:has-text('uid=bob')")
    assert "selected" in page.get_attribute("#tree .row[title^='uid=bob']", "class")

    # Search tab.
    page.click(".tabs button[data-tab=search]")
    page.fill("#search-form input[name=filter]", "(sn=Bulk)")
    page.fill("#search-form input[name=attributes]", "uid, cn")
    page.fill("#search-form input[name=limit]", "50")
    page.click("#btn-search")
    page.wait_for_selector("#search-status:has-text('50 entries')")
    assert "limit reached" in page.text_content("#search-status")
    assert page.locator("#results tbody tr").count() == 50
    shot(page, "3-search")

    page.fill("#search-form input[name=filter]", "(cn=unbalanced")
    page.click("#btn-search")
    page.wait_for_selector("#search-error:not([hidden])")

    # Server and schema tabs.
    page.click(".tabs button[data-tab=server]")
    assert "namingContexts" in page.text_content("#rootdse-body")
    shot(page, "4-server")
    page.click(".tabs button[data-tab=schema]")
    page.wait_for_selector("#schema-list li")
    page.fill("#schema-filter", "inetOrgPerson")
    page.click("#schema-list li:text-is('inetOrgPerson')")
    assert "May contain" in page.text_content("#schema-detail")
    shot(page, "5-schema")

    page.click("#btn-disconnect")
    page.wait_for_selector("#view-connect:not([hidden])")
    assert page.errors == []


def test_bad_password_shows_error(page, slapd):
    page.fill("input[name=host]", "127.0.0.1")
    page.fill("input[name=port]", str(slapd["port"]))
    page.select_option("select[name=security]", "none")
    page.fill("input[name=user]", slapd["admin"])
    page.fill("input[name=password]", "nope")
    page.click("#btn-connect")
    page.wait_for_selector("#connect-error:not([hidden])")
    assert "Bind failed" in page.text_content("#connect-error")
    assert page.errors == []


def test_protocol_handler_url_prefills_form(page):
    """Firefox opens ext+ldaps://... links as browser.html#<encoded url>."""
    from urllib.parse import quote

    base = page.url.split("#")[0]
    page.goto(base + "#" + quote("ext+ldaps://dc01.corp.example:3269/DC=corp,DC=example", safe=""))
    page.wait_for_function("document.querySelector('input[name=host]').value === 'dc01.corp.example'")
    assert page.input_value("input[name=port]") == "3269"
    assert page.input_value("select[name=security]") == "ldaps"
    assert page.input_value("input[name=base]") == "DC=corp,DC=example"

    page.goto(base + "#" + quote("ext+ldap://ldap.example.org", safe=""))
    page.wait_for_function("document.querySelector('input[name=host]').value === 'ldap.example.org'")
    assert page.input_value("select[name=security]") == "starttls"
    assert page.errors == []
