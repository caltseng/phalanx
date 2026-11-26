(() => {
  "use strict";

  const TOKEN = /^[A-Za-z0-9_-]{32,}$/;
  const { readSignal } = window.PhalanxOperatorHelpers;

  function client(token) {
    const snapshots = new Map();

    function request(path, { headers = {}, ...options } = {}) {
      return fetch(path, {
        redirect: "error", cache: "no-store", credentials: "omit",
        ...options,
        headers: { ...headers, ...(token ? { Authorization: "Bearer " + token } : {}) }
      });
    }

    async function read(path, signal, accept = "application/json") {
      const headers = { Accept: accept };
      const cached = snapshots.get(path);
      if (cached) headers["If-None-Match"] = cached.etag;
      const response = await request(path, { signal, headers });
      if (response.status === 304) {
        if (!cached) throw new Error("The server returned 304 without a saved response.");
        return cached.body;
      }
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          throw new Error(token ? "The server rejected the owner token." : "The server requires an owner token.");
        }
        if (response.status === 404) {
          throw new Error("This resource is unavailable on the server.");
        }
        throw new Error("The server returned HTTP " + response.status + ".");
      }
      if (response.headers.get("content-type")?.split(";")[0] !== accept) {
        throw new Error("The server returned an unexpected response type.");
      }
      const body = await (accept === "application/json" ? response.json() : response.text());
      const etag = response.headers.get("etag");
      if (etag) {
        snapshots.delete(path);
        snapshots.set(path, { etag, body });
        if (snapshots.size > 64) snapshots.delete(snapshots.keys().next().value);
      } else snapshots.delete(path);
      return body;
    }

    function write(path, { headers = {}, ...options }) {
      return request(path, {
        signal: readSignal(),
        ...options,
        headers: { Accept: "application/json", "Content-Type": "application/json", ...headers }
      });
    }

    return { read, write };
  }

  window.PhalanxOperatorHttp = { client, validToken: token => TOKEN.test(token) };
})();
