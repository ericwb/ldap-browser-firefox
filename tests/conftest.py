"""Test fixtures: spin up a throwaway OpenLDAP server (slapd) with TLS.

Requires a `slapd` binary (macOS ships /usr/libexec/slapd; on Linux install
the `slapd` package) and `openssl`. Tests that need it are skipped otherwise.
"""

import base64
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))

SLAPD_CANDIDATES = ["/usr/libexec/slapd", "/usr/sbin/slapd", shutil.which("slapd") or ""]
SCHEMA_DIRS = ["/etc/openldap/schema", "/etc/ldap/schema", "/usr/local/etc/openldap/schema"]

BASE = "dc=example,dc=test"
ADMIN = "cn=admin," + BASE
ADMIN_PW = "s3cret!"
BULK_USERS = 1200  # more than a 500/1000 page to exercise paging


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ssha(password):
    # Apple's slapd ignores cleartext rootpw values, so always hash it.
    salt = os.urandom(4)
    digest = hashlib.sha1(password.encode() + salt).digest()
    return "{SSHA}" + base64.b64encode(digest + salt).decode()


def _seed_ldif():
    lines = [
        f"dn: {BASE}",
        "objectClass: dcObject",
        "objectClass: organization",
        "o: Example",
        "dc: example",
        "",
        f"dn: {ADMIN}",
        "objectClass: organizationalRole",
        "objectClass: simpleSecurityObject",
        "cn: admin",
        f"userPassword: {_ssha(ADMIN_PW)}",
        "",
        f"dn: ou=People,{BASE}",
        "objectClass: organizationalUnit",
        "ou: People",
        "",
        f"dn: ou=Groups,{BASE}",
        "objectClass: organizationalUnit",
        "ou: Groups",
        "",
        f"dn: ou=Bulk,{BASE}",
        "objectClass: organizationalUnit",
        "ou: Bulk",
        "",
        f"dn: uid=alice,ou=People,{BASE}",
        "objectClass: inetOrgPerson",
        "uid: alice",
        "cn: Alice Liddell",
        "sn: Liddell",
        "mail: alice@example.test",
        "description:: w4FsaWNlIGluIFfDtm5kZXJsYW5k",  # base64 UTF-8 "Álice in Wönderland"
        "jpegPhoto:: /9j/4AAQSkZJRgABAQ==",
        "",
        f"dn: uid=bob,ou=People,{BASE}",
        "objectClass: inetOrgPerson",
        "uid: bob",
        "cn: Bob Builder",
        "sn: Builder",
        "",
        f"dn: cn=Admins,ou=Groups,{BASE}",
        "objectClass: groupOfNames",
        "cn: Admins",
        f"member: uid=alice,ou=People,{BASE}",
        f"member: uid=bob,ou=People,{BASE}",
        "",
    ]
    for i in range(BULK_USERS):
        lines += [
            f"dn: uid=user{i:04d},ou=Bulk,{BASE}",
            "objectClass: inetOrgPerson",
            f"uid: user{i:04d}",
            f"cn: User {i}",
            "sn: Bulk",
            "",
        ]
    return "\n".join(lines) + "\n"


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass


def _tls_proxy(cert, key, backend_port):
    """LDAPS front end for slapd builds that cannot load PEM certificates
    (Apple's slapd only reads TLS identities from the Keychain)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)

    def serve():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                tls = ctx.wrap_socket(client, server_side=True)
            except (OSError, ssl.SSLError):
                client.close()
                continue
            backend = socket.create_connection(("127.0.0.1", backend_port))
            threading.Thread(target=_pipe, args=(tls, backend), daemon=True).start()
            threading.Thread(target=_pipe, args=(backend, tls), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return listener


@pytest.fixture(scope="session")
def slapd(tmp_path_factory):
    binary = next((p for p in SLAPD_CANDIDATES if p and os.path.exists(p)), None)
    schema = next((d for d in SCHEMA_DIRS if os.path.exists(os.path.join(d, "core.schema"))), None)
    if not binary or not schema or not shutil.which("openssl"):
        pytest.skip("slapd/openssl not available")

    work = tmp_path_factory.mktemp("slapd")
    db = work / "db"
    db.mkdir()
    cert, key = work / "server.crt", work / "server.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )
    conf = work / "slapd.conf"
    conf.write_text(
        "\n".join(
            [
                f"include {schema}/core.schema",
                f"include {schema}/cosine.schema",
                f"include {schema}/inetorgperson.schema",
                f"pidfile {work}/slapd.pid",
                f"TLSCertificateFile {cert}",
                f"TLSCertificateKeyFile {key}",
                "sizelimit 2000",
                "database bdb",
                f"directory {db}",
                f'suffix "{BASE}"',
                f'rootdn "{ADMIN}"',
                f"rootpw {_ssha(ADMIN_PW)}",
                "access to attrs=userPassword by self write by anonymous auth by * none",
                "access to * by * read",
                "",
            ]
        )
    )
    ldif = work / "seed.ldif"
    ldif.write_text(_seed_ldif())
    subprocess.run([binary, "-T", "add", "-f", str(conf), "-l", str(ldif)], check=True, capture_output=True)

    port, tls_port = _free_port(), _free_port()
    proc = subprocess.Popen(
        [binary, "-f", str(conf), "-h", f"ldap://127.0.0.1:{port}/ ldaps://127.0.0.1:{tls_port}/", "-d", os.environ.get("SLAPD_DEBUG", "0")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("slapd exited: " + proc.stderr.read().decode())
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("slapd did not start")

    # Does slapd itself speak TLS? If not, front it with a Python TLS proxy.
    native_tls = False
    probe = socket.create_connection(("127.0.0.1", tls_port), timeout=2) if _listening(tls_port) else None
    if probe is not None:
        try:
            ctx = ssl.create_default_context(cafile=str(cert))
            ctx.wrap_socket(probe, server_hostname="localhost").close()
            native_tls = True
        except (OSError, ssl.SSLError):
            probe.close()
    proxy = None
    if not native_tls:
        proxy = _tls_proxy(cert, key, port)
        tls_port = proxy.getsockname()[1]

    yield {
        "host": "127.0.0.1",
        "port": port,
        "tls_port": tls_port,
        "base": BASE,
        "admin": ADMIN,
        "password": ADMIN_PW,
        "ca": str(cert),
        "bulk": BULK_USERS,
        "starttls": native_tls,
    }
    if proxy is not None:
        proxy.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
