/* Thin promise-based client for the ldap_browser_host native helper.
 *
 * Every LdapHost owns one native port, which Firefox backs with its own
 * helper process, so each browser tab has an independent LDAP session.
 */

export const HOST_NAME = "ldap_browser_host";

export class HostMissingError extends Error {}

export class LdapHost {
  constructor() {
    this.port = null;
    this.nextId = 1;
    this.pending = new Map();
    this.chunks = new Map();
    this.onclose = null;
  }

  _ensurePort() {
    if (this.port) return this.port;
    const port = browser.runtime.connectNative(HOST_NAME);
    port.onMessage.addListener((msg) => this._onMessage(msg));
    port.onDisconnect.addListener((p) => {
      const why = (p && p.error && p.error.message) || "The LDAP helper exited.";
      const err = /no such native application|not found|manifest/i.test(why)
        ? new HostMissingError(why)
        : new Error(why);
      for (const { reject } of this.pending.values()) reject(err);
      this.pending.clear();
      this.chunks.clear();
      this.port = null;
      if (this.onclose) this.onclose(err);
    });
    this.port = port;
    return port;
  }

  _onMessage(msg) {
    if (msg && typeof msg.chunk === "number") {
      const parts = this.chunks.get(msg.id) || [];
      parts[msg.chunk] = msg.data;
      this.chunks.set(msg.id, parts);
      if (parts.filter((p) => p !== undefined).length < msg.of) return;
      this.chunks.delete(msg.id);
      msg = JSON.parse(parts.join(""));
    }
    const waiter = this.pending.get(msg.id);
    if (!waiter) return;
    this.pending.delete(msg.id);
    if (msg.ok) waiter.resolve(msg.result);
    else waiter.reject(new Error(msg.error || "Unknown error"));
  }

  request(op, params = {}) {
    const port = this._ensurePort();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      port.postMessage({ id, op, ...params });
    });
  }

  close() {
    if (this.port) {
      this.port.disconnect();
      this.port = null;
    }
  }
}
