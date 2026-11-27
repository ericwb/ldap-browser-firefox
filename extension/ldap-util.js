/* Pure helpers: DN handling, ldap:// URL parsing, LDIF and CSV output.
 * No DOM or WebExtension APIs here so this file can be unit-tested in Node.
 */

/** Split a DN into RDN strings, honouring backslash escapes and quotes. */
export function splitDn(dn) {
  if (!dn) return [];
  const out = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < dn.length; i++) {
    const c = dn[i];
    if (c === "\\" && i + 1 < dn.length) {
      cur += c + dn[++i];
    } else if (c === '"') {
      quoted = !quoted;
      cur += c;
    } else if ((c === "," || c === ";") && !quoted) {
      out.push(cur.trim());
      cur = "";
    } else {
      cur += c;
    }
  }
  if (cur.trim() || out.length) out.push(cur.trim());
  return out;
}

/** Normalise a DN for comparison: trim around separators, lowercase. */
export function normDn(dn) {
  return splitDn(dn || "")
    .map((rdn) =>
      rdn
        .split("+")
        .map((ava) => {
          const i = ava.indexOf("=");
          return i < 0 ? ava.trim() : `${ava.slice(0, i).trim()}=${ava.slice(i + 1).trim()}`;
        })
        .join("+"),
    )
    .join(",")
    .toLowerCase();
}

/** True when `dn` equals `base` or sits somewhere beneath it. */
export function isDnUnder(dn, base) {
  const d = normDn(dn);
  const b = normDn(base);
  if (!b) return true;
  return d === b || d.endsWith("," + b);
}

/**
 * Parse what the user typed as a server: a bare host, host:port, or an
 * RFC 4516 URL such as ldaps://dc01.corp.example:636/DC=corp,DC=example.
 */
export function parseTarget(text) {
  let s = (text || "").trim();
  const out = { host: s, port: undefined, security: undefined, base: undefined };
  // Also accept the ext+ldap:// / ext+ldaps:// (and web+) forms the add-on registers.
  const m = /^(?:ext\+|web\+)?(ldaps?|ldapi):\/\/([^/?#]*)(?:\/([^?#]*))?/i.exec(s);
  if (m) {
    out.security = m[1].toLowerCase() === "ldaps" ? "ldaps" : undefined;
    s = m[2];
    if (m[3]) {
      try {
        out.base = decodeURIComponent(m[3]);
      } catch {
        out.base = m[3];
      }
    }
  }
  // [ipv6]:port | host:port | host
  const v6 = /^\[([^\]]+)\](?::(\d+))?$/.exec(s);
  const hp = /^([^:]+):(\d+)$/.exec(s);
  if (v6) {
    out.host = v6[1];
    out.port = v6[2];
  } else if (hp) {
    out.host = hp[1];
    out.port = hp[2];
  } else {
    out.host = s;
  }
  if (out.port) out.port = String(Number(out.port));
  return out;
}

/* ----------------------------------------------------------------- LDIF */

function utf8ToBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

// RFC 2849 SAFE-STRING: no leading space/colon/<, ASCII without NUL/CR/LF, no trailing space.
function isSafe(s) {
  return s === "" || (/^[\x01-\x09\x0b\x0c\x0e-\x1f\x21-\x39\x3b\x3d-\x7f][\x01-\x09\x0b\x0c\x0e-\x7f]*$/.test(s) && !s.endsWith(" "));
}

function fold(line) {
  if (line.length <= 76) return line;
  const parts = [line.slice(0, 76)];
  for (let i = 76; i < line.length; i += 75) parts.push(" " + line.slice(i, i + 75));
  return parts.join("\n");
}

function ldifLine(name, value) {
  if (value.b64 !== undefined) return fold(`${name}:: ${value.b64}`);
  const v = value.v ?? "";
  return fold(isSafe(v) ? `${name}: ${v}` : `${name}:: ${utf8ToBase64(v)}`);
}

/** entries: [{dn, attributes: [{name, values: [{v}|{b64}]}]}] */
export function toLdif(entries) {
  const out = ["version: 1", ""];
  for (const e of entries) {
    out.push(ldifLine("dn", { v: e.dn }));
    for (const a of e.attributes) {
      for (const v of a.values) out.push(ldifLine(a.name, v));
    }
    out.push("");
  }
  return out.join("\n");
}

/* ------------------------------------------------------------------ CSV */

function csvCell(s) {
  let t = String(s ?? "");
  // Neutralise spreadsheet formulas (CSV injection), but keep negative numbers.
  if (/^[=+@\t\r]/.test(t) || /^-(?![\d.])/.test(t)) t = "'" + t;
  return /[",\r\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

export function toCsv(entries, columns) {
  const lines = [["dn", ...columns].map(csvCell).join(",")];
  for (const e of entries) {
    const by = new Map(e.attributes.map((a) => [a.name.toLowerCase(), a]));
    const row = [e.dn];
    for (const c of columns) {
      const a = by.get(c.toLowerCase());
      row.push(a ? a.values.map((v) => (v.b64 !== undefined ? v.d || v.b64 : v.v)).join("; ") : "");
    }
    lines.push(row.map(csvCell).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}
