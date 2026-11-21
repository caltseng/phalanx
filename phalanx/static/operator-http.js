(() => {
  "use strict";

  const ID = /^[0-9a-f]{32}$/;
  const TOKEN = /^[A-Za-z0-9_-]{32,}$/;
  const CURSOR = /^[A-Za-z0-9_-]{1,512}$/;
  const EXECUTIONS = "/v1/operator/executions";

  function client(token) {
    async function read(path, signal, accept = "application/json") {
      const response = await fetch(path, {
        signal,
        redirect: "error",
        cache: "no-store",
        credentials: "omit",
        headers: { Authorization: "Bearer " + token, Accept: accept }
      });
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          throw new Error("The server rejected the owner token.");
        }
        if (response.status === 404) {
          throw new Error("This resource is unavailable on the server.");
        }
        throw new Error("The server returned HTTP " + response.status + ".");
      }
      if (response.headers.get("content-type")?.split(";")[0] !== accept) {
        throw new Error("The server returned an unexpected response type.");
      }
      return accept === "application/json" ? response.json() : response.text();
    }

    function query(limit, cursor) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 ||
          (cursor !== null && !CURSOR.test(cursor))) {
        throw new Error("Invalid page request.");
      }
      const params = new URLSearchParams({ limit: String(limit) });
      if (cursor !== null) params.set("cursor", cursor);
      return params;
    }

    function nextCursor(value) {
      if (value === null || (typeof value === "string" && CURSOR.test(value))) return value;
      throw new Error("Invalid page cursor returned by the server.");
    }

    function pageRows(rows, limit) {
      if (!Array.isArray(rows) || rows.length > limit) throw new Error("Invalid page size.");
      return rows;
    }

    function pullRequestUrl(value) {
      try {
        const url = new URL(value);
        if (url.protocol === "https:" && !url.username && !url.password) return url.href;
      } catch {
        // Missing or invalid PR reference.
      }
      return null;
    }

    function identifier(value) {
      if (typeof value !== "string" || !ID.test(value)) throw new Error("Invalid execution reference.");
      return value;
    }

    function item(row) {
      const id = identifier(row.id);
      const itemId = identifier(row.item_id);
      let output;
      if (row.output?.kind === "report") {
        output = { kind: "report", itemId, id };
      } else if (row.output?.kind === "pr") {
        const url = pullRequestUrl(row.output.url);
        if (url) output = { kind: "pr", url };
      }
      return {
        id, output,
        title: row.title,
        repo: row.repository_id,
        stage: row.stage,
        status: row.status,
        lastAdvancedAt: row.last_advanced_at,
        waitReason: row.wait_reason,
        retryAt: Number.isSafeInteger(row.retry_at_ms) && row.retry_at_ms > 0 ? row.retry_at_ms : null,
        primaryPrUrl: pullRequestUrl(row.primary_pr_url),
        closedAt: row.closed_at,
        outcome: row.outcome
      };
    }

    return {
      owner: signal => read("/v1/owner/status", signal),
      async list({ kind, cursor, limit, signal }) {
        if (kind !== "open" && kind !== "closed") throw new Error("Invalid work list.");
        const params = query(limit, cursor);
        params.set("kind", kind);
        const page = await read(EXECUTIONS + "?" + params, signal);
        return { items: pageRows(page.items, limit).map(item), nextCursor: nextCursor(page.next_cursor) };
      },
      async detail({ id, cursor, limit, signal }) {
        const params = query(limit, cursor);
        const page = await read(EXECUTIONS + "/" + identifier(id) + "/timeline?" + params, signal);
        return {
          item: item(page.item),
          attempt: page.attempt,
          resumeTrigger: page.item.resume_trigger,
          dependencies: pageRows(page.prerequisites ?? [], 100).map(dependency => ({
            title: dependency.title || identifier(dependency.item_id),
            milestone: dependency.milestone,
            status: "waiting",
            reason: dependency.reason
          })),
          events: pageRows(page.events, limit).map(event => ({
            at: event.created_at, fromStage: event.from_stage,
            stage: event.stage, status: event.status, reason: event.reason
          })),
          nextCursor: nextCursor(page.next_cursor)
        };
      },
      report({ itemId, id }, signal) {
        const path = "/v1/items/" + identifier(itemId) + "/executions/" + identifier(id) + "/report";
        return read(path, signal, "text/plain");
      }
    };
  }

  const form = document.querySelector("#operator-connect");
  const input = document.querySelector("#operator-token");
  const connect = form.querySelector("button");
  const disconnect = document.querySelector("#operator-disconnect");
  const status = document.querySelector("#operator-session-status");
  const board = document.querySelector("#operator-board");
  const dialog = document.querySelector("#operator-report");
  const reportStatus = document.querySelector("#operator-report-status");
  const reportContent = document.querySelector("#operator-report-content");
  let unmount;
  let reportRead;

  dialog.querySelector("button").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    // A queued close event can arrive after a new report opens.
    if (!dialog.open) clearReport();
  });

  function clearReport() {
    reportRead?.abort();
    reportRead = undefined;
    reportStatus.textContent = "";
    reportContent.textContent = "";
  }

  async function openReport(source, output) {
    clearReport();
    const request = new AbortController();
    reportRead = request;
    reportStatus.textContent = "Loading report…";
    dialog.showModal();
    try {
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(12000)]);
      const markdown = await source.report(output, signal);
      if (reportRead === request && dialog.open) {
        reportStatus.textContent = "";
        reportContent.textContent = markdown;
      }
    } catch {
      if (reportRead === request && dialog.open) {
        reportStatus.textContent = "Could not load the report. Close it and try again.";
      }
    }
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (connect.disabled) return;
    const token = input.value.trim();
    input.value = "";
    if (!TOKEN.test(token)) {
      status.textContent = "Enter a valid Phalanx owner token.";
      input.focus();
      return;
    }
    connect.disabled = true;
    input.disabled = true;
    status.textContent = "Checking owner access…";
    try {
      const source = client(token);
      const access = await source.owner(AbortSignal.timeout(12000));
      if (access.role !== "owner") throw new Error("Owner access required.");
      source.openReport = output => openReport(source, output);
      unmount = window.PhalanxOperatorBoard.mount(board, source);
      form.hidden = true;
      board.hidden = false;
      disconnect.hidden = false;
      status.textContent = "";
      const heading = board.querySelector("h1");
      heading.tabIndex = -1;
      heading.focus();
    } catch {
      status.textContent = "Could not verify owner access. Check the token and server, then try again.";
    } finally {
      connect.disabled = false;
      input.disabled = false;
      if (!form.hidden) input.focus();
    }
  });

  disconnect.addEventListener("click", () => {
    if (dialog.open) dialog.close();
    clearReport();
    unmount?.();
    unmount = undefined;
    board.hidden = true;
    disconnect.hidden = true;
    form.hidden = false;
    status.textContent = "Disconnected.";
    input.focus();
  });
})();
