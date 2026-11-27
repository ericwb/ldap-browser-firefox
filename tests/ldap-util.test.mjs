import { test } from "node:test";
import assert from "node:assert/strict";
import { splitDn, normDn, isDnUnder, parseTarget, toLdif, toCsv } from "../extension/ldap-util.js";

test("splitDn handles escapes, quotes and spaces", () => {
  assert.deepEqual(splitDn("CN=Smith\\, John,OU=Staff , DC=corp,DC=example"), [
    "CN=Smith\\, John", "OU=Staff", "DC=corp", "DC=example",
  ]);
  assert.deepEqual(splitDn('cn="a,b",dc=x'), ['cn="a,b"', "dc=x"]);
  assert.deepEqual(splitDn(""), []);
});

test("normDn / isDnUnder are case and space insensitive", () => {
  assert.equal(normDn("CN=Users, DC=Corp ,DC=Example"), "cn=users,dc=corp,dc=example");
  assert.ok(isDnUnder("cn=bob,CN=Users,DC=corp,DC=example", "dc=CORP, dc=example"));
  assert.ok(isDnUnder("dc=corp,dc=example", "DC=corp,DC=example"));
  assert.ok(!isDnUnder("dc=notcorp,dc=example", "dc=corp,dc=example"));
});

test("parseTarget", () => {
  assert.deepEqual(parseTarget("dc01.corp.example"), { host: "dc01.corp.example", port: undefined, security: undefined, base: undefined });
  assert.deepEqual(parseTarget("dc01:3269"), { host: "dc01", port: "3269", security: undefined, base: undefined });
  assert.deepEqual(parseTarget("ldaps://dc01.corp.example:636/DC=corp,DC=example"), {
    host: "dc01.corp.example", port: "636", security: "ldaps", base: "DC=corp,DC=example",
  });
  assert.deepEqual(parseTarget("ext+ldaps://dc01:636/DC=corp,DC=example"), {
    host: "dc01", port: "636", security: "ldaps", base: "DC=corp,DC=example",
  });
  assert.deepEqual(parseTarget("ext+ldap://dc01"), { host: "dc01", port: undefined, security: undefined, base: undefined });
  assert.deepEqual(parseTarget("ldap://[::1]:389/dc=a%20b"), { host: "::1", port: "389", security: undefined, base: "dc=a b" });
});

test("toLdif base64-encodes unsafe values and folds long lines", () => {
  const ldif = toLdif([{
    dn: "uid=Ålice,dc=x",
    attributes: [
      { name: "cn", values: [{ v: "Alice" }, { v: " leading space" }] },
      { name: "jpegPhoto", values: [{ b64: "/9j/4A==", size: 4 }] },
      { name: "description", values: [{ v: "x".repeat(200) }] },
    ],
  }]);
  const lines = ldif.split("\n");
  assert.equal(lines[0], "version: 1");
  assert.equal(lines[2], "dn:: " + Buffer.from("uid=Ålice,dc=x").toString("base64"));
  assert.ok(lines.includes("cn: Alice"));
  assert.ok(lines.includes("cn:: " + Buffer.from(" leading space").toString("base64")));
  assert.ok(lines.includes("jpegPhoto:: /9j/4A=="));
  assert.ok(lines.every((l) => l.length <= 76));
  // unfold and check round-trip of the long line
  const unfolded = ldif.replace(/\n /g, "");
  assert.ok(unfolded.includes("description: " + "x".repeat(200)));
});

test("toCsv quotes and neutralises formulas", () => {
  const csv = toCsv([{
    dn: "cn=a,dc=x",
    attributes: [{ name: "cn", values: [{ v: "=HYPERLINK(1)" }] }, { name: "groupType", values: [{ v: "-2147483646" }] }, { name: "info", values: [{ v: 'say "hi", ok' }] }],
  }], ["cn", "groupType", "info", "missing"]);
  assert.equal(csv, 'dn,cn,groupType,info,missing\r\n"cn=a,dc=x",\'=HYPERLINK(1),-2147483646,"say ""hi"", ok",\r\n');
});
