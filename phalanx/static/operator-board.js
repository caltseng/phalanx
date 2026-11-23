(() => {
  "use strict";

  const PAGE_SIZE = 25;
  const REFRESH_MS = 15000;
  const STALE_MS = 45000;
  const READ_TIMEOUT_MS = 12000;
  const timestamp = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "medium"
  });

  function element(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }

  function when(value) {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime()) ? timestamp.format(date) : "Unknown";
  }

  // Durations arrive precomputed by the server, so they advance with each bounded refresh.
  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "unavailable";
    const units = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
    const total = Math.floor(seconds);
    const found = units.findIndex(([, size]) => total >= size);
    const index = found === -1 ? units.length - 1 : found;
    const [unit, size] = units[index];
    let text = Math.floor(total / size) + unit;
    const smaller = units[index + 1];
    if (smaller) {
      const rest = Math.floor((total % size) / smaller[1]);
      if (rest) text += " " + rest + smaller[0];
    }
    return text;
  }

  function since(at, seconds) {
    return at ? duration(seconds) + " (since " + when(at) + ")" : "unavailable";
  }

  function addLink(parent, label, value) {
    if (!value) return;
    let url;
    try {
      url = new URL(value, document.baseURI);
    } catch {
      return;
    }
    if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) {
      const line = element("p");
      line.append(element("a", label, { href: url.href }));
      parent.append(line);
    }
  }

  /**
   * mount(root, source) returns an unmount function. Call once per root with class "phalanx-board".
   * The host supplies the authenticated adapter and translates API fields here.
   * source.list({kind, cursor, limit, signal}) resolves to {items, nextCursor}.
   * kind is "open" or "closed". Cursors are opaque strings or null. The adapter
   * orders closed items newest first and prioritizes input waits in open work.
   * Items contain string id, title, repo, stage, status, lastAdvancedAt, waitReason?,
   * retryAt? and recheckAt? (epoch ms), workerRef? (opaque bridge reference), chatUrl?, draftUrl?,
   * primaryPrUrl?, closedAt?, outcome?, output?: {kind: "pr"|"report", url}, and for replies itemId? and version?.
   * Timing fields are server-computed seconds as of the read: ageSeconds? (since intake),
   * pending?: {at, seconds} (current admission), wait?: {at, seconds} (current wait reason), and
   * slot?: {occupiedSince, occupiedSeconds, attemptSeconds, handoffs, activity}. A null at or seconds
   * shows as unavailable.
   * Use status "waiting_for_input" for items requiring operator attention.
   * outcome is a display label, including "Checked in" vs "Ready for check-in".
   * source.detail({id, cursor, limit, signal}) resolves to
   * {item, attempt?, nextAction?, resumeTrigger?, question?, dependencies?, events, nextCursor}.
   * question is {prompt, options, proposedDefault?} while the item waits for the owner.
   * attempt is a display reference. Dependencies are {title, milestone, status}.
   * Events are newest first and contain {at, fromStage?, stage, status, reason?}.
   * lifecycle? is {coverage, note?, closedAt?, queuedSeconds, waitingSeconds, executingSeconds,
   * stages: [{stage, visits, seconds, queuedSeconds, waitingSeconds, executingSeconds, partial}],
   * visits: [{stage, enteredAt, exitedAt?, open, seconds, queuedSeconds, waitingSeconds, executingSeconds,
   * attempts, retries, returned, outcome?, reason?, partial}] oldest first, visitsOmitted}.
   * source.slots?({cursor, limit, signal}) resolves to {items, nextCursor}; items are
   * {runner, title, stage, status, blocker?, ...slot fields}.
   * Dates are ISO timestamps. Status/stage values may use underscores.
   * source.openReport?(output) opens an authenticated report in the host.
   * Reject failed reads. Runner availability is independent of read availability.
   */
  function mount(root, source) {
    const panels = [];
    const status = element("p", "Connecting…", { role: "status" });
    const refresh = element("button", "Refresh", { type: "button" });
    const header = element("header");
    header.append(element("h1", "Phalanx work board"), refresh);
    root.replaceChildren(header, status);

    let selected = null;

    function label(value) {
      return value ? String(value).replaceAll("_", " ") : "Unknown";
    }

    function syncSelection() {
      for (const button of root.querySelectorAll("button[data-item]")) {
        button.setAttribute("aria-pressed", String(button.dataset.item === selected?.id));
      }
    }

    function addOutput(parent, item) {
      if (item.output?.kind === "report" && source.openReport) {
        const report = element("button", "Field report", { type: "button", "data-report": item.id });
        report.addEventListener("click", () => source.openReport(item.output));
        const line = element("p");
        line.append(report);
        parent.append(line);
      } else if (item.output) {
        addLink(parent, item.output.kind === "pr" ? "Verified PR" : "Field report", item.output.url);
      }
    }

    function addWorker(parent, item) {
      addLink(parent, "Worker chat", item.chatUrl);
      if (item.workerRef) parent.append(element("p", "Worker reference: " + item.workerRef, { class: "meta" }));
      if (item.slotNote) parent.append(element("p", item.slotNote, { class: "meta" }));
    }

    const ACTIVITY = {
      executing: "Executing", resource_wait: "Waiting on a resource", external_wait: "Waiting on an external system",
      lease_expired: "Lease expired"
    };

    function slotText(slot) {
      const handoffs = slot.handoffs ? " · " + slot.handoffs + (slot.handoffs === 1 ? " handoff" : " handoffs") : "";
      return "Slot occupied " + since(slot.occupiedSince, slot.occupiedSeconds) +
        " · Current attempt " + duration(slot.attemptSeconds) + handoffs + " · " +
        (ACTIVITY[slot.activity] || label(slot.activity));
    }

    function addTiming(parent, item) {
      const parts = [];
      if (item.pending) parts.push("Pending this admission " + since(item.pending.at, item.pending.seconds));
      if (item.ageSeconds !== undefined) parts.push("Age since intake " + duration(item.ageSeconds));
      if (parts.length) parent.append(element("p", parts.join(" · "), { class: "meta" }));
      if (item.waitReason && item.wait) {
        parent.append(element("p", "Wait elapsed " + since(item.wait.at, item.wait.seconds), { class: "meta" }));
      }
      if (item.slot) parent.append(element("p", slotText(item.slot), { class: "meta" }));
    }

    function addSchedule(parent, item, attrs = {}) {
      if (item.retryAt) parent.append(element("p", "Retry scheduled: " + when(item.retryAt), attrs));
      if (item.recheckAt) parent.append(element("p", "Recheck scheduled: " + when(item.recheckAt), attrs));
    }

    function renderItem(item, kind) {
      const row = element("li");
      const button = element("button", item.title, { type: "button", "data-item": item.id });
      button.addEventListener("click", () => {
        if (selected?.id !== item.id) {
          selected = item;
          detail.heading.textContent = item.title;
          detail.reset();
          syncSelection();
          detail.load();
        }
        detail.heading.focus();
      });
      row.append(button, element("p", item.repo, { class: "meta" }));
      if (kind === "closed") {
        row.append(element("p", label(item.outcome) + " · " + when(item.closedAt)));
        addOutput(row, item);
      } else {
        row.append(element("p", "Stage: " + label(item.stage) + " · Status: " + label(item.status), {
          class: item.status === "waiting_for_input" ? "attention" : "meta"
        }));
        row.append(element("p", "Last advanced: " + when(item.lastAdvancedAt), { class: "meta" }));
        if (item.waitReason) row.append(element("p", item.waitReason));
        addTiming(row, item);
        addSchedule(row, item, { class: "meta" });
        addWorker(row, item);
        if (item.primaryPrUrl) addLink(row, "Primary PR", item.primaryPrUrl);
        else addLink(row, "Draft PR", item.draftUrl);
      }
      return row;
    }

    function phases(entry) {
      return "Queued " + duration(entry.queuedSeconds) + " · Waiting " + duration(entry.waitingSeconds) +
        " · Executing " + duration(entry.executingSeconds);
    }

    function renderLifecycle(lifecycle) {
      const content = document.createDocumentFragment();
      content.append(element("h3", "Lifecycle"));
      const final = lifecycle.closedAt ? " · Final values, closed " + when(lifecycle.closedAt) : "";
      content.append(element("p", "Recorded time: " + phases(lifecycle) + final));
      if (lifecycle.coverage !== "complete") {
        content.append(element("p", "Partial history: " + (lifecycle.note || "some intervals are unavailable."), { class: "meta" }));
      }
      if (lifecycle.stages.length) {
        const table = element("table");
        table.append(element("caption", "Time per stage, including repeat visits"));
        const head = element("tr");
        for (const name of ["Stage", "Visits", "Total", "Queued", "Waiting", "Executing"]) {
          head.append(element("th", name, { scope: "col" }));
        }
        const thead = element("thead");
        thead.append(head);
        table.append(thead);
        const body = element("tbody");
        for (const stage of lifecycle.stages) {
          const row = element("tr");
          row.append(element("th", label(stage.stage) + (stage.partial ? " (partial)" : ""), { scope: "row" }));
          for (const value of [String(stage.visits), duration(stage.seconds), duration(stage.queuedSeconds),
            duration(stage.waitingSeconds), duration(stage.executingSeconds)]) {
            row.append(element("td", value));
          }
          body.append(row);
        }
        table.append(body);
        const scroll = element("div", null, { class: "scroll", role: "region", tabindex: "0", "aria-label": "Time per stage" });
        scroll.append(table);
        content.append(scroll);
      }
      const visits = element("ol", null, { class: "visits", "aria-label": "Stage visits, oldest first" });
      for (const visit of lifecycle.visits) {
        const end = visit.open ? "now (current)" : visit.exitedAt ? when(visit.exitedAt) : "unavailable";
        const entry = element("li", label(visit.stage) + " · " + when(visit.enteredAt) + " → " + end + " · " +
          duration(visit.seconds) + (visit.partial ? " (partial)" : ""));
        const facts = [phases(visit), "Attempts " + visit.attempts];
        if (visit.retries) facts.push("Retries " + visit.retries);
        if (visit.returned) facts.push("Returned to this stage");
        entry.append(element("p", facts.join(" · "), { class: "meta" }));
        if (visit.outcome) {
          entry.append(element("p", "Exit: " + label(visit.outcome) + (visit.reason ? " · " + visit.reason : ""), { class: "meta" }));
        }
        visits.append(entry);
      }
      if (lifecycle.visitsOmitted) {
        content.append(element("p", lifecycle.visitsOmitted + " earlier visits omitted; stage totals include them.", { class: "meta" }));
      }
      content.append(visits);
      return content;
    }

    function renderSlots(data) {
      if (!data.items.length) return element("p", "No runner slots are occupied.");
      const rows = element("ul", null, { class: "records" });
      for (const slot of data.items) {
        const row = element("li");
        row.append(element("p", slot.title), element("p", slot.runner + " · Stage: " + label(slot.stage) +
          " · Status: " + label(slot.status), { class: "meta" }), element("p", slotText(slot)));
        if (slot.blocker) row.append(element("p", slot.blocker, { class: "meta" }));
        rows.append(row);
      }
      return rows;
    }

    function renderDetail(data) {
      const content = document.createDocumentFragment();
      const item = data.item;
      content.append(element("p", item.repo + " · Stage: " + label(item.stage) + " · Status: " + label(item.status)));
      if (item.waitReason) content.append(element("p", item.waitReason));
      addTiming(content, item);
      content.append(element("p", "Attempt: " + (data.attempt || "None recorded")));
      addWorker(content, item);
      if (data.nextAction) content.append(element("p", "Next action: " + data.nextAction));
      addSchedule(content, item);
      if (data.resumeTrigger) content.append(element("p", "Resume trigger: " + data.resumeTrigger));
      if (data.question) {
        content.append(element("h3", "Owner question"), element("p", data.question.prompt, { class: "attention" }));
        if (data.question.options.length) {
          const options = element("ul");
          for (const option of data.question.options) options.append(element("li", option));
          content.append(options);
        }
        if (data.question.proposedDefault) {
          content.append(element("p", "Proposed default: " + data.question.proposedDefault));
        }
        const reply = element("p", "Reply through the owner inbox or API.", { class: "meta" });
        if (item.itemId && item.version) {
          reply.append(" ", element("code", "python -m phalanx reply " + item.itemId + " " + item.id +
            " \"ANSWER\" --version " + item.version + " --key KEY"));
        }
        content.append(reply);
      }
      if (item.primaryPrUrl) addLink(content, "Primary PR", item.primaryPrUrl);
      addOutput(content, item);
      if (data.dependencies?.length) {
        const list = element("ul");
        for (const dependency of data.dependencies) {
          const blocker = dependency.reason || label(dependency.status);
          list.append(element("li", dependency.title + " · " + label(dependency.milestone) + " · " + blocker));
        }
        content.append(element("h3", "Prerequisites"), list);
      }
      if (data.lifecycle) content.append(renderLifecycle(data.lifecycle));
      content.append(element("h3", "Transition history"));
      if (data.events.length) {
        const timeline = element("ol");
        for (const event of data.events) {
          const transition = event.fromStage ? label(event.fromStage) + " → " : "";
          const entry = element("li", when(event.at) + " · " + transition + label(event.stage) + " · " + label(event.status));
          if (event.reason) entry.append(element("p", event.reason));
          timeline.append(entry);
        }
        content.append(timeline);
      } else {
        content.append(element("p", "No transitions on this page."));
      }
      return content;
    }

    function updateStatus() {
      const visible = panels.filter(panel => panel.enabled());
      const failed = visible.some(panel => panel.failed);
      const dated = visible.map(panel => panel.updated).filter(Boolean);
      const stale = dated.some(time => Date.now() - time >= STALE_MS);
      let message = "Connecting…";
      if (visible.every(panel => panel.failed)) message = "Disconnected. Refresh to retry.";
      else if (failed) message = "Some data is unavailable. Refresh to retry.";
      else if (dated.length) message = "Connected.";
      if (stale || (failed && dated.length)) message += " Previously loaded data may be stale.";
      if (dated.length) message += " Oldest displayed update: " + when(Math.min(...dated)) + ".";
      if (status.textContent !== message) status.textContent = message;
    }

    function createPanel(title, pageLabel, enabled, read, render, empty) {
      const section = element("section", null, { "aria-label": title });
      const heading = element("h2", title, { tabindex: "-1" });
      const notice = element("p", null, { role: "status" });
      const body = element("div");
      const nav = element("nav", null, { "aria-label": pageLabel + " pagination" });
      const previous = element("button", "Previous", { type: "button", "aria-label": "Previous " + pageLabel + " page" });
      const next = element("button", "Next", { type: "button", "aria-label": "Next " + pageLabel + " page" });
      const page = element("span", "Page 1");
      nav.append(previous, page, next);
      section.append(heading, notice, body, nav);
      root.append(section);

      let cursors = [null];
      let nextCursor = null;
      let request;
      let disposed = false;
      const panel = {
        enabled, heading, updated: 0, failed: false,
        reset() {
          request?.abort();
          request = undefined;
          cursors = [null];
          clearPage();
        },
        dispose() {
          disposed = true;
          request?.abort();
        },
        async load() {
          if (disposed || !enabled() || request) return;
          const current = new AbortController();
          request = current;
          section.setAttribute("aria-busy", "true");
          if (!panel.updated) notice.textContent = "Loading…";
          let timeout;
          try {
            const deadline = new Promise((_, reject) => {
              timeout = setTimeout(() => reject(new Error("Queue read timed out")), READ_TIMEOUT_MS);
            });
            const data = await Promise.race([read(cursors.at(-1), current.signal), deadline]);
            if (current.signal.aborted) return;
            const content = render(data);
            const focused = body.contains(document.activeElement) ? document.activeElement : null;
            const focusedItem = focused?.dataset.item;
            const focusedReport = focused?.dataset.report;
            const focusedHref = focused?.getAttribute("href");
            body.replaceChildren(content);
            syncSelection();
            if (focused) {
              const replacement = [...body.querySelectorAll("button[data-item], button[data-report], a")].find(node =>
                (focusedItem && node.dataset.item === focusedItem) ||
                (focusedReport && node.dataset.report === focusedReport) ||
                (focusedHref && node.getAttribute("href") === focusedHref)
              );
              (replacement || heading).focus({ preventScroll: true });
            }
            nextCursor = data.nextCursor ?? null;
            notice.textContent = "";
            panel.updated = Date.now();
            panel.failed = false;
          } catch {
            if (current.signal.aborted) return;
            notice.textContent = panel.updated
              ? "Update failed. Showing the previous page contents."
              : "Could not load this page. Use Refresh to retry.";
            panel.failed = true;
          } finally {
            clearTimeout(timeout);
            current.abort();
            if (request === current) {
              request = undefined;
              section.removeAttribute("aria-busy");
              updateControls();
              updateStatus();
            }
          }
        }
      };

      function updateControls() {
        previous.disabled = cursors.length === 1;
        next.disabled = nextCursor === null;
        page.textContent = "Page " + cursors.length;
        nav.hidden = !enabled() || (previous.disabled && next.disabled);
      }

      function clearPage() {
        panel.updated = 0;
        panel.failed = false;
        nextCursor = null;
        section.removeAttribute("aria-busy");
        body.replaceChildren();
        notice.textContent = enabled() ? "" : empty;
        updateControls();
      }

      function changePage(advance) {
        request?.abort();
        request = undefined;
        if (advance) cursors.push(nextCursor);
        else cursors.pop();
        heading.focus();
        clearPage();
        updateStatus();
        panel.load();
      }

      previous.addEventListener("click", () => changePage(false));
      next.addEventListener("click", () => changePage(true));
      clearPage();
      panels.push(panel);
      return panel;
    }

    function list(title, kind, empty) {
      return createPanel(title, kind === "open" ? "open work" : "recent outcomes", () => true,
        (cursor, signal) => source.list({ kind, cursor, limit: PAGE_SIZE, signal }),
        data => {
          if (!data.items.length) return element("p", empty);
          const rows = element("ul", null, { class: "records" });
          for (const item of data.items) rows.append(renderItem(item, kind));
          return rows;
        }, empty);
    }

    list("Work in progress", "open", "No open work on this page. The line holds.");
    createPanel("Runner slots", "runner slots", () => Boolean(source.slots),
      (cursor, signal) => source.slots({ cursor, limit: PAGE_SIZE, signal }),
      renderSlots, "Runner slot occupancy is unavailable from this source.");
    const detail = createPanel("Selected item", "history", () => Boolean(selected),
      (cursor, signal) => source.detail({ id: selected.id, cursor, limit: PAGE_SIZE, signal }),
      renderDetail, "Select an item from the muster to inspect its recorded history.");
    list("Recently closed", "closed", "No closed work on this page.");

    function refreshAll() {
      updateStatus();
      for (const panel of panels) panel.load();
    }

    refresh.addEventListener("click", refreshAll);
    const interval = setInterval(refreshAll, REFRESH_MS);
    document.addEventListener("visibilitychange", refreshAll);
    refreshAll();

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshAll);
      for (const panel of panels) panel.dispose();
      root.replaceChildren();
    };
  }

  window.PhalanxOperatorBoard = { mount };
})();
