import base64
import io
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import ldap_browser_host as h

HOST_SCRIPT = Path(__file__).resolve().parent.parent / "host" / "ldap_browser_host.py"


# ---------------------------------------------------------------- decoders


def test_decode_sid():
    # S-1-5-21-1004336348-1177238915-682003330-512 (Domain Admins)
    subs = [21, 1004336348, 1177238915, 682003330, 512]
    raw = bytes([1, len(subs)]) + (5).to_bytes(6, "big") + b"".join(struct.pack("<I", s) for s in subs)
    assert h.decode_sid(raw) == "S-1-5-21-1004336348-1177238915-682003330-512"
    assert h.describe("objectSid", raw) == "S-1-5-21-1004336348-1177238915-682003330-512"


def test_decode_guid_is_little_endian():
    raw = bytes.fromhex("33221100554477668899aabbccddeeff")
    assert h.decode_guid(raw) == "00112233-4455-6677-8899-aabbccddeeff"


def test_filetime():
    assert h.describe("pwdLastSet", b"0") == "never"
    assert h.describe("accountExpires", b"9223372036854775807") == "never"
    # 2024-01-01 00:00:00 UTC
    assert h.describe("lastLogonTimestamp", b"133485408000000000") == "2024-01-01 00:00:00 UTC"


def test_interval_and_flags():
    assert h.describe("maxPwdAge", b"-36288000000000") == "42d"
    assert h.describe("userAccountControl", b"514") == "ACCOUNTDISABLE | NORMAL_ACCOUNT"
    assert h.describe("groupType", b"-2147483646") == "ACCOUNT_GROUP (global) | SECURITY_ENABLED"
    assert h.describe("sAMAccountType", b"805306368") == "SAM_USER_OBJECT"
    assert h.describe("whenCreated", b"20240131120000.0Z") == "2024-01-31 12:00:00 UTC"


def test_encode_value_binary_vs_text():
    assert h.encode_value("cn", "Ålice".encode()) == {"v": "Ålice"}
    jpeg = b"\xff\xd8\xff\xe0"
    assert h.encode_value("jpegPhoto", jpeg) == {"b64": base64.b64encode(jpeg).decode(), "size": 4}
    # objectGUID is binary even if it happens to be valid UTF-8
    guid = b"A" * 16
    enc = h.encode_value("objectGUID", guid)
    assert "b64" in enc and enc["d"] == "41414141-4141-4141-4141-414141414141"


def test_kind_and_rdn():
    assert h.kind_for(["top", "person", "user", "computer"]) == "computer"
    assert h.kind_for(["top", "organizationalUnit"]) == "ou"
    assert h.first_rdn("CN=Smith\\, John,OU=Staff,DC=corp,DC=example") == "CN=Smith\\, John"


# ---------------------------------------------------------------- framing


def frame(obj):
    data = json.dumps(obj).encode()
    return struct.pack("@I", len(data)) + data


def unframe_all(buf):
    out, pos = [], 0
    while pos < len(buf):
        (n,) = struct.unpack("@I", buf[pos : pos + 4])
        out.append(json.loads(buf[pos + 4 : pos + 4 + n]))
        pos += 4 + n
    return out


def test_large_messages_are_chunked():
    out = io.BytesIO()
    big = {"id": 7, "ok": True, "result": {"blob": "x" * (3 * h.MAX_MESSAGE)}}
    h.write_message(out, big)
    msgs = unframe_all(out.getvalue())
    assert len(msgs) > 1
    for m in msgs:
        assert len(json.dumps(m).encode()) < 1024 * 1024
    assert all(m["id"] == 7 and m["of"] == len(msgs) for m in msgs)
    assert json.loads("".join(m["data"] for m in sorted(msgs, key=lambda m: m["chunk"]))) == big


def test_unknown_op_and_hello():
    inp = io.BytesIO(frame({"id": 1, "op": "hello"}) + frame({"id": 2, "op": "nope"}) + frame({"id": 3, "op": "children"}))
    out = io.BytesIO()
    h.main(inp, out)
    a, b, c = unframe_all(out.getvalue())
    assert a["ok"] and a["result"]["protocol"] == h.PROTOCOL_VERSION
    assert not b["ok"] and "Unknown operation" in b["error"]
    assert not c["ok"] and c["error"] == "Not connected."


# ---------------------------------------------------------------- slapd


def connect(slapd, **kw):
    s = h.Session()
    req = {
        "host": slapd["host"],
        "port": slapd["port"],
        "security": "none",
        "auth": "simple",
        "user": slapd["admin"],
        "password": slapd["password"],
    }
    req.update(kw)
    return s, s.connect(req)


def test_connect_simple_and_rootdse(slapd):
    s, info = connect(slapd)
    assert slapd["base"] in info["roots"]
    assert info["isActiveDirectory"] is False
    assert info["whoami"] == "dn:" + slapd["admin"]
    assert info["tls"] is False


def test_bad_password_reports_error(slapd):
    with pytest.raises(h.HostError, match="invalidCredentials|insufficientAccessRights"):
        connect(slapd, password="wrong")


def test_anonymous(slapd):
    s, info = connect(slapd, auth="anonymous", user="", password="")
    assert slapd["base"] in info["roots"]
    assert s.children({"dn": slapd["base"]})["children"]


def test_ldaps_with_ca(slapd):
    s, info = connect(slapd, port=slapd["tls_port"], security="ldaps", host="localhost", caFile=slapd["ca"])
    assert info["tls"] is True


def test_ldaps_against_plain_port_explains(slapd):
    with pytest.raises(h.HostError, match="doesn't offer LDAPS"):
        connect(slapd, security="ldaps")  # plain LDAP port


def test_connection_refused_explains(slapd):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    with pytest.raises(h.HostError, match="Nothing is accepting connections"):
        connect(slapd, port=closed)


def test_ldaps_untrusted_cert_fails_then_works_unverified(slapd):
    with pytest.raises(h.HostError, match="isn't trusted"):
        connect(slapd, port=slapd["tls_port"], security="ldaps", host="localhost")
    s, info = connect(slapd, port=slapd["tls_port"], security="ldaps", host="localhost", verifyCert=False)
    assert info["tls"] is True


def test_starttls(slapd):
    if not slapd["starttls"]:
        pytest.skip("this slapd build has no usable TLS (e.g. Apple's Keychain-only slapd)")
    s, info = connect(slapd, security="starttls", host="localhost", caFile=slapd["ca"])
    assert info["tls"] is True


def test_children(slapd):
    s, _ = connect(slapd)
    res = s.children({"dn": slapd["base"]})
    rdns = [c["rdn"] for c in res["children"]]
    assert {"ou=People", "ou=Groups", "ou=Bulk", "cn=admin"} <= set(rdns)
    people = next(c for c in res["children"] if c["rdn"] == "ou=People")
    assert people["kind"] == "ou" and people["hasChildren"] is True
    leaf = s.children({"dn": "ou=People," + slapd["base"]})["children"]
    assert all(c["hasChildren"] is False and c["kind"] == "user" for c in leaf)


def test_children_paging(slapd):
    s, _ = connect(slapd)
    seen, cookie, pages = set(), None, 0
    while True:
        res = s.children({"dn": "ou=Bulk," + slapd["base"], "pageSize": 500, "cookie": cookie})
        seen.update(c["dn"] for c in res["children"])
        pages += 1
        cookie = res["cookie"]
        if not cookie:
            break
    assert len(seen) == slapd["bulk"] and pages == 3


def test_entry(slapd):
    s, _ = connect(slapd)
    e = s.entry({"dn": "uid=alice,ou=People," + slapd["base"]})
    attrs = {a["name"]: a for a in e["attributes"]}
    assert attrs["description"]["values"] == [{"v": "Álice in Wönderland"}]
    assert attrs["jpegPhoto"]["values"][0]["size"] == 13
    assert attrs["createTimestamp"]["operational"] is True
    assert "d" in attrs["createTimestamp"]["values"][0]
    assert attrs["mail"]["operational"] is False
    names = [a["name"] for a in e["attributes"]]
    # user attributes sorted before operational ones
    assert names.index("uid") < names.index("createTimestamp")


def test_entry_missing(slapd):
    s, _ = connect(slapd)
    with pytest.raises(h.HostError, match="noSuchObject"):
        s.entry({"dn": "uid=nobody," + slapd["base"]})


def test_search(slapd):
    s, _ = connect(slapd)
    res = s.search({"base": slapd["base"], "filter": "mail=*", "scope": "sub"})
    assert [e["dn"] for e in res["entries"]] == ["uid=alice,ou=People," + slapd["base"]]
    assert res["filter"] == "(mail=*)"
    big = s.search({"base": slapd["base"], "filter": "(sn=Bulk)", "limit": 750})
    assert len(big["entries"]) == 750 and big["truncated"] is True
    everything = s.search({"base": slapd["base"], "filter": "(sn=Bulk)", "limit": 5000, "attributes": ["uid"]})
    assert len(everything["entries"]) == slapd["bulk"] and everything["truncated"] is False


def test_bad_filter(slapd):
    s, _ = connect(slapd)
    with pytest.raises(Exception):
        s.search({"base": slapd["base"], "filter": "(cn=unbalanced"})


def test_schema(slapd):
    s, _ = connect(slapd)
    sc = s.schema({})
    assert any(c["name"] == "inetOrgPerson" for c in sc["objectClasses"])
    assert any(a["name"] == "mail" for a in sc["attributeTypes"])


def test_end_to_end_subprocess(slapd):
    """Drive the real script over stdin/stdout exactly like Firefox does."""
    msgs = [
        {"id": 1, "op": "connect", "host": slapd["host"], "port": slapd["port"], "auth": "simple",
         "user": slapd["admin"], "password": slapd["password"]},
        {"id": 2, "op": "search", "base": slapd["base"], "filter": "(objectClass=*)", "limit": 5000},
        {"id": 3, "op": "disconnect"},
    ]
    proc = subprocess.run(
        [sys.executable, str(HOST_SCRIPT)],
        input=b"".join(frame(m) for m in msgs),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    raw = unframe_all(proc.stdout)
    # reassemble chunks
    parts, responses = {}, []
    for m in raw:
        if "chunk" in m:
            parts.setdefault(m["id"], []).append(m)
            if len(parts[m["id"]]) == m["of"]:
                responses.append(json.loads("".join(p["data"] for p in sorted(parts[m["id"]], key=lambda p: p["chunk"]))))
        else:
            responses.append(m)
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert all(r["ok"] for r in responses), responses
    assert len(responses[1]["result"]["entries"]) == slapd["bulk"] + 8


def test_ntlm_md4_available():
    """NTLM binds need MD4, which modern OpenSSL drops from hashlib."""
    from ldap3.utils.ntlm import NtlmClient

    client = NtlmClient("CORP", "bob", "Password1")
    assert len(client.ntowf_v2()) == 16
    from Crypto.Hash import MD4

    assert MD4.new("Password1".encode("utf-16-le")).hexdigest() == "64f12cddaa88057e06a81b54e73b949b"


def test_ntlm_requires_domain_user():
    with pytest.raises(h.HostError, match="DOMAIN"):
        h.Session().connect({"host": "x", "auth": "ntlm", "user": "bob", "password": "p"})


def test_refuses_end_of_life_python(monkeypatch):
    monkeypatch.setattr(h, "MIN_PYTHON", (sys.version_info[0], sys.version_info[1] + 1))
    with pytest.raises(h.HostError, match="end-of-life"):
        h.handle(h.Session(), {"op": "hello"})
