#!/usr/bin/env python3
"""Native messaging host for the LDAP Browser Firefox extension.

Firefox extensions cannot open raw TCP sockets, so this small helper speaks
LDAP on the extension's behalf. It reads length-prefixed JSON requests on
stdin and writes length-prefixed JSON responses on stdout, as described in
https://developer.mozilla.org/docs/Mozilla/Add-ons/WebExtensions/Native_messaging

Each browser tab that connects gets its own host process, so one process
holds at most one LDAP connection.

The host is read-only: it binds, reads the RootDSE, lists children, reads
entries and runs searches. It never modifies the directory.
"""

import base64
import json
import ssl
import struct
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars  # noqa: F401  (re-exported for tests)
from ldap3.utils.dn import parse_dn

# Release version. The source tree always says 0.0.0 (a development build);
# scripts/package.sh replaces it with the release tag's version.
__version__ = "0.0.0"

# Oldest Python release still supported upstream (3.10 went end-of-life on
# 2026-10-01); keep in step with MIN_MINOR in install.sh / install.ps1.
MIN_PYTHON = (3, 11)

PROTOCOL_VERSION = 1
# Firefox rejects host->extension messages over 1 MB; stay well under it.
MAX_MESSAGE = 768 * 1024
DEFAULT_PAGE = 500

# ---------------------------------------------------------------------------
# Native messaging framing
# ---------------------------------------------------------------------------


def read_message(stream):
    raw_len = stream.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("@I", raw_len)
    data = stream.read(length)
    if len(data) < length:
        return None
    return json.loads(data.decode("utf-8"))


def _write_raw(stream, obj):
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("@I", len(data)))
    stream.write(data)
    stream.flush()


def write_message(stream, obj):
    """Write a message, splitting it into chunks if it is too big for Firefox."""
    text = json.dumps(obj, separators=(",", ":"))
    if len(text.encode("utf-8")) <= MAX_MESSAGE:
        _write_raw(stream, obj)
        return
    # Chunk on characters; 4 bytes per char worst case keeps each chunk legal.
    step = MAX_MESSAGE // 4
    parts = [text[i : i + step] for i in range(0, len(text), step)]
    for index, part in enumerate(parts):
        _write_raw(
            stream,
            {"id": obj.get("id"), "chunk": index, "of": len(parts), "data": part},
        )


# ---------------------------------------------------------------------------
# Value decoding (Active Directory and generic LDAP)
# ---------------------------------------------------------------------------

UAC_FLAGS = [
    (0x0001, "SCRIPT"),
    (0x0002, "ACCOUNTDISABLE"),
    (0x0008, "HOMEDIR_REQUIRED"),
    (0x0010, "LOCKOUT"),
    (0x0020, "PASSWD_NOTREQD"),
    (0x0040, "PASSWD_CANT_CHANGE"),
    (0x0080, "ENCRYPTED_TEXT_PWD_ALLOWED"),
    (0x0100, "TEMP_DUPLICATE_ACCOUNT"),
    (0x0200, "NORMAL_ACCOUNT"),
    (0x0800, "INTERDOMAIN_TRUST_ACCOUNT"),
    (0x1000, "WORKSTATION_TRUST_ACCOUNT"),
    (0x2000, "SERVER_TRUST_ACCOUNT"),
    (0x10000, "DONT_EXPIRE_PASSWORD"),
    (0x20000, "MNS_LOGON_ACCOUNT"),
    (0x40000, "SMARTCARD_REQUIRED"),
    (0x80000, "TRUSTED_FOR_DELEGATION"),
    (0x100000, "NOT_DELEGATED"),
    (0x200000, "USE_DES_KEY_ONLY"),
    (0x400000, "DONT_REQ_PREAUTH"),
    (0x800000, "PASSWORD_EXPIRED"),
    (0x1000000, "TRUSTED_TO_AUTH_FOR_DELEGATION"),
    (0x4000000, "PARTIAL_SECRETS_ACCOUNT"),
]

GROUP_TYPE_FLAGS = [
    (0x00000001, "BUILTIN_LOCAL_GROUP"),
    (0x00000002, "ACCOUNT_GROUP (global)"),
    (0x00000004, "RESOURCE_GROUP (domain local)"),
    (0x00000008, "UNIVERSAL_GROUP"),
    (0x00000010, "APP_BASIC_GROUP"),
    (0x00000020, "APP_QUERY_GROUP"),
    (0x80000000, "SECURITY_ENABLED"),
]

SAM_ACCOUNT_TYPES = {
    0x00000000: "SAM_DOMAIN_OBJECT",
    0x10000000: "SAM_GROUP_OBJECT",
    0x10000001: "SAM_NON_SECURITY_GROUP_OBJECT",
    0x20000000: "SAM_ALIAS_OBJECT",
    0x20000001: "SAM_NON_SECURITY_ALIAS_OBJECT",
    0x30000000: "SAM_USER_OBJECT",
    0x30000001: "SAM_MACHINE_ACCOUNT",
    0x30000002: "SAM_TRUST_ACCOUNT",
    0x40000000: "SAM_APP_BASIC_GROUP",
    0x40000001: "SAM_APP_QUERY_GROUP",
}

# Attributes holding Windows FILETIME (100ns ticks since 1601-01-01 UTC).
FILETIME_ATTRS = {
    "accountexpires",
    "badpasswordtime",
    "lastlogon",
    "lastlogoff",
    "lastlogontimestamp",
    "lockouttime",
    "pwdlastset",
    "creationtime",
    "ms-mcs-admpwdexpirationtime",
    "mslaps-passwordexpirationtime",
}
# Attributes holding negative 100ns intervals (durations).
INTERVAL_ATTRS = {
    "maxpwdage",
    "minpwdage",
    "lockoutduration",
    "lockoutobservationwindow",
    "forcelogoff",
}
SID_ATTRS = {"objectsid", "sidhistory", "securityidentifier", "tokengroups"}
GUID_ATTRS = {
    "objectguid",
    "schemaidguid",
    "attributesecurityguid",
    "msexchmailboxguid",
    "invocationid",
    "ms-ds-consistencyguid",
    "msds-consistencyguid",
}
GENERALIZED_TIME_ATTRS = {
    "whencreated",
    "whenchanged",
    "createtimestamp",
    "modifytimestamp",
    "pwdchangedtime",
    "pwdaccountlockedtime",
    "dscorepropagationdata",
}
# Attributes that are always binary even when the bytes happen to be UTF-8.
BINARY_ATTRS = (
    SID_ATTRS
    | GUID_ATTRS
    | {
        "ntsecuritydescriptor",
        "usercertificate",
        "cacertificate",
        "thumbnailphoto",
        "jpegphoto",
        "msexchmailboxsecuritydescriptor",
        "logonhours",
        "replpropertymetadata",
        "repluptodatevector",
        "msds-generationid",
        "unicodepwd",
        "userpassword",
        "dbcspwd",
        "ntpwdhistory",
        "lmpwdhistory",
        "supplementalcredentials",
        "msds-keycredentiallink",
        "msds-allowedtoactonbehalfofotheridentity",
    }
)

FILETIME_NEVER = {0, 0x7FFFFFFFFFFFFFFF}
EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)


def decode_sid(raw):
    if len(raw) < 8:
        return None
    revision = raw[0]
    count = raw[1]
    authority = int.from_bytes(raw[2:8], "big")
    if len(raw) < 8 + 4 * count:
        return None
    subs = [
        struct.unpack("<I", raw[8 + 4 * i : 12 + 4 * i])[0] for i in range(count)
    ]
    return "S-%d-%d%s" % (revision, authority, "".join("-%d" % s for s in subs))


def decode_guid(raw):
    if len(raw) != 16:
        return None
    return str(uuid.UUID(bytes_le=bytes(raw)))


def filetime_to_text(value):
    ticks = int(value)
    if ticks in FILETIME_NEVER:
        return "never"
    try:
        dt = EPOCH_1601 + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def interval_to_text(value):
    ticks = int(value)
    if ticks == -0x8000000000000000:
        return "none"
    seconds = abs(ticks) // 10_000_000
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if secs or not parts:
        parts.append("%ds" % secs)
    return " ".join(parts)


def generalized_time_to_text(text):
    # e.g. 20240131120000.0Z or 20240131120000Z
    core = text.rstrip("Z").split(".")[0]
    try:
        dt = datetime.strptime(core[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def flags_to_text(value, table):
    value = int(value) & 0xFFFFFFFF
    names = [name for bit, name in table if value & bit]
    return " | ".join(names) if names else "(none)"


def describe(attr, raw):
    """Return a human-friendly interpretation of a value, or None."""
    name = attr.lower().split(";")[0]
    try:
        if name in SID_ATTRS and isinstance(raw, bytes):
            return decode_sid(raw)
        if name in GUID_ATTRS and isinstance(raw, bytes):
            return decode_guid(raw)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        if name in FILETIME_ATTRS:
            return filetime_to_text(text)
        if name in INTERVAL_ATTRS:
            return interval_to_text(text)
        if name in GENERALIZED_TIME_ATTRS:
            return generalized_time_to_text(text)
        if name == "useraccountcontrol":
            return flags_to_text(text, UAC_FLAGS)
        if name == "grouptype":
            return flags_to_text(text, GROUP_TYPE_FLAGS)
        if name == "samaccounttype":
            return SAM_ACCOUNT_TYPES.get(int(text))
    except (ValueError, UnicodeDecodeError, struct.error):
        return None
    return None


def encode_value(attr, raw):
    """Turn a raw LDAP value into a JSON-safe dict for the UI."""
    name = attr.lower().split(";")[0]
    out = {}
    if isinstance(raw, bytes):
        text = None
        if name not in BINARY_ATTRS:
            try:
                text = raw.decode("utf-8")
                if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
                    text = None
            except UnicodeDecodeError:
                text = None
        if text is None:
            out["b64"] = base64.b64encode(raw).decode("ascii")
            out["size"] = len(raw)
        else:
            out["v"] = text
    else:
        out["v"] = str(raw)
    info = describe(attr, raw)
    if info:
        out["d"] = info
    return out


# ---------------------------------------------------------------------------
# LDAP session
# ---------------------------------------------------------------------------

# objectClasses that never have children in practice (used when the server
# does not publish hasSubordinates, e.g. Active Directory).
LEAF_CLASSES = {
    "person",
    "user",
    "inetorgperson",
    "group",
    "groupofnames",
    "groupofuniquenames",
    "posixgroup",
    "contact",
    "foreignsecurityprincipal",
    "dnsnode",
    "printqueue",
    "volume",
}
# ...except these, which are users that can hold children.
CONTAINER_OVERRIDES = {"computer", "organizationalunit", "container"}

RDN_ICON_HINTS = [
    ("domaindns", "domain"),
    ("domain", "domain"),
    ("organization", "domain"),
    ("organizationalunit", "ou"),
    ("computer", "computer"),
    ("group", "group"),
    ("groupofnames", "group"),
    ("groupofuniquenames", "group"),
    ("posixgroup", "group"),
    ("user", "user"),
    ("inetorgperson", "user"),
    ("person", "user"),
    ("contact", "contact"),
    ("container", "container"),
    ("builtindomain", "container"),
]


def kind_for(object_classes):
    classes = {c.lower() for c in object_classes}
    for cls, kind in RDN_ICON_HINTS:
        if cls in classes:
            return kind
    return "object"


def first_rdn(dn):
    try:
        parts = parse_dn(dn, escape=False)
    except LDAPException:
        return dn
    if not parts:
        return dn
    attr, value, _sep = parts[0]
    return "%s=%s" % (attr, value)


def _single(values):
    if isinstance(values, list):
        return values[0] if values else None
    return values


def _as_list(values):
    if values is None:
        return []
    if isinstance(values, list):
        return values
    return [values]


class HostError(Exception):
    pass


def explain_connect_error(exc, security, host, port):
    """Turn ldap3's socket/TLS errors into advice a person can act on."""
    raw = "%s: %s" % (type(exc).__name__, exc)
    text = raw.lower()
    mode = {"ldaps": "LDAPS", "starttls": "StartTLS", "none": "plain LDAP"}.get(security, security)
    tried = "Tried %s to %s on port %d." % (mode, host, port)
    if "certificate verify failed" in text or "certificate_verify_failed" in text:
        if "hostname" in text or "ip address mismatch" in text:
            hint = (
                "The server's certificate doesn't match the name \"%s\". Connect using the name "
                "on the certificate (usually the server's full DNS name)." % host
            )
        else:
            hint = (
                "The server's TLS certificate isn't trusted. Give the issuing CA's PEM file under "
                "\"CA certificate file\", or (on a trusted network) untick \"Verify the server certificate\"."
            )
    elif "ssl wrapping error" in text or "wrong version number" in text or "unexpected_eof" in text or (
        security == "starttls" and ("bad file descriptor" in text or "socket sending error" in text)
    ):
        if security == "ldaps":
            hint = (
                "The server didn't complete a TLS handshake, so it probably doesn't offer LDAPS on this port.\n"
                "Other settings to try: Security \"StartTLS\" (port 389), or \"None\" if the server has no TLS."
            )
        elif security == "starttls":
            hint = (
                "The server accepted the connection but the StartTLS handshake failed, so it probably "
                "has no working TLS on this port.\n"
                "Other settings to try: Security \"LDAPS\" (port 636), or \"None\" if the server has no TLS."
            )
        else:
            hint = "The TLS handshake failed."
    elif "connection refused" in text:
        hint = (
            "Nothing is accepting connections there. Check the port and the security setting "
            "(LDAPS normally uses port 636; StartTLS and plain LDAP use 389)."
        )
    elif "timed out" in text or "timeout" in text:
        hint = "The connection timed out. Check the host name, the port, and any firewall or VPN in between."
    elif "nodename nor servname" in text or "name or service not known" in text or "getaddrinfo" in text:
        hint = "Couldn't resolve the server name. Check its spelling and that you're on the right network or VPN."
    else:
        return raw
    return "%s %s\n\nDetails: %s" % (tried, hint, raw)


class Session:
    def __init__(self):
        self.conn = None
        self.server_info = None
        self.description = None

    # -- connection --------------------------------------------------------

    def connect(self, req):
        self.disconnect()
        host = (req.get("host") or "").strip()
        if not host:
            raise HostError("A server host name is required.")
        security = req.get("security", "none")
        if security not in ("none", "ldaps", "starttls"):
            raise HostError("Unknown security mode: %s" % security)
        port = int(req.get("port") or (636 if security == "ldaps" else 389))
        timeout = int(req.get("timeout") or 10)

        tls = None
        if security in ("ldaps", "starttls"):
            verify = req.get("verifyCert", True)
            tls = ldap3.Tls(
                validate=ssl.CERT_REQUIRED if verify else ssl.CERT_NONE,
                ca_certs_file=req.get("caFile") or None,
                version=ssl.PROTOCOL_TLS_CLIENT,
            )

        server = ldap3.Server(
            host,
            port=port,
            use_ssl=security == "ldaps",
            tls=tls,
            get_info=ldap3.ALL,
            connect_timeout=timeout,
        )

        auth = req.get("auth", "simple")
        user = req.get("user") or None
        password = req.get("password") or None
        kwargs = dict(
            server=server,
            receive_timeout=timeout,
            auto_range=True,
            check_names=False,
            raise_exceptions=False,
            read_only=True,
            return_empty_attributes=False,
        )
        if auth == "anonymous":
            kwargs["authentication"] = ldap3.ANONYMOUS
        elif auth == "simple":
            if not user:
                raise HostError("A bind DN or user name is required for simple bind.")
            kwargs.update(authentication=ldap3.SIMPLE, user=user, password=password)
        elif auth == "ntlm":
            if not user or "\\" not in user:
                raise HostError("NTLM needs a user in DOMAIN\\username form.")
            kwargs.update(authentication=ldap3.NTLM, user=user, password=password)
        else:
            raise HostError("Unknown authentication method: %s" % auth)

        conn = ldap3.Connection(**kwargs)
        try:
            conn.open()
            if conn.closed:
                raise HostError("Could not connect to %s:%d" % (host, port))
            if security == "starttls" and not conn.start_tls():
                raise HostError(self._result_text(conn, "StartTLS failed"))
            if auth != "anonymous" and not conn.bind():
                raise HostError(self._result_text(conn, "Bind failed"))
            if auth == "anonymous":
                # Anonymous: refresh the schema/DSE info now that we're open.
                server.get_info_from_server(conn)
        except LDAPException as exc:
            try:
                conn.unbind()
            except LDAPException:
                pass
            raise HostError(explain_connect_error(exc, security, host, port)) from exc
        except HostError:
            try:
                conn.unbind()
            except LDAPException:
                pass
            raise

        self.conn = conn
        self.description = "%s:%d" % (host, port)
        return self.root_dse()

    def disconnect(self):
        if self.conn is not None:
            try:
                self.conn.unbind()
            except LDAPException:
                pass
        self.conn = None
        return {"disconnected": True}

    def _require(self):
        if self.conn is None or self.conn.closed:
            raise HostError("Not connected.")
        return self.conn

    @staticmethod
    def _result_text(conn, prefix):
        res = conn.result or {}
        desc = res.get("description") or ""
        msg = res.get("message") or ""
        text = prefix
        if desc:
            text += ": " + desc
        if msg:
            text += " (" + msg.strip() + ")"
        if conn.last_error and conn.last_error not in text:
            text += " - " + str(conn.last_error)
        return text

    # -- reads ---------------------------------------------------------------

    def root_dse(self):
        conn = self._require()
        conn.search(
            "",
            "(objectClass=*)",
            search_scope=ldap3.BASE,
            attributes=["*", "+"],
        )
        attrs = {}
        if conn.response:
            raw = conn.response[0].get("raw_attributes", {})
            attrs = {k: [v.decode("utf-8", "replace") for v in vals] for k, vals in raw.items()}
        lower = {k.lower(): v for k, v in attrs.items()}

        naming = lower.get("namingcontexts", [])
        default = _single(lower.get("defaultnamingcontext"))
        is_ad = "1.2.840.113556.1.4.800" in lower.get("supportedcapabilities", [])
        vendor = _single(lower.get("vendorname")) or (
            "Microsoft Active Directory" if is_ad else None
        )
        if not vendor and any("openldap" in c.lower() for c in lower.get("objectclass", [])):
            vendor = "OpenLDAP"

        # Order roots: default naming context first, then the rest.
        roots = []
        if default:
            roots.append(default)
        for nc in naming:
            if nc not in roots:
                roots.append(nc)
        for key in ("configurationnamingcontext", "schemanamingcontext", "subschemasubentry"):
            val = _single(lower.get(key))
            if val and val not in roots:
                roots.append(val)

        whoami = None
        try:
            whoami = conn.extend.standard.who_am_i()
        except LDAPException:
            pass
        if isinstance(whoami, bytes):
            whoami = whoami.decode("utf-8", "replace")

        return {
            "server": self.description,
            "vendor": vendor,
            "isActiveDirectory": is_ad,
            "defaultNamingContext": default,
            "roots": roots,
            "whoami": whoami or None,
            "tls": bool(conn.tls_started or conn.server.ssl),
            "rootDSE": attrs,
        }

    def _known(self, attributes):
        """Drop attribute names the server's schema doesn't define.

        ldap3 raises on unknown names whenever it has a schema, but LDAP
        servers simply ignore them; we want the server behaviour so that
        AD-specific hints are harmless on OpenLDAP and vice versa."""
        schema = self.conn.server.schema if self.conn else None
        if schema is None or not schema.attribute_types:
            return attributes
        keep = []
        for name in attributes:
            bare = name.split(";")[0]
            if name in ("*", "+", "1.1") or bare in schema.attribute_types:
                keep.append(name)
        return keep or ["1.1"]

    def _paged(self, base, flt, scope, attributes, page_size, cookie, limit=0):
        conn = self._require()
        attributes = self._known(attributes)
        page_size = max(1, min(int(page_size or DEFAULT_PAGE), 1000))
        cookie_bytes = base64.b64decode(cookie) if cookie else None
        ok = conn.search(
            base,
            flt,
            search_scope=scope,
            attributes=attributes,
            paged_size=page_size,
            paged_cookie=cookie_bytes,
            size_limit=limit,
        )
        result = conn.result or {}
        code = result.get("result", 0)
        # 4 = sizeLimitExceeded: still return what we got.
        if not ok and code not in (0, 4):
            # Some servers refuse the paged control on a base/one-level search
            # of special entries (e.g. cn=Subschema); retry without it.
            if code in (12, 53) and cookie_bytes is None:
                ok = conn.search(
                    base, flt, search_scope=scope, attributes=attributes, size_limit=limit
                )
                result = conn.result or {}
                code = result.get("result", 0)
            if not ok and code not in (0, 4):
                raise HostError(self._result_text(conn, "Search failed"))
        entries = [r for r in conn.response or [] if r.get("type") == "searchResEntry"]
        next_cookie = None
        controls = result.get("controls") or {}
        paged = controls.get("1.2.840.113556.1.4.319")
        if paged:
            value = paged.get("value", {}).get("cookie")
            if value:
                next_cookie = base64.b64encode(value).decode("ascii")
        return entries, next_cookie, code == 4

    def children(self, req):
        dn = req.get("dn", "")
        entries, cookie, truncated = self._paged(
            dn,
            "(objectClass=*)",
            ldap3.LEVEL,
            ["objectClass", "hasSubordinates", "msDS-Approx-Immed-Subordinates"],
            req.get("pageSize"),
            req.get("cookie"),
        )
        nodes = []
        for e in entries:
            raw = {k.lower(): v for k, v in e.get("raw_attributes", {}).items()}
            classes = [c.decode("utf-8", "replace") for c in raw.get("objectclass", [])]
            nodes.append(
                {
                    "dn": e["dn"],
                    "rdn": first_rdn(e["dn"]),
                    "kind": kind_for(classes),
                    "objectClass": classes,
                    "hasChildren": self._has_children(raw, classes),
                }
            )
        nodes.sort(key=lambda n: (n["hasChildren"] is False, n["rdn"].lower()))
        return {"dn": dn, "children": nodes, "cookie": cookie, "truncated": truncated}

    @staticmethod
    def _has_children(raw, classes):
        hs = raw.get("hassubordinates")
        if hs:
            return hs[0].decode("ascii", "replace").upper() == "TRUE"
        approx = raw.get("msds-approx-immed-subordinates")
        if approx:
            try:
                return int(approx[0]) > 0
            except ValueError:
                pass
        lowered = {c.lower() for c in classes}
        if lowered & CONTAINER_OVERRIDES:
            return None  # unknown: let the UI try
        if lowered & LEAF_CLASSES:
            return False
        return None

    def entry(self, req):
        dn = req.get("dn", "")
        attributes = ["*", "+"]
        if req.get("withSecurityDescriptor"):
            attributes.append("nTSecurityDescriptor")
        conn = self._require()
        ok = conn.search(
            dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=self._known(attributes)
        )
        if not ok or not conn.response:
            raise HostError(self._result_text(conn, "Could not read entry"))
        e = next((r for r in conn.response if r.get("type") == "searchResEntry"), None)
        if e is None:
            raise HostError("Entry not found: %s" % dn)
        return {"dn": e["dn"], "attributes": self._encode_attrs(e)}

    def _encode_attrs(self, e):
        out = []
        operational = self._operational_names()
        for name, values in e.get("raw_attributes", {}).items():
            out.append(
                {
                    "name": name,
                    "operational": name.lower() in operational,
                    "values": [encode_value(name, v) for v in values],
                }
            )
        out.sort(key=lambda a: (a["operational"], a["name"].lower()))
        return out

    def _operational_names(self):
        schema = self.conn.server.schema if self.conn else None
        names = set()
        if schema is not None:
            for at in schema.attribute_types.values():
                usage = getattr(at, "usage", None)
                if usage and usage != "userApplications" and usage != 0:
                    for n in at.name or []:
                        names.add(n.lower())
        names.update(
            {
                "createtimestamp",
                "modifytimestamp",
                "creatorsname",
                "modifiersname",
                "entryuuid",
                "entrydn",
                "entrycsn",
                "hassubordinates",
                "subschemasubentry",
                "structuralobjectclass",
                "numsubordinates",
                "memberof",
            }
        )
        return names

    def search(self, req):
        base = req.get("base", "")
        flt = (req.get("filter") or "(objectClass=*)").strip()
        if not flt.startswith("("):
            flt = "(" + flt + ")"
        scope = {
            "base": ldap3.BASE,
            "one": ldap3.LEVEL,
            "sub": ldap3.SUBTREE,
        }.get(req.get("scope", "sub"), ldap3.SUBTREE)
        attrs = [a.strip() for a in (req.get("attributes") or []) if a.strip()]
        limit = int(req.get("limit") or 500)
        wanted = attrs or ["objectClass", "cn", "name", "displayName", "sAMAccountName", "mail", "uid"]
        if "objectClass" not in wanted:
            wanted = wanted + ["objectClass"]

        results = []
        cookie = req.get("cookie")
        truncated = False
        while True:
            remaining = limit - len(results)
            entries, cookie, hit_limit = self._paged(
                base, flt, scope, wanted, min(remaining, DEFAULT_PAGE), cookie
            )
            for e in entries:
                raw = {k.lower(): v for k, v in e.get("raw_attributes", {}).items()}
                classes = [c.decode("utf-8", "replace") for c in raw.get("objectclass", [])]
                results.append(
                    {
                        "dn": e["dn"],
                        "kind": kind_for(classes),
                        "attributes": self._encode_attrs(e),
                    }
                )
            if hit_limit or len(results) > limit:
                truncated = True
                break
            if not cookie:
                break
            if len(results) >= limit:
                truncated = True
                break
        return {
            "filter": flt,
            "entries": results[:limit],
            "truncated": truncated,
            "cookie": cookie if truncated else None,
        }

    def schema(self, _req):
        conn = self._require()
        schema = conn.server.schema
        if schema is None:
            raise HostError("The server did not return a schema.")
        attrs = []
        for at in schema.attribute_types.values():
            attrs.append(
                {
                    "name": (at.name or [at.oid])[0],
                    "oid": at.oid,
                    "syntax": at.syntax,
                    "single": bool(at.single_value),
                    "description": at.description,
                }
            )
        classes = []
        for oc in schema.object_classes.values():
            classes.append(
                {
                    "name": (oc.name or [oc.oid])[0],
                    "oid": oc.oid,
                    "kind": oc.kind,
                    "sup": oc.superior,
                    "must": oc.must_contain,
                    "may": oc.may_contain,
                    "description": oc.description,
                }
            )
        attrs.sort(key=lambda a: a["name"].lower())
        classes.sort(key=lambda c: c["name"].lower())
        return {"attributeTypes": attrs, "objectClasses": classes}


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def handle(session, msg):
    if sys.version_info[:2] < MIN_PYTHON:
        raise HostError(
            "The LDAP helper is running on Python %d.%d, which is end-of-life. Install Python %d.%d "
            "or newer and re-run install.sh." % (sys.version_info[:2] + MIN_PYTHON)
        )
    op = msg.get("op")
    if op == "hello":
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "ldap3": ldap3.__version__,
            "python": sys.version.split()[0],
        }
    if op == "connect":
        return session.connect(msg)
    if op == "disconnect":
        return session.disconnect()
    if op == "rootdse":
        return session.root_dse()
    if op == "children":
        return session.children(msg)
    if op == "entry":
        return session.entry(msg)
    if op == "search":
        return session.search(msg)
    if op == "schema":
        return session.schema(msg)
    raise HostError("Unknown operation: %s" % op)


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    session = Session()
    while True:
        try:
            msg = read_message(stdin)
        except (ValueError, json.JSONDecodeError) as exc:
            write_message(stdout, {"id": None, "ok": False, "error": "Bad message: %s" % exc})
            continue
        if msg is None:
            break
        mid = msg.get("id")
        try:
            result = handle(session, msg)
            write_message(stdout, {"id": mid, "ok": True, "result": result})
        except HostError as exc:
            write_message(stdout, {"id": mid, "ok": False, "error": str(exc)})
        except LDAPException as exc:
            write_message(stdout, {"id": mid, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        except Exception as exc:  # keep the host alive on unexpected errors
            traceback.print_exc(file=sys.stderr)
            write_message(stdout, {"id": mid, "ok": False, "error": "Internal error: %s" % exc})
    session.disconnect()


if __name__ == "__main__":
    main()
