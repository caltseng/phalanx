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

  function since(value) {
    const minutes = Math.floor((Date.now() - new Date(value).getTime()) / 60000);
    if (!value || !Number.isFinite(minutes)) return "Unknown";
    if (minutes < 1) return "<1m";
    if (minutes < 60) return minutes + "m";
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h " + (minutes % 60) + "m";
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h";
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
   * Items contain string id, title (one short line), repo, stage, status, lastAdvancedAt, waitReason?,
   * retryAt? and recheckAt? (epoch ms), workerRef? (opaque bridge reference), chatUrl?, draftUrl?,
   * primaryPrUrl?, closedAt?, outcome?, output?: {kind: "pr"|"report", url}, and for replies itemId? and version?.
   * writer? is the registered worker session: {state, runtime, captainId, sessionUrl?, lastProgressAt?, progressNote?}.
   * Optional source.flags({signal}) resolves to {flags: [{kind, itemId?, captainId?, reason, recovery,
   * firstSeen, notifyOwner}]}, and source.captains({signal}) to {captains: [{id, sessionRef, state, live,
   * stale, heartbeatAt, wakeCount, focus, handoffNote?}]}. Their panels appear only when supplied.
   * Use status "waiting_for_input" for items requiring operator attention.
   * outcome is a display label, including "Checked in" vs "Ready for check-in".
   * source.detail({id, cursor, limit, signal}) resolves to
   * {item, goal?, attempt?, nextAction?, resumeTrigger?, question?, dependencies?, events, nextCursor}.
   * goal is the full work request text.
   * question is {prompt, options, proposedDefault?} while the item waits for the owner.
   * attempt is a display reference. Dependencies are {title, milestone, status}.
   * Events are newest first and contain {at, fromStage?, stage, status, reason?}.
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
      if (item.writer) {
        const writer = item.writer;
        parent.append(element("p", "Writer: " + label(writer.state) + " · " + writer.runtime +
          " · Captain " + String(writer.captainId).slice(0, 8), { class: "meta" }));
        addLink(parent, "Writer session", writer.sessionUrl);
        if (writer.lastProgressAt) {
          parent.append(element("p", "Last progress: " + when(writer.lastProgressAt) +
            (writer.progressNote ? " · " + writer.progressNote : ""), { class: "meta" }));
        }
      }
      addLink(parent, "Worker chat", item.chatUrl);
      if (item.workerRef) parent.append(element("p", "Worker reference: " + item.workerRef, { class: "meta" }));
      if (item.slotNote) parent.append(element("p", item.slotNote, { class: "meta" }));
    }

    function addSchedule(parent, item, attrs = {}) {
      if (item.retryAt) parent.append(element("p", "Retry scheduled: " + when(item.retryAt), attrs));
      if (item.recheckAt) parent.append(element("p", "Recheck scheduled: " + when(item.recheckAt), attrs));
    }

    function renderItem(item, kind) {
      const row = element("li", null, kind === "open" ? { class: "line" } : {});
      const button = element("button", item.title, { type: "button", "data-item": item.id, title: item.title });
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
      if (kind === "closed") {
        row.append(button, element("p", item.repo, { class: "meta" }));
        row.append(element("p", label(item.outcome) + " · " + when(item.closedAt)));
        addOutput(row, item);
      } else {
        // Active means a worker or runner stage is running without a recorded wait or blocker.
        const active = item.status === "running" && !item.waitReason;
        row.append(active
          ? element("span", null, { class: "activity", role: "img", "aria-label": "Being worked on", title: "Being worked on" })
          : element("span"));
        row.append(button);
        const stage = element("span", null, { class: "meta", title: "Stage: " + label(item.stage) + " · Status: " + label(item.status) });
        stage.append(element("span", "Stage: ", { class: "visually-hidden" }), label(item.stage),
          element("span", ", status: " + label(item.status), { class: "visually-hidden" }));
        const age = element("span", null, { class: "meta", title: "Last advanced: " + when(item.lastAdvancedAt) });
        age.append(element("span", "In stage for ", { class: "visually-hidden" }), since(item.lastAdvancedAt));
        const reason = item.waitReason || label(item.status);
        row.append(stage, age, element("span", reason, {
          title: reason, class: item.status === "waiting_for_input" ? "attention" : "meta"
        }));
      }
      return row;
    }

    function renderDetail(data) {
      const content = document.createDocumentFragment();
      const item = data.item;
      content.append(element("p", item.repo + " · Stage: " + label(item.stage) + " · Status: " + label(item.status)));
      if (item.waitReason) content.append(element("p", item.waitReason));
      content.append(element("p", "Last advanced: " + when(item.lastAdvancedAt), { class: "meta" }));
      if (data.goal) content.append(element("h3", "Request"), element("p", data.goal, { class: "goal" }));
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
      else addLink(content, "Draft PR", item.draftUrl);
      addOutput(content, item);
      if (data.dependencies?.length) {
        const list = element("ul");
        for (const dependency of data.dependencies) {
          const blocker = dependency.reason || label(dependency.status);
          list.append(element("li", dependency.title + " · " + label(dependency.milestone) + " · " + blocker));
        }
        content.append(element("h3", "Prerequisites"), list);
      }
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

    if (source.flags) {
      createPanel("Flags", "flags", () => true, (cursor, signal) => source.flags({ signal }), data => {
        if (!data.flags.length) return element("p", "No flags. Nothing has drifted.");
        const rows = element("ul", null, { class: "records" });
        for (const flag of data.flags) {
          const row = element("li");
          const subject = [flag.itemId && "item " + flag.itemId.slice(0, 8),
            flag.captainId && "Captain " + flag.captainId.slice(0, 8)].filter(Boolean).join(" · ");
          row.append(element("p", label(flag.kind) + (subject ? " · " + subject : ""),
            { class: flag.notifyOwner ? "attention" : "meta" }));
          row.append(element("p", flag.reason), element("p", "Recovery: " + flag.recovery, { class: "meta" }));
          row.append(element("p", "Since " + when(flag.firstSeen), { class: "meta" }));
          rows.append(row);
        }
        return rows;
      }, "");
    }
    list("Work in progress", "open", "No open work on this page. The line holds.");
    const detail = createPanel("Selected item", "history", () => Boolean(selected),
      (cursor, signal) => source.detail({ id: selected.id, cursor, limit: PAGE_SIZE, signal }),
      renderDetail, "Select an item from the muster to inspect its recorded history.");
    list("Recently closed", "closed", "No closed work on this page.");
    if (source.captains) {
      createPanel("Captains", "captains", () => true, (cursor, signal) => source.captains({ signal }), data => {
        if (!data.captains.length) return element("p", "No Captain has registered.");
        const rows = element("ul", null, { class: "records" });
        for (const captain of data.captains) {
          const row = element("li");
          const health = captain.live ? "live" : captain.stale ? "stale" : label(captain.state);
          row.append(element("p", "Captain " + captain.id.slice(0, 8) + " · " + health + " · " + captain.sessionRef,
            { class: captain.stale ? "attention" : "meta" }));
          row.append(element("p", "Heartbeat: " + when(captain.heartbeatAt) + " · Wakes: " + captain.wakeCount,
            { class: "meta" }));
          if (captain.focus.length) row.append(element("p", "Focus: " + captain.focus.join(", "), { class: "meta" }));
          if (captain.handoffNote) row.append(element("p", "Handoff: " + captain.handoffNote));
          rows.append(row);
        }
        return rows;
      }, "");
    }

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
