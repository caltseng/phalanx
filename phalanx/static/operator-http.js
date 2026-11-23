(() => {
  "use strict";

  const ID = /^[0-9a-f]{32}$/;
  const TOKEN = /^[A-Za-z0-9_-]{32,}$/;
  const CURSOR = /^[A-Za-z0-9_-]{1,512}$/;
  const EXECUTIONS = "/v1/operator/executions";

  function client(token) {
    async function read(path, signal, accept = "application/json") {
      const headers = { Accept: accept };
      if (token) headers.Authorization = "Bearer " + token;
      const response = await fetch(path, {
        signal,
        redirect: "error",
        cache: "no-store",
        credentials: "omit",
        headers
      });
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

    function httpsUrl(value) {
      try {
        const url = new URL(value);
        if (url.protocol === "https:" && !url.username && !url.password) return url.href;
      } catch {
        // Missing or invalid link.
      }
      return null;
    }

    function epochMs(value) {
      return Number.isSafeInteger(value) && value > 0 ? value : null;
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
        const url = httpsUrl(row.output.url);
        if (url) output = { kind: "pr", url };
      }
      return {
        id, itemId, output,
        version: row.version,
        title: row.title,
        repo: row.repository_id,
        stage: row.stage,
        status: row.status,
        lastAdvancedAt: row.last_advanced_at,
        waitReason: row.wait_reason,
        retryAt: epochMs(row.retry_at_ms),
        recheckAt: epochMs(row.recheck_at_ms),
        workerRef: typeof row.worker_reference === "string" ? row.worker_reference : null,
        chatUrl: httpsUrl(row.worker_url),
        slotNote: typeof row.stage_owner === "string"
          ? (row.slot_retained ? "Retained slot" : "Admitted") + "; " + row.stage + " attempt " + row.stage_owner.slice(0, 8)
          : typeof row.capacity_release === "string" ? "Slot released: " + row.capacity_release : null,
        primaryPrUrl: httpsUrl(row.primary_pr_url),
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
          question: page.question ? {
            prompt: page.question.prompt,
            options: Array.isArray(page.question.options) ? page.question.options : [],
            proposedDefault: page.question.proposed_default
          } : null,
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

  function show(source) {
    source.openReport = output => openReport(source, output);
    unmount = window.PhalanxOperatorBoard.mount(board, source);
    form.hidden = true;
    board.hidden = false;
    status.textContent = "";
    const heading = board.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus();
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
      show(source);
      disconnect.hidden = false;
    } catch {
      status.textContent = "Could not verify owner access. Check the token and server, then try again.";
    } finally {
      connect.disabled = false;
      input.disabled = false;
      if (!form.hidden) input.focus();
    }
  });

  (async () => {
    // A local board opens without a token; otherwise the server returns 401 and the form appears.
    const source = client(null);
    try {
      await source.list({ kind: "open", cursor: null, limit: 1, signal: AbortSignal.timeout(12000) });
      show(source);
    } catch (error) {
      form.hidden = false;
      status.textContent = error.message === "The server requires an owner token."
        ? "" : "Could not load the board without a token: " + error.message;
      input.focus();
    }
  })();

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
