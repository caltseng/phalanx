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
   * retryAt? (epoch ms), chatUrl?, draftUrl?, primaryPrUrl?, closedAt?, outcome?, output?: {kind: "pr"|"report", url}.
   * Use status "waiting_for_input" for items requiring operator attention.
   * outcome is a display label, including "Checked in" vs "Ready for check-in".
   * source.detail({id, cursor, limit, signal}) resolves to
   * {item, attempt?, nextAction?, resumeTrigger?, dependencies?, events, nextCursor}.
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
        if (item.output?.kind === "report" && source.openReport) {
          const report = element("button", "Field report", { type: "button", "data-report": item.id });
          report.addEventListener("click", () => source.openReport(item.output));
          const line = element("p");
          line.append(report);
          row.append(line);
        } else if (item.output) {
          addLink(row, item.output.kind === "pr" ? "Verified PR" : "Field report", item.output.url);
        }
      } else {
        row.append(element("p", "Stage: " + label(item.stage) + " · Status: " + label(item.status), {
          class: item.status === "waiting_for_input" ? "attention" : "meta"
        }));
        row.append(element("p", "Last advanced: " + when(item.lastAdvancedAt), { class: "meta" }));
        if (item.waitReason) row.append(element("p", item.waitReason));
        if (item.retryAt) row.append(element("p", "Retry scheduled: " + when(item.retryAt), { class: "meta" }));
        addLink(row, "Worker chat", item.chatUrl);
        if (item.primaryPrUrl) addLink(row, "Primary PR", item.primaryPrUrl);
        else addLink(row, "Draft PR", item.draftUrl);
      }
      return row;
    }

    function renderDetail(data) {
      const content = document.createDocumentFragment();
      const item = data.item;
      content.append(element("p", item.repo + " · Stage: " + label(item.stage) + " · Status: " + label(item.status)));
      if (item.waitReason) content.append(element("p", item.waitReason));
      content.append(element("p", "Attempt: " + (data.attempt || "None recorded")));
      if (data.nextAction) content.append(element("p", "Next action: " + data.nextAction));
      if (item.retryAt) content.append(element("p", "Retry scheduled: " + when(item.retryAt)));
      if (data.resumeTrigger) content.append(element("p", "Resume trigger: " + data.resumeTrigger));
      if (data.dependencies?.length) {
        const list = element("ul");
        for (const dependency of data.dependencies) {
          list.append(element("li", dependency.title + " · " + dependency.milestone + " · " + label(dependency.status)));
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

    list("Work in progress", "open", "No open work on this page.");
    const detail = createPanel("Selected item", "history", () => Boolean(selected),
      (cursor, signal) => source.detail({ id: selected.id, cursor, limit: PAGE_SIZE, signal }),
      renderDetail, "Select an item to inspect its recorded history.");
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
