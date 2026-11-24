(() => {
  "use strict";

  const ID = /^[0-9a-f]{32}$/;
  const TOKEN = /^[A-Za-z0-9_-]{32,}$/;
  const CURSOR = /^[A-Za-z0-9_-]{1,512}$/;
  const EXECUTIONS = "/v1/operator/executions";

  const filters = new URLSearchParams(location.search);
  const FILTERS = ["repository", "label", "status", "stage", "outcome", "q", "closed_after"];
  const route = /^\/operator\/(items|executions)\/([0-9a-f]{32})$/.exec(location.pathname);
  const page = route ? "detail" : location.pathname === "/operator/queue" ? "queue"
    : location.pathname === "/operator/history" ? "history" : "home";

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
      const merge = row.delivery?.merged ? row.delivery : null;
      const delivery = merge && typeof merge.merge_commit === "string" ? {
        mergeCommit: merge.merge_commit,
        method: typeof merge.merge_method === "string" ? merge.merge_method : null,
        mergedAt: typeof merge.merged_at === "string" ? merge.merged_at : null
      } : undefined;
      return {
        id, itemId, output, delivery,
        version: row.version,
        title: row.title,
        goal: row.goal,
        target: row.target,
        publicationMode: row.publication_mode,
        repo: row.repository_id,
        stage: row.stage,
        status: row.status,
        createdAt: row.created_at,
        lastAdvancedAt: row.last_advanced_at,
        lastProgressAt: row.last_progress_at,
        waitSince: row.wait_since,
        waitReason: row.wait_reason,
        resumeTrigger: row.resume_trigger,
        retryAt: epochMs(row.retry_at_ms),
        recheckAt: epochMs(row.recheck_at_ms),
        // Preserved worker references on historical attempts.
        historicalWorker: {
          workerRef: typeof row.worker_reference === "string" ? row.worker_reference : null,
          chatUrl: httpsUrl(row.worker_url)
        },
        primaryPrUrl: httpsUrl(row.primary_pr_url),
        prState: row.pr?.state,
        closedAt: row.closed_at,
        outcome: row.outcome,
        writer: row.writer ? {
          state: row.writer.state,
          runtime: row.writer.runtime,
          captainId: row.writer.captain_id,
          sessionId: row.writer.session_id,
          sessionUrl: httpsUrl(row.writer.session_url),
          lastProgressAt: row.writer.last_progress_at,
          progressNote: row.writer.progress_note
        } : null
      };
    }

    function captain(row) {
      return {
        id: row.id, sessionRef: row.session_ref, state: row.state, live: row.live === true,
        stale: row.stale === true, heartbeatAt: row.heartbeat_at, wakeCount: row.wake_count,
        compactionCount: Number.isSafeInteger(row.compaction_count) && row.compaction_count >= 0
          ? row.compaction_count : null,
        charterRevision: typeof row.charter_revision === "string" ? row.charter_revision : null,
        focus: Array.isArray(row.focus) ? row.focus : [], handoffNote: row.handoff_note
      };
    }

    function flag(row) {
      return {
        kind: row.kind, itemId: row.item_id, executionId: row.execution_id, captainId: row.captain_id,
        reason: row.reason, recovery: row.recovery, firstSeen: row.first_seen,
        notifyOwner: row.notify_owner === true
      };
    }

    return {
      owner: signal => read("/v1/owner/status", signal),
      boardAccess: signal => read(EXECUTIONS + "?limit=1", signal),
      capacity: signal => read("/v1/dispatch/settings", signal),
      async saveCapacity(maximum) {
        const response = await fetch("/v1/dispatch/settings", {
          method: "PUT", redirect: "error", cache: "no-store", credentials: "omit",
          signal: AbortSignal.timeout(12000),
          headers: { Accept: "application/json", "Content-Type": "application/json",
            Authorization: "Bearer " + token },
          body: JSON.stringify({ max_writers: maximum })
        });
        if (!response.ok) throw new Error("Save rejected (HTTP " + response.status + ").");
        return response.json();
      },
      repositories: signal => read("/v1/repositories", signal),
      async create(payload, key) {
        const response = await fetch("/v1/items", {
          method: "POST", redirect: "error", cache: "no-store", credentials: "omit",
          signal: AbortSignal.timeout(12000),
          headers: { Accept: "application/json", "Content-Type": "application/json",
            Authorization: "Bearer " + token, "Idempotency-Key": key },
          body: payload
        });
        if (!response.ok) {
          const error = new Error("The server returned HTTP " + response.status + ".");
          // A definitive rejection lets the user correct the form. Unknown outcomes
          // retain the exact payload and key, including when a response cannot be read.
          if ([400, 401, 403, 404, 409, 422].includes(response.status)) {
            const body = await response.json();
            error.rejected = true;
            error.detail = body.detail;
          }
          throw error;
        }
        const row = await response.json();
        return {
          id: row.execution.id, itemId: row.id, title: row.goal.split("\n")[0], goal: row.goal,
          repo: row.repository_id, target: row.execution.state.target,
          publicationMode: row.execution.state.publication_mode, version: row.execution.version,
          status: row.execution.state.status, stage: row.execution.state.stage,
          createdAt: row.execution.created_at, lastAdvancedAt: row.execution.updated_at
        };
      },
      async list({ kind, cursor, limit, signal }) {
        if (kind !== "open" && kind !== "closed") throw new Error("Invalid work list.");
        const params = query(limit, cursor);
        params.set("kind", kind);
        for (const name of FILTERS) {
          const value = filters.get(name);
          if (!value) continue;
          params.set(name, value);
        }
        const result = await read(EXECUTIONS + "?" + params, signal);
        return { items: pageRows(result.items, limit).map(item), nextCursor: nextCursor(result.next_cursor) };
      },
      async currentItem(id, signal) {
        const result = await read("/v1/operator/items/" + identifier(id), signal);
        return item(result.item);
      },
      async detail({ id, cursor, limit, signal }) {
        const params = query(limit, cursor);
        const page = await read(EXECUTIONS + "/" + identifier(id) + "/timeline?" + params, signal);
        return {
          item: item(page.item),
          nextAction: page.item.next_action,
          previousExecutionId: page.previous_execution_id ? identifier(page.previous_execution_id) : null,
          nextExecutionId: page.next_execution_id ? identifier(page.next_execution_id) : null,
          attempt: page.attempt,
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
            at: event.created_at, event: event.event, fromStage: event.from_stage,
            stage: event.stage, status: event.status, reason: event.reason
          })),
          nextCursor: nextCursor(page.next_cursor)
        };
      },
      report({ itemId, id }, signal) {
        const path = "/v1/items/" + identifier(itemId) + "/executions/" + identifier(id) + "/report";
        return read(path, signal, "text/plain");
      },
      async captains({ signal }) {
        const page = await read("/v1/captains?limit=25", signal);
        return { captains: pageRows(page.captains, 25).map(captain), nextCursor: null };
      },
      async flags({ signal }) {
        const page = await read("/v1/flags", signal);
        return { flags: Array.isArray(page.flags) ? page.flags.map(flag) : [], nextCursor: null };
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
  let clearCreate;
  let clearSettings;
  let reportRead;

  function concurrency(source, authenticated) {
    const panel = document.querySelector("#operator-settings");
    const form = document.querySelector("#operator-settings-form");
    const maximum = document.querySelector("#operator-max-writers");
    const token = document.querySelector("#operator-settings-token");
    const capacity = document.querySelector("#operator-capacity");
    const status = document.querySelector("#operator-settings-status");
    const refresh = document.querySelector("#operator-settings-refresh");
    const submit = form.querySelector("button");
    let disposed = false;
    let busy = false;
    let generation = 0;
    panel.hidden = false;
    document.querySelector("#operator-settings-auth").hidden = authenticated;
    function display(value) {
      capacity.textContent = value.active_writers + " active / " + value.max_writers + " maximum writer sessions.";
      maximum.value = String(value.max_writers);
    }
    async function load() {
      if (busy) return;
      const current = ++generation;
      status.textContent = "Loading capacity...";
      try {
        const value = await source.capacity(AbortSignal.timeout(12000));
        if (disposed || generation !== current) return;
        display(value);
        status.textContent = "";
      } catch (error) {
        if (!disposed && generation === current) status.textContent = "Could not load capacity. " + error.message;
      }
    }
    const toggle = () => { if (panel.open) load(); };
    async function save(event) {
      event.preventDefault();
      if (busy) return;
      const value = Number(maximum.value);
      if (!Number.isSafeInteger(value) || value < 1 || value > 2147483647) {
        status.textContent = "Enter a whole number from 1 to 2147483647.";
        maximum.focus();
        return;
      }
      const secret = token.value.trim();
      token.value = "";
      if (!authenticated && !TOKEN.test(secret)) {
        status.textContent = "Enter a valid Phalanx owner token to save.";
        token.focus();
        return;
      }
      busy = true;
      generation++;
      submit.disabled = true;
      refresh.disabled = true;
      status.textContent = "Saving concurrency...";
      try {
        const result = await (authenticated ? source : client(secret)).saveCapacity(value);
        if (disposed) return;
        display(result);
        status.textContent = "Concurrency saved.";
      } catch (error) {
        if (!disposed) status.textContent = "Could not confirm the save. " + error.message +
          " Refresh capacity before retrying.";
      } finally {
        busy = false;
        submit.disabled = false;
        refresh.disabled = false;
      }
    }
    panel.addEventListener("toggle", toggle);
    refresh.addEventListener("click", load);
    form.addEventListener("submit", save);
    if (panel.open) load();
    return () => {
      disposed = true;
      generation++;
      panel.hidden = true;
      token.value = "";
      maximum.value = "";
      panel.removeEventListener("toggle", toggle);
      refresh.removeEventListener("click", load);
      form.removeEventListener("submit", save);
    };
  }

  function newItem(source, authenticated) {
    const modal = document.querySelector("#operator-create");
    const auth = document.querySelector("#operator-create-auth");
    const password = document.querySelector("#operator-create-token");
    const form = document.querySelector("#operator-create-form");
    const fields = document.querySelector("#operator-create-fields");
    const submit = document.querySelector("#operator-create-submit");
    const close = document.querySelector("#operator-create-close");
    const status = document.querySelector("#operator-create-status");
    const errors = document.querySelector("#operator-create-errors");
    const repository = form.elements.repository_id;
    const target = form.elements.target;
    const label = window.PhalanxOperatorBoard.label;
    target.replaceChildren(...["checked_in", "ready_for_check_in", "report"].map(value => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label(value);
      return option;
    }));
    let writer = authenticated ? source : null;
    let pending = null;
    let busy = false;
    let generation = 0;
    let trigger;
    let acknowledge;
    let disposed = false;

    function clearErrors() {
      errors.replaceChildren();
      for (const field of fields.elements) {
        field.removeAttribute("aria-invalid");
        if (field.getAttribute("aria-describedby") === errors.id) field.removeAttribute("aria-describedby");
      }
    }

    function showErrors(detail) {
      clearErrors();
      const messages = Array.isArray(detail) ? detail : [{ msg: String(detail) }];
      for (const error of messages) {
        const name = error.loc?.[0] === "body" ? error.loc[1] : null;
        const field = name && form.elements.namedItem(name);
        const message = document.createElement("p");
        const caption = field && form.querySelector('label[for="' + field.id + '"]');
        message.textContent = (caption ? caption.textContent + ": " : "") + error.msg;
        errors.append(message);
        if (field) {
          field.setAttribute("aria-invalid", "true");
          if (!field.hasAttribute("aria-describedby")) field.setAttribute("aria-describedby", errors.id);
        }
      }
      if (modal.open) errors.focus({ preventScroll: true });
    }

    async function unlock() {
      const current = ++generation;
      auth.querySelector("button").disabled = true;
      status.textContent = "Loading registered repositories...";
      try {
        const access = writer;
        await access.owner(AbortSignal.timeout(12000));
        const rows = await access.repositories(AbortSignal.timeout(12000));
        if (disposed || generation !== current || !modal.open) return;
        const selected = repository.value;
        repository.replaceChildren(...rows.map(row => {
          const option = document.createElement("option");
          option.value = row.id;
          option.textContent = row.id;
          return option;
        }));
        if (rows.some(row => row.id === selected)) repository.value = selected;
        auth.hidden = true;
        form.hidden = false;
        fields.disabled = Boolean(pending);
        submit.disabled = busy || !rows.length;
        status.textContent = pending ? "The previous outcome is unknown. Retry the unchanged request safely."
          : rows.length ? "" : "No registered repositories. Register one through the owner API or CLI first.";
        if (!busy) (pending ? submit : repository).focus({ preventScroll: true });
      } catch (error) {
        if (disposed || generation !== current || !modal.open) return;
        writer = authenticated ? source : null;
        auth.hidden = authenticated;
        form.hidden = true;
        status.textContent = "Could not unlock creation. " + error.message + " Close and try again.";
      } finally {
        auth.querySelector("button").disabled = false;
      }
    }

    function open(button, onCreated) {
      trigger = button;
      acknowledge = onCreated;
      clearErrors();
      status.textContent = "";
      auth.hidden = Boolean(writer);
      form.hidden = true;
      modal.showModal();
      if (writer) unlock();
      else password.focus({ preventScroll: true });
    }

    function onAuth(event) {
      event.preventDefault();
      const token = password.value.trim();
      password.value = "";
      if (!TOKEN.test(token)) {
        status.textContent = "Enter a valid Phalanx owner token.";
        password.focus();
        return;
      }
      writer = client(token);
      unlock();
    }

    async function onSubmit(event) {
      event.preventDefault();
      if (busy || !writer) return;
      clearErrors();
      if (!pending) {
        const lines = value => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
        const data = Object.fromEntries(new FormData(form));
        const payload = {
          repository_id: data.repository_id, goal: data.goal,
          acceptance_criteria: lines(data.acceptance_criteria),
          target: data.target, priority: data.priority, publication_mode: data.publication_mode,
          dependencies: lines(data.dependencies).map(line => {
            const [upstream_id, ...milestone] = line.split(/\s+/);
            return { upstream_id, milestone: milestone.join(" ") || "checked_in" };
          })
        };
        if (data.label.trim()) payload.label = data.label.trim();
        pending = { payload: JSON.stringify(payload), key: crypto.randomUUID() };
      }
      busy = true;
      fields.disabled = true;
      submit.disabled = true;
      status.textContent = "Queuing work item...";
      try {
        const item = await writer.create(pending.payload, pending.key);
        if (disposed) return;
        pending = null;
        form.reset();
        target.value = "checked_in";
        acknowledge(item);
        status.textContent = "";
        modal.close();
      } catch (error) {
        if (disposed) return;
        if (error.rejected) {
          pending = null;
          showErrors(error.detail);
          status.textContent = "Not queued. Correct the request and try again.";
        } else {
          status.textContent = "The outcome is unknown. Retry to safely send the same request; do not file it again.";
        }
      } finally {
        busy = false;
        if (!disposed) {
          fields.disabled = Boolean(pending);
          submit.disabled = false;
          submit.textContent = pending ? "Retry queue request" : "Queue work item";
        }
      }
    }

    function onClose() {
      if (modal.open) return;
      generation++;
      password.value = "";
      if (!authenticated) writer = null;
      trigger?.focus({ preventScroll: true });
    }
    const closeModal = () => modal.close();
    close.addEventListener("click", closeModal);
    modal.addEventListener("close", onClose);
    auth.addEventListener("submit", onAuth);
    form.addEventListener("submit", onSubmit);
    source.openNewItem = open;
    return () => {
      disposed = true;
      generation++;
      modal.close();
      writer = null;
      password.value = "";
      pending = null;
      form.reset();
      close.removeEventListener("click", closeModal);
      modal.removeEventListener("close", onClose);
      auth.removeEventListener("submit", onAuth);
      form.removeEventListener("submit", onSubmit);
    };
  }

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
    document.querySelector("#operator-report-body").scrollTop = 0;
    try {
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(12000)]);
      const markdown = await source.report(output, signal);
      if (reportRead === request && dialog.open) {
        reportStatus.textContent = "";
        reportContent.replaceChildren(window.PhalanxMarkdown.render(markdown));
      }
    } catch {
      if (reportRead === request && dialog.open) {
        reportStatus.textContent = "Could not load the report. Close it and try again.";
      }
    }
  }

  function show(source, authenticated = false) {
    source.openReport = output => openReport(source, output);
    clearCreate = newItem(source, authenticated);
    clearSettings = concurrency(source, authenticated);
    const cursorName = kind => page === "home" ? kind + "_cursor" : "cursor";
    unmount = window.PhalanxOperatorBoard.mount(board, source, {
      page,
      itemId: route?.[1] === "items" ? route[2] : null,
      executionId: route?.[1] === "executions" ? route[2] : null,
      filters: Object.fromEntries(filters),
      cursors: Object.fromEntries(["open", "closed"].map(kind => [kind,
        page === "home" || (page === "queue" ? kind === "open" : kind === "closed") ? filters.get(cursorName(kind)) : null])),
      pageChanged(kind, cursor) {
        const url = new URL(location.href);
        if (cursor) url.searchParams.set(cursorName(kind), cursor);
        else url.searchParams.delete(cursorName(kind));
        history.replaceState(null, "", url);
      }
    });
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
      show(source, true);
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
      await source.boardAccess(AbortSignal.timeout(12000));
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
    clearCreate?.();
    clearSettings?.();
    unmount?.();
    unmount = undefined;
    board.hidden = true;
    disconnect.hidden = true;
    form.hidden = false;
    status.textContent = "Disconnected.";
    input.focus();
  });
})();
