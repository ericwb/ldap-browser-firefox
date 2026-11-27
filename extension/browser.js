import { LdapHost, HostMissingError } from "./native.js";
import { splitDn, normDn, isDnUnder, parseTarget, toLdif, toCsv } from "./ldap-util.js";

/* =====================================================================
 * Small DOM helpers. Directory data is untrusted: it only ever reaches
 * the page through textContent / attribute setters, never innerHTML.
 * ===================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style") node.setAttribute("style", v);
    else node[k] = v;
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const ICON_PATHS = {
  domain: "M8 1.5 1.5 4.5v1h13v-1L8 1.5ZM2.5 6.5v5h2v-5h-2Zm4.5 0v5h2v-5H7Zm4.5 0v5h2v-5h-2ZM1.5 12.5v2h13v-2h-13Z",
  ou: "M1.5 3.5a1 1 0 0 1 1-1h3.6l1.4 1.5h6a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-9.5Z",
  container: "M2 3.5a1 1 0 0 1 1-1h3.4l1.3 1.4H13a1 1 0 0 1 1 1v8.1a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5Zm1.2 2.3v6.9h9.6V5.8H3.2Z",
  user: "M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-5.5 6.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5v.5h-11v-.5Z",
  group: "M5.5 7.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Zm6 0a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM.5 14c0-2.7 2.2-4.5 5-4.5s5 1.8 5 4.5v.5h-10V14Zm11-4.4c2.3.1 4 1.6 4 3.9v1h-4V14c0-1.7-.6-3.2-1.6-4.2.5-.1 1-.2 1.6-.2Z",
  computer: "M1.5 3a1 1 0 0 1 1-1h11a1 1 0 0 1 1 1v7.5a1 1 0 0 1-1 1h-4v1.5h2.5v1.5h-8v-1.5H6.5v-1.5h-4a1 1 0 0 1-1-1V3Zm1.5.5V10h10V3.5H3Z",
  contact: "M2.5 1.5h10a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-10v-13ZM8 8.2a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM4.6 12h6.8c0-1.9-1.5-3-3.4-3S4.6 10.1 4.6 12ZM1 3.5h2.2v1.5H1V3.5Zm0 3.7h2.2v1.5H1V7.2Zm0 3.7h2.2v1.5H1v-1.5Z",
  object: "M3 1.5h6.5l3.5 3.5v9.5H3v-13Zm6 1v3h3",
  chevron: "M5 2.5 10.5 8 5 13.5",
};

function icon(kind) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICON_PATHS[kind] || ICON_PATHS.object);
  if (kind === "chevron") {
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
  } else if (kind === "object") {
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-linejoin", "round");
  } else {
    path.setAttribute("fill", "currentColor");
  }
  svg.append(path);
  return svg;
}

function kindIcon(kind) {
  const k = ICON_PATHS[kind] ? kind : "object";
  return el("span", { class: `kind-icon k-${k}` }, icon(k));
}

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2200);
}

function download(filename, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function safeFileName(s) {
  return (s || "export").replace(/[^\w.-]+/g, "_").slice(0, 80);
}

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage unavailable: ignore */
    }
  },
};

/* =====================================================================
 * State
 * ===================================================================== */

const state = {
  host: null,
  info: null,
  roots: [],
  nodes: new Map(), // normDn -> node
  selected: null, // node
  entry: null, // last loaded entry
  searchResults: null,
  schema: null,
};

/* =====================================================================
 * Saved connection profiles (browser.storage.local, never passwords)
 * ===================================================================== */

const PROFILE_FIELDS = ["host", "port", "security", "verifyCert", "caFile", "auth", "user", "base"];

async function loadProfiles() {
  const { profiles = [] } = await browser.storage.local.get("profiles");
  return profiles;
}

async function saveProfiles(profiles) {
  await browser.storage.local.set({ profiles });
}

function formValues() {
  const f = $("#connect-form");
  return {
    host: f.host.value.trim(),
    port: f.port.value.trim(),
    security: f.security.value,
    verifyCert: f.verifyCert.checked,
    caFile: f.caFile.value.trim(),
    auth: f.auth.value,
    user: f.user.value.trim(),
    password: f.password.value,
    base: f.base.value.trim(),
    profileName: f.profileName.value.trim(),
  };
}

function fillForm(p) {
  const f = $("#connect-form");
  if (p.host !== undefined) f.host.value = p.host;
  if (p.port !== undefined) f.port.value = p.port || "";
  if (p.security) f.security.value = p.security;
  if (p.verifyCert !== undefined) f.verifyCert.checked = p.verifyCert !== false;
  if (p.caFile !== undefined) f.caFile.value = p.caFile || "";
  if (p.auth) f.auth.value = p.auth;
  if (p.user !== undefined) f.user.value = p.user || "";
  if (p.base !== undefined) f.base.value = p.base || "";
  if (p.name !== undefined) f.profileName.value = p.name;
  syncFormHints();
}

function syncFormHints() {
  const f = $("#connect-form");
  const tls = f.security.value !== "none";
  $("#verify-wrap").style.visibility = tls ? "visible" : "hidden";
  $("#ca-wrap").hidden = !tls;
  const anon = f.auth.value === "anonymous";
  $$(".cred", f).forEach((n) => (n.style.display = anon ? "none" : ""));
  $("#plain-warning").hidden = tls || anon;
  f.port.placeholder = f.security.value === "ldaps" ? "636" : "389";
  f.user.placeholder =
    f.auth.value === "ntlm" ? "CORP\\username" : "user@corp.example, CORP\\user or cn=admin,dc=example,dc=com";
}

function profileSubtitle(p) {
  const t = parseTarget(p.host);
  const security = t.security || p.security;
  const scheme = security === "ldaps" ? "ldaps" : security === "starttls" ? "ldap+starttls" : "ldap";
  const port = p.port || t.port;
  const host = t.host.includes(":") ? `[${t.host}]` : t.host;
  return `${scheme}://${host}${port ? ":" + port : ""}`;
}

async function renderProfiles(activeName) {
  const profiles = await loadProfiles();
  const list = $("#profile-list");
  list.replaceChildren();
  $("#profile-empty").hidden = profiles.length > 0;
  for (const p of profiles) {
    const li = el(
      "li",
      {
        class: p.name === activeName ? "active" : "",
        title: `${p.host}${p.port ? ":" + p.port : ""}`,
        onclick: () => {
          fillForm(p);
          $("#connect-form").password.value = "";
          renderProfiles(p.name);
          (p.auth === "anonymous" ? $("#btn-connect") : $("#connect-form").password).focus();
        },
      },
      el(
        "div",
        { class: "ptext" },
        el("span", { class: "pname", text: p.name }),
        el("span", { class: "phost", text: profileSubtitle(p) }),
      ),
      el("button", {
        class: "del",
        title: "Delete",
        text: "✕",
        onclick: async (e) => {
          e.stopPropagation();
          const rest = (await loadProfiles()).filter((x) => x.name !== p.name);
          await saveProfiles(rest);
          renderProfiles();
        },
      }),
    );
    list.append(li);
  }
}

async function saveCurrentProfile() {
  const v = formValues();
  if (!v.host) {
    toast("Enter a server first");
    return;
  }
  const name = v.profileName || v.host;
  const profile = { name };
  for (const k of PROFILE_FIELDS) profile[k] = v[k];
  const profiles = (await loadProfiles()).filter((p) => p.name !== name);
  profiles.push(profile);
  profiles.sort((a, b) => a.name.localeCompare(b.name));
  await saveProfiles(profiles);
  $("#connect-form").profileName.value = name;
  renderProfiles(name);
  toast(`Saved “${name}”`);
}

/* =====================================================================
 * Connecting
 * ===================================================================== */

function showError(sel, err) {
  const box = $(sel);
  if (!err) {
    box.hidden = true;
    return;
  }
  box.textContent = err.message || String(err);
  box.hidden = false;
}

async function connect(ev) {
  ev.preventDefault();
  showError("#connect-error", null);
  const v = formValues();
  const target = parseTarget(v.host);
  const req = {
    host: target.host,
    port: v.port || target.port || undefined,
    security: target.security || v.security,
    verifyCert: v.verifyCert,
    caFile: v.caFile || undefined,
    auth: v.auth,
    user: v.auth === "anonymous" ? undefined : v.user,
    password: v.auth === "anonymous" ? undefined : v.password,
  };
  const base = v.base || target.base;
  const btn = $("#btn-connect");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    if (state.host) state.host.close();
    state.host = new LdapHost();
    state.host.onclose = (err) => {
      if (!$("#view-browse").hidden) {
        toast("Connection to the LDAP helper was lost");
        showConnectView();
        showError("#connect-error", err);
      }
    };
    const info = await state.host.request("connect", req);
    $("#connect-form").password.value = "";
    onConnected(info, base);
  } catch (err) {
    if (err instanceof HostMissingError) {
      $("#host-missing").hidden = false;
      $("#host-missing-detail").textContent = err.message;
    } else {
      showError("#connect-error", err);
    }
    if (state.host) state.host.close();
    state.host = null;
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect";
  }
}

function onConnected(info, base) {
  state.info = info;
  state.roots = base ? [base] : info.roots.length ? info.roots : [""];
  state.nodes.clear();
  state.selected = null;
  state.entry = null;
  state.schema = null;
  state.searchResults = null;

  $("#status").hidden = false;
  $("#btn-disconnect").hidden = false;
  $("#status-server").textContent = info.server;
  $("#status-vendor").textContent = info.isActiveDirectory ? "Active Directory" : info.vendor || "LDAP";
  const tlsChip = $("#status-tls");
  tlsChip.textContent = info.tls ? "🔒 TLS" : "Not encrypted";
  tlsChip.className = info.tls ? "chip tls" : "chip insecure";
  $("#status-who").textContent = info.whoami ? `as ${info.whoami.replace(/^(dn|u):/, "")}` : "";
  document.title = `${info.server} — LDAP Browser`;

  $("#view-connect").hidden = true;
  $("#view-browse").hidden = false;

  buildTree();
  renderServer();
  setupPresets();
  $("#search-form").base.value = state.roots[0] || "";
  $("#entry").hidden = true;
  $("#entry-empty").hidden = false;
  $("#results thead").replaceChildren();
  $("#results tbody").replaceChildren();
  $("#search-status").textContent = "";
  $("#btn-export-csv").disabled = $("#btn-export-ldif").disabled = true;
  switchTab("entry");

  const first = state.nodes.get(normDn(state.roots[0]));
  if (first) {
    selectNode(first);
    expand(first);
  }
}

async function disconnect() {
  if (state.host) {
    state.host.onclose = null;
    try {
      await state.host.request("disconnect");
    } catch {
      /* already gone */
    }
    state.host.close();
  }
  state.host = null;
  showConnectView();
}

function showConnectView() {
  $("#status").hidden = true;
  $("#btn-disconnect").hidden = true;
  $("#view-browse").hidden = true;
  $("#view-connect").hidden = false;
  document.title = "LDAP Browser";
}

/* =====================================================================
 * Tree
 * ===================================================================== */

function rdnLabel(rdn) {
  const i = rdn.indexOf("=");
  if (i < 0) return el("span", { class: "label", text: rdn });
  return el(
    "span",
    { class: "label" },
    el("span", { class: "attr", text: rdn.slice(0, i + 1) }),
    rdn.slice(i + 1),
  );
}

function makeNode({ dn, rdn, kind, hasChildren }, depth, isRoot = false) {
  const li = el("li", { role: "treeitem", class: hasChildren === false ? "leaf" : "" });
  const row = el(
    "div",
    { class: `row${isRoot ? " root" : ""}`, style: `--depth:${depth}`, title: dn || "(Root DSE)" },
    el("span", { class: "twisty" }, icon("chevron")),
    kindIcon(kind),
    isRoot ? el("span", { class: "label", text: dn || "(Root DSE)" }) : rdnLabel(rdn),
  );
  const ul = el("ul", { role: "group", hidden: true });
  li.append(row, ul);
  const node = { dn, kind, li, row, ul, depth, loaded: false, loading: false, cookie: null, hasChildren };
  li._node = node;
  row.addEventListener("click", (e) => {
    if (e.target.closest(".twisty")) toggle(node);
    else {
      selectNode(node);
      if (!node.li.classList.contains("open")) expand(node);
    }
  });
  row.addEventListener("dblclick", (e) => {
    if (!e.target.closest(".twisty")) toggle(node);
  });
  state.nodes.set(normDn(dn), node);
  return node;
}

function buildTree() {
  const tree = $("#tree");
  tree.replaceChildren();
  for (const root of state.roots) {
    const kind = /^dc=/i.test(root) ? "domain" : root ? "container" : "object";
    const node = makeNode({ dn: root, rdn: root, kind, hasChildren: true }, 0, true);
    tree.append(node.li);
  }
}

function statusRow(node, cls, text, onclick) {
  const li = el(
    "li",
    { class: cls },
    el(
      "div",
      { class: "row", style: `--depth:${node.depth + 1}`, onclick },
      el("span", { class: "twisty" }),
      el("span", { class: "label", text }),
    ),
  );
  li.classList.add("leaf");
  return li;
}

async function loadChildren(node, more = false) {
  if (node.loading) return;
  node.loading = true;
  const loadingRow = statusRow(node, "loading", "Loading…");
  if (!more) node.ul.replaceChildren();
  node.ul.append(loadingRow);
  try {
    const res = await state.host.request("children", {
      dn: node.dn,
      pageSize: 500,
      cookie: more ? node.cookie : undefined,
    });
    loadingRow.remove();
    for (const c of res.children) node.ul.append(makeNode(c, node.depth + 1).li);
    node.cookie = res.cookie;
    node.loaded = true;
    if (res.cookie) {
      const moreRow = statusRow(node, "more", "Load more…", () => {
        moreRow.remove();
        loadChildren(node, true);
      });
      node.ul.append(moreRow);
    }
    if (!more && res.children.length === 0) {
      node.li.classList.add("leaf");
      node.li.classList.remove("open");
      node.ul.hidden = true;
    }
  } catch (err) {
    loadingRow.remove();
    node.ul.append(statusRow(node, "err", err.message));
    node.loaded = false;
  } finally {
    node.loading = false;
  }
}

async function expand(node) {
  if (node.li.classList.contains("leaf")) return;
  node.li.classList.add("open");
  node.ul.hidden = false;
  if (!node.loaded) await loadChildren(node);
}

function collapse(node) {
  node.li.classList.remove("open");
  node.ul.hidden = true;
}

function toggle(node) {
  if (node.li.classList.contains("open")) collapse(node);
  else expand(node);
}

function selectNode(node, { focusTree = false, scroll = true } = {}) {
  if (state.selected) state.selected.row.classList.remove("selected");
  state.selected = node;
  node.row.classList.add("selected");
  if (scroll) node.row.scrollIntoView({ block: "nearest" });
  if (focusTree) $("#tree").focus();
  showEntry(node.dn);
  if ($(".tabs .active").dataset.tab !== "entry" && $(".tabs .active").dataset.tab !== "search") switchTab("entry");
}

/** Expand the tree down to `dn` (loading each level) and select it. */
async function reveal(dn) {
  const target = normDn(dn);
  let node = state.nodes.get(target);
  if (!node) {
    const root = state.roots.find((r) => isDnUnder(dn, r));
    if (root === undefined) {
      // Outside every naming context: just show it.
      showEntry(dn);
      switchTab("entry");
      return;
    }
    const rdns = splitDn(dn);
    const rootLen = root ? splitDn(root).length : 0;
    let current = state.nodes.get(normDn(root));
    for (let i = rdns.length - rootLen - 1; i >= 0 && current; i--) {
      const childDn = rdns.slice(i).join(",");
      await expand(current);
      let next = state.nodes.get(normDn(childDn));
      while (!next && current.cookie) {
        await loadChildren(current, true);
        current.ul.querySelector(":scope > li.more")?.remove();
        next = state.nodes.get(normDn(childDn));
      }
      current = next;
    }
    node = state.nodes.get(target);
  }
  switchTab("entry");
  if (node) selectNode(node);
  else showEntry(dn);
}

function visibleRows() {
  return $$("#tree li").filter((li) => li._node && li.offsetParent !== null).map((li) => li._node);
}

function onTreeKey(e) {
  const node = state.selected;
  if (!node) return;
  const rows = visibleRows();
  const i = rows.indexOf(node);
  const go = (n) => n && selectNode(n);
  switch (e.key) {
    case "ArrowDown":
      go(rows[i + 1]);
      break;
    case "ArrowUp":
      go(rows[i - 1]);
      break;
    case "ArrowRight":
      if (!node.li.classList.contains("open")) expand(node);
      else go(rows[i + 1]?.depth > node.depth ? rows[i + 1] : null);
      break;
    case "ArrowLeft":
      if (node.li.classList.contains("open")) collapse(node);
      else go(node.li.parentElement.closest("li")?._node);
      break;
    case "Enter":
      toggle(node);
      break;
    case "Home":
      go(rows[0]);
      break;
    case "End":
      go(rows[rows.length - 1]);
      break;
    default:
      return;
  }
  e.preventDefault();
}

/* =====================================================================
 * Entry view
 * ===================================================================== */

// Attributes whose values are DNs (so we can link them).
const DN_ATTRS = new Set(
  [
    "member", "memberOf", "manager", "directReports", "owner", "managedBy", "managedObjects",
    "seeAlso", "secretary", "roleOccupant", "uniqueMember", "creatorsName", "modifiersName",
    "distinguishedName", "objectCategory", "defaultObjectCategory", "subschemaSubentry",
    "masteredBy", "msDS-hasMasterNCs", "hasMasterNCs", "msDS-HasDomainNCs", "serverReference",
    "serverReferenceBL", "frsComputerReference", "rIDSetReferences", "fSMORoleOwner",
    "wellKnownObjects", "otherWellKnownObjects", "msDS-AuthenticatedAtDC",
    "namingContexts", "defaultNamingContext", "configurationNamingContext", "schemaNamingContext",
    "rootDomainNamingContext", "dsServiceName", "entryDN", "aliasedObjectName", "pwdPolicySubentry",
    "msDS-PSOApplied", "msDS-ResultantPSO", "lastKnownParent", "showInAddressBook", "addressBookRoots",
    "msExchHomeServerName", "homeMDB", "publicDelegates", "altRecipient",
  ].map((s) => s.toLowerCase()),
);
const IMAGE_ATTRS = new Set(["jpegphoto", "thumbnailphoto", "photo", "thumbnaillogo"]);
const MAX_VALUES = 200;

function looksLikeDn(attr, text) {
  const name = attr.toLowerCase().split(";")[0];
  if (DN_ATTRS.has(name)) {
    // wellKnownObjects is "B:32:<guid>:<dn>"
    const m = /^B:\d+:[0-9a-f]+:(.*)$/i.exec(text);
    return m ? m[1] : text;
  }
  if (!/^[a-z][\w-]*=/i.test(text) || !text.includes(",")) return null;
  return state.roots.some((r) => r && isDnUnder(text, r)) ? text : null;
}

function hexDump(b64) {
  const bin = atob(b64);
  const out = [];
  for (let i = 0; i < bin.length && i < 4096; i++) out.push(bin.charCodeAt(i).toString(16).padStart(2, "0"));
  return out.join(" ") + (bin.length > 4096 ? " …" : "");
}

function renderValue(attr, v) {
  const box = el("div", { class: "val" });
  const name = attr.toLowerCase().split(";")[0];
  if (v.b64 !== undefined) {
    if (IMAGE_ATTRS.has(name)) {
      box.append(el("img", { src: `data:image/jpeg;base64,${v.b64}`, alt: "", onerror: (e) => e.target.remove() }));
    }
    if (v.d) box.append(el("span", { class: "text mono", text: v.d }));
    else box.append(el("span", { class: "bin", text: `binary · ${v.size.toLocaleString()} bytes` }));
    const hex = el("code", { class: "hex", hidden: true });
    box.append(
      el("button", {
        class: "link tiny",
        text: "hex",
        onclick: () => {
          if (!hex.textContent) hex.textContent = hexDump(v.b64);
          hex.hidden = !hex.hidden;
        },
      }),
      el("button", {
        class: "link tiny",
        text: "copy base64",
        onclick: () => copy(v.b64, "Copied base64"),
      }),
      hex,
    );
    return box;
  }
  const dn = looksLikeDn(attr, v.v);
  if (dn) {
    box.append(el("button", { class: "link mono", text: v.v, title: "Go to entry", onclick: () => reveal(dn) }));
  } else {
    box.append(el("span", { class: "text", text: v.v }));
  }
  if (v.d && v.d !== v.v) box.append(el("span", { class: "decoded", text: v.d }));
  return box;
}

function renderAttrRow(a) {
  const vals = el("td", { class: "vals" });
  const shown = a.values.slice(0, MAX_VALUES);
  shown.forEach((v) => vals.append(renderValue(a.name, v)));
  if (a.values.length > MAX_VALUES) {
    const more = el("button", {
      class: "link more-vals",
      text: `Show all ${a.values.length.toLocaleString()} values`,
      onclick: () => {
        more.remove();
        a.values.slice(MAX_VALUES).forEach((v) => vals.append(renderValue(a.name, v)));
      },
    });
    vals.append(more);
  }
  const count = a.values.length > 1 ? ` (${a.values.length.toLocaleString()})` : "";
  return el(
    "tr",
    { dataset: { name: a.name.toLowerCase(), op: a.operational ? "1" : "" } },
    el("td", { class: "name" }, a.name + count, a.operational ? el("span", { class: "op", text: "operational" }) : null),
    vals,
  );
}

let entryRequest = 0;
async function showEntry(dn) {
  const mine = ++entryRequest;
  $("#entry-empty").hidden = true;
  const box = $("#entry");
  box.hidden = false;
  box.style.opacity = ".55";
  try {
    const entry = await state.host.request("entry", { dn });
    if (mine !== entryRequest) return;
    state.entry = entry;
    const classes = entry.attributes.find((a) => a.name.toLowerCase() === "objectclass");
    const classNames = classes ? classes.values.map((v) => v.v) : [];
    const node = state.nodes.get(normDn(entry.dn));
    const kind = node ? node.kind : guessKind(classNames);
    const rdns = splitDn(entry.dn);
    $("#entry-icon").replaceWith(Object.assign(kindIcon(kind), { id: "entry-icon" }));
    $("#entry-rdn").textContent = rdns[0] || "(Root DSE)";
    $("#entry-dn").textContent = entry.dn || "(empty DN)";
    $("#entry-classes").replaceChildren(...classNames.filter((c) => c.toLowerCase() !== "top").map((c) => el("span", { class: "chip", text: c })));
    $("#attr-body").replaceChildren(...entry.attributes.map(renderAttrRow));
    applyAttrFilter();
  } catch (err) {
    if (mine !== entryRequest) return;
    state.entry = null;
    $("#entry-rdn").textContent = splitDn(dn)[0] || dn;
    $("#entry-dn").textContent = dn;
    $("#entry-classes").replaceChildren();
    $("#attr-body").replaceChildren(el("tr", {}, el("td", { colSpan: 2, class: "vals" }, el("div", { class: "banner error", text: err.message }))));
  } finally {
    if (mine === entryRequest) box.style.opacity = "";
  }
}

function guessKind(classes) {
  const c = new Set(classes.map((x) => x.toLowerCase()));
  if (c.has("computer")) return "computer";
  if (c.has("group") || c.has("groupofnames") || c.has("groupofuniquenames") || c.has("posixgroup")) return "group";
  if (c.has("user") || c.has("person") || c.has("inetorgperson")) return "user";
  if (c.has("organizationalunit")) return "ou";
  if (c.has("domaindns") || c.has("domain") || c.has("organization")) return "domain";
  if (c.has("container")) return "container";
  return "object";
}

function applyAttrFilter() {
  const q = $("#attr-filter").value.trim().toLowerCase();
  const showOp = $("#show-operational").checked;
  for (const tr of $$("#attr-body tr")) {
    if (!tr.dataset.name) continue;
    const matchName = !q || tr.dataset.name.includes(q) || tr.textContent.toLowerCase().includes(q);
    tr.hidden = !matchName || (tr.dataset.op && !showOp && !q);
  }
}

async function copy(text, msg = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(msg);
  } catch {
    toast("Clipboard unavailable");
  }
}

/* =====================================================================
 * Search
 * ===================================================================== */

const PRESETS_AD = [
  ["All users", "(&(objectCategory=person)(objectClass=user))"],
  ["All groups", "(objectClass=group)"],
  ["All computers", "(objectClass=computer)"],
  ["Organizational units", "(objectClass=organizationalUnit)"],
  ["Disabled accounts", "(&(objectCategory=person)(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=2))"],
  ["Locked-out accounts", "(&(objectCategory=person)(objectClass=user)(lockoutTime>=1))"],
  ["Password never expires", "(&(objectCategory=person)(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=65536))"],
  ["Domain controllers", "(userAccountControl:1.2.840.113556.1.4.803:=8192)"],
  ["Admin-protected (adminCount=1)", "(adminCount=1)"],
  ["Service accounts (SPN set)", "(&(objectCategory=person)(servicePrincipalName=*))"],
  ["Kerberos pre-auth disabled", "(userAccountControl:1.2.840.113556.1.4.803:=4194304)"],
  ["Unconstrained delegation", "(&(userAccountControl:1.2.840.113556.1.4.803:=524288)(!(primaryGroupID=516)))"],
  ["Group Policy objects", "(objectClass=groupPolicyContainer)"],
  ["Nested members of a group…", "(memberOf:1.2.840.113556.1.4.1941:=CN=Domain Admins,CN=Users,{base})"],
];
const PRESETS_LDAP = [
  ["People (inetOrgPerson)", "(objectClass=inetOrgPerson)"],
  ["POSIX accounts", "(objectClass=posixAccount)"],
  ["Groups", "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames)(objectClass=posixGroup))"],
  ["Organizational units", "(objectClass=organizationalUnit)"],
  ["Entries with mail", "(mail=*)"],
  ["Everything", "(objectClass=*)"],
];

function setupPresets() {
  const sel = $("#filter-presets");
  sel.replaceChildren(el("option", { value: "", text: "Presets…" }));
  const presets = state.info.isActiveDirectory ? PRESETS_AD : PRESETS_LDAP;
  for (const [label, filter] of presets) sel.append(el("option", { value: filter, text: label }));
}

let lastSearch = null;
async function runSearch(ev) {
  ev?.preventDefault();
  const f = $("#search-form");
  const req = {
    base: f.base.value.trim(),
    filter: f.filter.value.trim(),
    scope: f.scope.value,
    attributes: f.attributes.value.split(",").map((s) => s.trim()).filter(Boolean),
    limit: Number(f.limit.value) || 500,
  };
  showError("#search-error", null);
  $("#btn-search").disabled = true;
  $("#search-status").textContent = "Searching…";
  const t0 = performance.now();
  try {
    const res = await state.host.request("search", req);
    lastSearch = { req, res };
    const secs = ((performance.now() - t0) / 1000).toFixed(2);
    $("#search-status").textContent =
      `${res.entries.length.toLocaleString()} entr${res.entries.length === 1 ? "y" : "ies"} in ${secs}s` +
      (res.truncated ? ` — limit reached` : "");
    renderResults(res, req.attributes);
  } catch (err) {
    showError("#search-error", err);
    $("#search-status").textContent = "";
  } finally {
    $("#btn-search").disabled = false;
  }
}

function resultColumns(res, requested) {
  if (requested.length) return requested.filter((a) => a.toLowerCase() !== "objectclass");
  const seen = new Map();
  for (const e of res.entries) {
    for (const a of e.attributes) {
      const k = a.name.toLowerCase();
      if (k !== "objectclass" && !seen.has(k)) seen.set(k, a.name);
    }
  }
  return [...seen.values()];
}

function renderResults(res, requested) {
  const cols = resultColumns(res, requested);
  const thead = $("#results thead");
  const tbody = $("#results tbody");
  thead.replaceChildren(el("tr", {}, el("th", { text: "Entry" }), cols.map((c) => el("th", { text: c }))));
  const rows = res.entries.map((e) => {
    const byName = new Map(e.attributes.map((a) => [a.name.toLowerCase(), a]));
    return el(
      "tr",
      { onclick: () => reveal(e.dn), title: e.dn },
      el("td", {}, el("span", { class: "dn" }, kindIcon(e.kind), splitDn(e.dn)[0] || e.dn)),
      cols.map((c) => {
        const a = byName.get(c.toLowerCase());
        const text = a ? a.values.map((v) => v.d || v.v || `‹binary ${v.size}B›`).join("; ") : "";
        return el("td", { text, title: text });
      }),
    );
  });
  tbody.replaceChildren(...rows);
  $("#btn-export-csv").disabled = $("#btn-export-ldif").disabled = res.entries.length === 0;
  state.searchResults = { res, cols };
}

/* =====================================================================
 * Server and schema tabs
 * ===================================================================== */

function renderServer() {
  const info = state.info;
  const dse = info.rootDSE || {};
  const pick = (k) => {
    const key = Object.keys(dse).find((x) => x.toLowerCase() === k.toLowerCase());
    return key ? dse[key] : [];
  };
  const levels = { 0: "2000", 1: "2003 interim", 2: "2003", 3: "2008", 4: "2008 R2", 5: "2012", 6: "2012 R2", 7: "2016", 10: "2025" };
  const stats = [
    ["Server", info.server],
    ["Directory", info.isActiveDirectory ? "Microsoft Active Directory" : info.vendor || "Unknown"],
    ["Version", pick("vendorVersion")[0]],
    ["Bound as", info.whoami ? info.whoami.replace(/^(dn|u):/, "") : "anonymous"],
    ["Encryption", info.tls ? "TLS" : "None"],
    ["DNS host name", pick("dnsHostName")[0]],
    ["Domain functional level", pick("domainFunctionality")[0] && (levels[pick("domainFunctionality")[0]] || pick("domainFunctionality")[0])],
    ["Forest functional level", pick("forestFunctionality")[0] && (levels[pick("forestFunctionality")[0]] || pick("forestFunctionality")[0])],
    ["Global catalog", pick("isGlobalCatalogReady")[0]],
    ["LDAP versions", pick("supportedLDAPVersion").join(", ")],
    ["SASL mechanisms", pick("supportedSASLMechanisms").join(", ")],
    ["Naming contexts", pick("namingContexts").length || ""],
  ].filter(([, v]) => v !== undefined && v !== null && v !== "");
  $("#server-summary").replaceChildren(
    ...stats.map(([k, v]) => el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: String(v) }))),
  );
  const rows = Object.keys(dse)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }))
    .map((k) => renderAttrRow({ name: k, operational: false, values: dse[k].map((v) => ({ v })) }));
  $("#rootdse-body").replaceChildren(...rows);
}

async function ensureSchema() {
  if (state.schema) return state.schema;
  $("#schema-count").textContent = "Loading schema…";
  try {
    state.schema = await state.host.request("schema");
  } catch (err) {
    $("#schema-count").textContent = err.message;
    throw err;
  }
  return state.schema;
}

async function renderSchemaList() {
  let schema;
  try {
    schema = await ensureSchema();
  } catch {
    return;
  }
  const kind = $("#schema-kind").value;
  const q = $("#schema-filter").value.trim().toLowerCase();
  const items = schema[kind].filter((x) => !q || x.name.toLowerCase().includes(q) || x.oid.includes(q));
  $("#schema-count").textContent = `${items.length.toLocaleString()} of ${schema[kind].length.toLocaleString()}`;
  const list = $("#schema-list");
  list.replaceChildren(
    ...items.slice(0, 2000).map((x) =>
      el("li", {
        text: x.name,
        title: x.oid,
        onclick: (e) => {
          $$("#schema-list li.selected").forEach((n) => n.classList.remove("selected"));
          e.currentTarget.classList.add("selected");
          renderSchemaDetail(kind, x);
        },
      }),
    ),
  );
}

function schemaLink(name) {
  return el("button", {
    class: "link",
    text: name,
    onclick: () => {
      const isClass = state.schema.objectClasses.some((c) => c.name.toLowerCase() === name.toLowerCase());
      $("#schema-kind").value = isClass ? "objectClasses" : "attributeTypes";
      $("#schema-filter").value = name;
      renderSchemaList().then(() => $("#schema-list li")?.click());
    },
  });
}

function joinLinks(names) {
  const out = [];
  (names || []).forEach((n, i) => {
    if (i) out.push(", ");
    out.push(schemaLink(n));
  });
  return out.length ? out : ["—"];
}

function renderSchemaDetail(kind, x) {
  const d = $("#schema-detail");
  d.classList.remove("empty");
  const rows =
    kind === "objectClasses"
      ? [
          ["OID", x.oid],
          ["Kind", x.kind],
          ["Superior", joinLinks(x.sup)],
          ["Must contain", joinLinks(x.must)],
          ["May contain", joinLinks(x.may)],
        ]
      : [
          ["OID", x.oid],
          ["Syntax", x.syntax || "—"],
          ["Single-valued", x.single ? "yes" : "no"],
        ];
  d.replaceChildren(
    el("h2", { text: x.name }),
    x.description ? el("p", { class: "muted", text: String(x.description) }) : null,
    el("dl", {}, rows.map(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)])),
    kind === "objectClasses"
      ? el("button", {
          class: "ghost",
          style: "margin-top:16px",
          text: `Search for ${x.name} entries`,
          onclick: () => {
            $("#search-form").filter.value = `(objectClass=${x.name})`;
            switchTab("search");
            runSearch();
          },
        })
      : null,
  );
}

/* =====================================================================
 * Tabs, splitter, wiring
 * ===================================================================== */

function switchTab(name) {
  for (const b of $$(".tabs button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of $$(".tab-panel")) p.hidden = p.dataset.panel !== name;
  if (name === "schema" && !state.schema) renderSchemaList();
}

function setupSplitter() {
  const pane = $(".tree-pane");
  const width = store.get("treeWidth", null);
  if (width) pane.style.width = `${width}px`;
  const bar = $("#splitter");
  bar.addEventListener("pointerdown", (e) => {
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("drag");
    const startX = e.clientX;
    const startW = pane.getBoundingClientRect().width;
    const move = (ev) => {
      const w = Math.max(200, Math.min(window.innerWidth - 360, startW + ev.clientX - startX));
      pane.style.width = `${w}px`;
    };
    const up = () => {
      bar.classList.remove("drag");
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", up);
      store.set("treeWidth", Math.round(pane.getBoundingClientRect().width));
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up);
  });
}

async function applyHashTarget() {
  const raw = decodeURIComponent(location.hash.slice(1) || "");
  if (!raw) return;
  const t = parseTarget(raw);
  const profiles = await loadProfiles();
  const match = profiles.find((p) => p.name === raw || parseTarget(p.host).host.toLowerCase() === t.host.toLowerCase());
  if (match) {
    fillForm(match);
    renderProfiles(match.name);
  }
  const f = $("#connect-form");
  f.host.value = t.host;
  if (t.port) f.port.value = t.port;
  if (t.security) f.security.value = t.security;
  // ldap:// means port 389: prefer StartTLS over the LDAPS default.
  else if (/^(?:ext\+|web\+)?ldap:/i.test(raw) && f.security.value === "ldaps") f.security.value = "starttls";
  if (t.base) f.base.value = t.base;
  syncFormHints();
  (f.auth.value === "anonymous" ? f.user : f.user.value ? f.password : f.user).focus();
}

function init() {
  const f = $("#connect-form");
  f.addEventListener("submit", connect);
  f.security.addEventListener("change", syncFormHints);
  f.auth.addEventListener("change", syncFormHints);
  f.host.addEventListener("change", () => {
    const t = parseTarget(f.host.value);
    if (t.security) f.security.value = t.security;
    if (t.port && !f.port.value) f.port.value = t.port;
    if (t.base && !f.base.value) f.base.value = t.base;
    syncFormHints();
  });
  $("#btn-save").addEventListener("click", saveCurrentProfile);
  $("#btn-disconnect").addEventListener("click", disconnect);

  $("#tree").addEventListener("keydown", onTreeKey);
  $("#goto-dn").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) reveal(e.target.value.trim());
  });

  for (const b of $$(".tabs button")) b.addEventListener("click", () => switchTab(b.dataset.tab));

  $("#attr-filter").addEventListener("input", applyAttrFilter);
  $("#show-operational").checked = store.get("showOperational", false);
  $("#show-operational").addEventListener("change", (e) => {
    store.set("showOperational", e.target.checked);
    applyAttrFilter();
  });
  $("#btn-copy-dn").addEventListener("click", () => state.entry && copy(state.entry.dn, "DN copied"));
  $("#btn-ldif").addEventListener("click", () => {
    if (!state.entry) return;
    download(`${safeFileName(splitDn(state.entry.dn)[0])}.ldif`, toLdif([state.entry]));
  });
  $("#btn-search-here").addEventListener("click", () => {
    if (!state.selected) return;
    $("#search-form").base.value = state.selected.dn;
    switchTab("search");
    $("#search-form").filter.focus();
  });
  $("#btn-refresh").addEventListener("click", () => {
    const n = state.selected;
    if (!n) return;
    showEntry(n.dn);
    if (n.loaded || n.li.classList.contains("leaf")) {
      n.li.classList.remove("leaf");
      n.loaded = false;
      if (n.li.classList.contains("open")) loadChildren(n);
    }
  });

  $("#search-form").addEventListener("submit", runSearch);
  $("#filter-presets").addEventListener("change", (e) => {
    if (!e.target.value) return;
    const base = state.info?.defaultNamingContext || state.roots[0] || "";
    $("#search-form").filter.value = e.target.value.replace("{base}", base);
    e.target.value = "";
    $("#search-form").filter.focus();
  });
  $("#btn-export-csv").addEventListener("click", () => {
    if (!state.searchResults) return;
    const { res, cols } = state.searchResults;
    download("ldap-search.csv", toCsv(res.entries, cols), "text/csv");
  });
  $("#btn-export-ldif").addEventListener("click", () => {
    if (state.searchResults) download("ldap-search.ldif", toLdif(state.searchResults.res.entries));
  });

  $("#schema-kind").addEventListener("change", renderSchemaList);
  $("#schema-filter").addEventListener("input", renderSchemaList);

  setupSplitter();
  syncFormHints();
  renderProfiles();
  applyHashTarget();
  window.addEventListener("hashchange", applyHashTarget);

  // Probe the helper so a missing install is reported before the user types a password.
  const probe = new LdapHost();
  probe
    .request("hello")
    .catch((err) => {
      if (err instanceof HostMissingError) {
        $("#host-missing").hidden = false;
        $("#host-missing-detail").textContent = err.message;
      }
    })
    .finally(() => probe.close());
}

init();
