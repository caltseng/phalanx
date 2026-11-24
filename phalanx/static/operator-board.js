(() => {
  "use strict";

  const OPEN_PAGE = 100;
  const CLOSED_PAGE = 25;
  const DETAIL_PAGE = 50;
  const REFRESH_MS = 15000;
  const TICK_MS = 5000;
  const STALE_MS = 45000;
  const READ_TIMEOUT_MS = 12000;
  const CHANGED_MS = 2400;
  const THEME_KEY = "phalanx-board-theme";
  const STAGES = ["development", "self_review", "publication", "delivery",
    "report_drafting", "report_review", "report_storage"];
  const GLYPH = { attn: "!", ok: "✓", fail: "✕", muted: "–" };
  const DISPLAY_LABEL = { checked_in: "Merged", queue_backlogged: "Queue backlogged",
    captain_idle_with_work: "Dispatch overdue" };
  const absolute = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child != null && child !== false) node.append(child);
    }
    return node;
  }

  function label(value) {
    return DISPLAY_LABEL[value] || (value ? String(value).replaceAll("_", " ") : "unknown");
  }

  function short(value) {
    return value ? String(value).slice(0, 8) : "";
  }

  function firstLine(value) {
    return value ? String(value).split("\n").find(line => line.trim())?.trim() || "" : "";
  }

  function epoch(value) {
    if (value == null || value === "") return null;
    const time = typeof value === "number" ? value : Date.parse(value);
    return Number.isFinite(time) ? time : null;
  }

  function span(delta) {
    const seconds = Math.max(0, Math.round(delta / 1000));
    if (seconds < 60) return seconds + "s";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m";
    const hours = Math.floor(minutes / 60);
    if (hours < 10 && minutes % 60) return hours + "h " + (minutes % 60) + "m";
    if (hours < 48) return hours + "h";
    return Math.floor(hours / 24) + "d";
  }

  function stamp(value) {
    const time = epoch(value);
    if (time == null) return "Unknown";
    return absolute.format(time) + " · " + new Date(time).toISOString().replace(".000", "");
  }

  // A relative age with the local and UTC time on hover.
  function ago(value, now, attrs = {}) {
    const time = epoch(value);
    if (time == null) return h("span", { class: "faint", text: "—", ...attrs });
    const text = span(now - time) + " ago";
    const { "aria-label": context, ...rest } = attrs;
    return h("time", { datetime: new Date(time).toISOString(), title: stamp(time), ...rest,
      "aria-label": [context, text, stamp(time)].filter(Boolean).join(" · "), text });
  }

  function safeUrl(value) {
    if (!value) return null;
    try {
      const url = new URL(value, document.baseURI);
      if (["http:", "https:"].includes(url.protocol) && !url.username && !url.password) return url.href;
    } catch {
      // Missing or invalid link.
    }
    return null;
  }

  function prNumber(url) {
    const match = /\/pull\/(\d+)/.exec(url || "");
    return match ? "#" + match[1] : "PR";
  }

  function outcomeKey(item) {
    if (item.outcome === "ready_for_check_in" && item.prState === "merged") return "checked_in";
    return String(item.outcome || "").toLowerCase().replaceAll(" ", "_");
  }

  function shortfall(item) {
    const outcome = outcomeKey(item);
    if (!item.target || !outcome || outcome === item.target ||
        (item.target === "report" && outcome === "report_stored")) return "";
    const rank = { draft_published: 1, ready_for_check_in: 2, checked_in: 3 };
    if (rank[outcome] && rank[item.target] && rank[outcome] >= rank[item.target]) return "";
    return "wanted: " + label(item.target).toLowerCase();
  }

  function mark(kind) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({
      viewBox: "0 0 16 16", width: "1em", height: "1em", "aria-hidden": "true",
      focusable: "false", class: "status-mark mark-" + kind
    })) svg.setAttribute(key, value);
    const path = (d, attrs = {}) => {
      const node = document.createElementNS(svg.namespaceURI, "path");
      for (const [key, value] of Object.entries({ d, ...attrs })) node.setAttribute(key, value);
      svg.append(node);
    };
    if (kind === "run") {
      svg.setAttribute("viewBox", "0 0 12 16");
      path("M2 4.5C1.9 5.8 .2 6.7 .6 7.9L2 9.3 3.4 7.9C3.8 6.7 2.1 5.8 2 4.5ZM1.4 8.8H2.6V14.5H1.4Z",
        { class: "spear spear-left", fill: "currentColor" });
      path("M10 4.5C9.9 5.8 8.2 6.7 8.6 7.9L10 9.3 11.4 7.9C11.8 6.7 10.1 5.8 10 4.5ZM9.4 8.8H10.6V14.5H9.4Z",
        { class: "spear spear-right", fill: "currentColor" });
      path("M6 2.5C5.8 4.2 3.8 5.5 4.3 7L6 8.6 7.7 7C8.2 5.5 6.2 4.2 6 2.5ZM5.3 8H6.7V15H5.3Z",
        { class: "spear spear-center", fill: "currentColor" });
    } else if (kind === "captain") {
      path("M1 6C1 2.5 4 .5 8 .5S15 2.5 15 6H12.5C12 4 10.5 3 8 3S4 4 3.5 6Z",
        { fill: "currentColor" });
      path("M3 8A5 5 0 0 1 13 8V9H10V11L12 12 11 15 7 13V9H5V14H2L3 10Z",
        { fill: "currentColor" });
    } else if (kind === "flag" || kind === "notify") {
      path("M3 15V1", { stroke: "currentColor", "stroke-width": "1.5" });
      path("M4 2Q7 0 10 2T15 2L12 6 15 9Q12 11 9 9T4 9Z",
        { class: "pennant", fill: kind === "notify" ? "var(--flag)" : "none",
          stroke: "var(--flag)", "stroke-width": "1.5" });
    } else {
      path("M8 1 14 3V7Q14 12 8 15 2 12 2 7V3Z",
        { fill: "none", stroke: "currentColor", "stroke-width": "1.5" });
      if (kind === "wait") path("M5 6H11M5 9H11", { stroke: "currentColor", "stroke-width": "1.5" });
    }
    return svg;
  }

  function tone(item) {
    if (item.outcome) {
      const outcome = outcomeKey(item);
      return outcome === "failed" ? "fail" : outcome === "cancelled" ? "muted" : "ok";
    }
    if (item.status === "waiting_for_input") return "attn";
    if (item.status === "failed") return "fail";
    if (item.status === "running") return "run";
    if (item.status === "waiting_for_external" || item.waitReason) return "wait";
    return "idle";
  }

  function backward(from, to) {
    return Boolean(from) && (STAGES.indexOf(to) < STAGES.indexOf(from));
  }

  // Preserved worker references on historical attempts.
  function historicalWorkerFacts(item) {
    const legacy = item.historicalWorker || {};
    const chat = safeUrl(legacy.chatUrl);
    return [
      legacy.workerRef && ["Worker", legacy.workerRef],
      chat && ["Worker chat", h("a", { href: chat, text: "open" })]
    ].filter(Boolean);
  }

  function queryForm(action, props = {}, ...children) {
    const form = h("form", { action, method: "get", ...props }, ...children);
    form.addEventListener("submit", event => {
      event.preventDefault();
      const url = new URL(action, location.origin);
      for (const [name, value] of new FormData(form)) {
        if (value.trim()) url.searchParams.set(name, value.trim());
      }
      location.assign(url);
    });
    return form;
  }

  function toolbarIcon(kind) {
    const paths = {
      search: "M10.5 10.5 15 15M12 7A5 5 0 1 1 2 7a5 5 0 0 1 10 0",
      filters: "M2 3h12L9 8v5l-2 1V8Z",
      theme: "M8 1v2m0 10v2M1 8h2m10 0h2M3 3l1.5 1.5m7 7L13 13M3 13l1.5-1.5m7-7L13 3M11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
      moon: "M13.5 10A6 6 0 0 1 6 2.5a6 6 0 1 0 7.5 7.5Z",
      auto: "M8 2a6 6 0 1 0 0 12 6 6 0 0 0 0-12Zm0 0v12M8 5h4M8 8h6M8 11h4",
      settings: "M6 2h4l.5 2 2 .5 1.5 3.5-1.5 1.5.5 2-3 2-2-1-2 1-3-2 .5-2L2 8l1.5-3.5 2-.5ZM10 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
      refresh: "M13 6a5.5 5.5 0 1 0 .4 4M13 2v4H9",
      create: "M8 2v12M2 8h12"
    };
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({
      viewBox: "0 0 16 16", width: "18", height: "18", "aria-hidden": "true", focusable: "false",
      fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round",
      "stroke-linejoin": "round"
    })) svg.setAttribute(key, value);
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", paths[kind]);
    svg.append(path);
    return svg;
  }

  /**
   * mount(root, source) returns an unmount function. Call once per root with class "phalanx-board".
   * The host supplies the authenticated adapter and translates API fields here.
   * source.list({kind, cursor, limit, signal}) resolves to {items, nextCursor}. kind is "open" or "closed".
   * Cursors are opaque strings or null. Closed items come newest first; open work puts input waits first.
   * Items contain string id, itemId?, version?, title (one line), goal?, target?, publicationMode?, repo, stage, status,
   * createdAt?, lastAdvancedAt, lastProgressAt?, waitSince?, waitReason?, resumeTrigger?, retryAt? and
   * recheckAt? (epoch ms), primaryPrUrl?, closedAt?, outcome?, output?: {kind: "pr"|"report", url?},
   * delivery?: {mergeCommit, method?, mergedAt?} once GitHub shows the PR merged,
   * writer?: {state, runtime, captainId, sessionId?, sessionUrl?, lastProgressAt?, progressNote?} and
   * historicalWorker?: {workerRef?, chatUrl?} for preserved attempt evidence.
   * Optional source.flags({signal}) resolves to {flags: [{kind, itemId?, executionId?, captainId?, reason,
   * recovery, firstSeen, notifyOwner}]}, and source.captains({signal}) to {captains: [{id, sessionRef,
   * state, live, stale, heartbeatAt, wakeCount, compactionCount?, charterRevision?, focus, handoffNote?}]}.
   * source.detail({id, cursor, limit, signal}) resolves to
   * {item, attempt?, nextAction?, question?, dependencies?, events, nextCursor}.
   * question is {prompt, options, proposedDefault?}. Dependencies are {title, milestone, status, reason?}.
   * Events are newest first: {at, event?, fromStage?, stage, status, reason?}. Dates are ISO strings.
   * source.openReport?(output) opens a stored report in the host. Reject failed reads.
   * source.openNewItem?(trigger, created) opens host intake; call created(item) on acknowledgment.
   * source.concurrencyControl? is the host's authoritative settings disclosure; the host restores it on cleanup.
   * options selects home/queue/history/detail, URL filters/cursors and an itemId or executionId.
   */
  function mount(root, source, options = {}) {
    const page = options.page || "home";
    const routed = page === "detail";
    const slot = () => ({ rows: [], cursors: [null], next: null, updated: 0, failed: false, request: null });
    const state = {
      open: slot(), closed: slot(), flags: slot(), captains: slot(),
      route: { ...slot(), data: null },
      expanded: null, expandedAt: 0, endedOpen: false, details: new Map(), flagGroups: new Set(), seen: new Map(), changed: new Map()
    };
    const filterStatus = options.filters?.status;
    const closedStatus = ["completed", "failed", "cancelled"].includes(filterStatus);
    const enabled = {
      open: !routed && (page === "queue" || (page === "home" &&
        !options.filters?.outcome && !options.filters?.closed_after && !closedStatus)),
      closed: !routed && (page === "history" || (page === "home" &&
        (options.filters?.outcome || options.filters?.closed_after || !filterStatus || closedStatus))),
      route: routed, flags: Boolean(source.flags), captains: Boolean(source.captains) };
    for (const kind of ["open", "closed"]) {
      if (options.cursors?.[kind]) state[kind].cursors = [options.cursors[kind]];
    }
    let disposed = false;
    let clearConcurrency;

    const announce = h("p", { class: "sr-only", role: "status" });
    const health = h("div", { class: "health" });
    const counts = h("ul", { class: "counts", "aria-label": "Summary" });
    function describeTool(node, name) {
      node.setAttribute("aria-label", name);
      node.title = name;
      node.dataset.tooltip = name;
      const show = () => node.classList.remove("tooltip-dismissed");
      const dismiss = event => {
        if (event.key === "Escape") node.classList.add("tooltip-dismissed");
      };
      node.addEventListener("focus", show);
      node.addEventListener("pointerenter", show);
      node.addEventListener("keydown", dismiss);
      return () => {
        node.removeEventListener("focus", show);
        node.removeEventListener("pointerenter", show);
        node.removeEventListener("keydown", dismiss);
      };
    }
    const toolButton = (kind, name) => {
      const button = h("button", { type: "button", class: "tool-button", "data-action": kind }, toolbarIcon(kind));
      describeTool(button, name);
      return button;
    };
    const theme = toolButton("theme", "Change theme");
    const refresh = toolButton("refresh", "Refresh");
    const create = source.openNewItem && toolButton("create", "New work item");
    if (create) {
      create.classList.add("new-item");
      create.append(h("span", { text: "New" }));
    }
    if (create) create.addEventListener("click", () => source.openNewItem(create, created));
    const body = h("div", { class: "sections" });
    const navigation = h("nav", { class: "board-navigation", "aria-label": "Scribe pages" },
      [["/operator/", "Home"], ["/operator/queue", "Queue"], ["/operator/history", "History"]].map(([href, text]) =>
        h("a", { href, text, "aria-current": text.toLowerCase() === page ? "page" : null })));
    const search = queryForm("/operator/", { role: "search", class: "board-search" },
      h("label", {}, "Search all work ", h("input", { name: "q", type: "search", maxlength: "200",
        value: options.filters?.q || "", placeholder: "Item, execution, PR or title" })),
      h("button", { type: "submit", text: "Search" }));
    const actions = h("div", { class: "actions", role: "group", "aria-label": "Board controls" }, create);
    const header = h("header", { class: "bar" },
      h("div", { class: "brand" }, h("span", { class: "mark", "aria-hidden": "true" }),
        h("h1", { text: "Phalanx", tabindex: "-1" }), h("span", { class: "faint sub", text: "Scribe" })),
      health, navigation, actions, counts);
    const disclosures = [];
    function disclosure(kind, name, content, activeCount) {
      const description = name + (activeCount ? " (" + activeCount + " applied)" : "");
      const trigger = toolButton(kind, description);
      const panel = h("section", { id: "board-" + kind, class: "toolbar-panel", hidden: true,
        "aria-label": name }, content);
      trigger.setAttribute("aria-controls", panel.id);
      trigger.setAttribute("aria-expanded", "false");
      trigger.classList.toggle("is-active", Boolean(activeCount));
      if (activeCount) trigger.append(h("span", { class: "tool-badge", "aria-hidden": "true", text: activeCount }));
      const close = () => {
        panel.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        for (const dropdown of panel.querySelectorAll("details")) dropdown.open = false;
      };
      trigger.addEventListener("click", () => {
        const opening = panel.hidden;
        for (const other of disclosures) other.close();
        if (opening) {
          panel.hidden = false;
          trigger.setAttribute("aria-expanded", "true");
          content.querySelector("input:not([type=hidden]), summary")?.focus();
        }
      });
      const onEscape = event => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        event.stopPropagation();
        close();
        trigger.focus();
        trigger.classList.add("tooltip-dismissed");
      };
      panel.addEventListener("keydown", onEscape);
      trigger.addEventListener("keydown", onEscape);
      disclosures.push({ close });
      actions.append(trigger);
      header.append(panel);
    }
    disclosure("search", "Search all work", search, options.filters?.q ? 1 : 0);
    root.replaceChildren(header, announce);
    if (!routed) {
      const filters = queryForm(page === "home" ? "/operator/" : "/operator/" + page,
        { class: "board-filters", "aria-label": "Work filters" });
      const fields = ["repository", "label", "status", "stage"];
      if (page !== "queue") fields.push("outcome", "closed_after");
      for (const name of fields) {
        const input = h("input", {
          name, value: options.filters?.[name] || "", maxlength: name === "closed_after" ? "40" : "100",
          placeholder: name === "closed_after" ? "ISO date/time" : "Any"
        });
        const selected = h("span", { class: "filter-value" });
        const summary = h("summary", {}, label(name) + ": ", selected);
        const dropdown = h("details", { class: "filter-dropdown" }, summary,
          h("div", { class: "filter-panel" }, h("label", {}, label(name) + " ", input)));
        const update = () => {
          selected.textContent = input.value.trim() || "Any";
          summary.title = label(name) + ": " + selected.textContent;
          dropdown.classList.toggle("is-selected", Boolean(input.value.trim()));
        };
        update();
        input.addEventListener("input", update);
        summary.addEventListener("click", () => {
          for (const other of filters.querySelectorAll("details")) {
            if (other !== dropdown) other.open = false;
          }
        });
        dropdown.addEventListener("keydown", event => {
          if (event.key !== "Escape" || !dropdown.open) return;
          event.preventDefault();
          event.stopPropagation();
          dropdown.open = false;
          summary.focus();
        });
        filters.append(dropdown);
      }
      if (options.filters?.q) filters.append(h("input", { type: "hidden", name: "q", value: options.filters.q }));
      filters.append(h("button", { type: "submit", text: "Apply filters" }),
        h("a", { href: page === "home" ? "/operator/" : "/operator/" + page, text: "Clear" }));
      disclosure("filters", "Work filters", filters, fields.filter(name => options.filters?.[name]).length);
    }
    const concurrency = source.concurrencyControl;
    if (concurrency) {
      concurrency.classList.add("toolbar-settings");
      const summary = concurrency.querySelector("summary");
      summary.classList.add("tool-button");
      const clearDescription = describeTool(summary, "Writer concurrency settings");
      summary.replaceChildren(toolbarIcon("settings"));
      const settingsPanel = h("div", { class: "toolbar-settings-panel" });
      for (const child of [...concurrency.childNodes]) {
        if (child !== summary) settingsPanel.append(child);
      }
      concurrency.append(settingsPanel);
      const close = () => { concurrency.open = false; };
      const onClick = event => {
        event.preventDefault();
        const opening = !concurrency.open;
        for (const other of disclosures) other.close();
        concurrency.open = opening;
      };
      const onToggle = () => {
        if (concurrency.open) concurrency.querySelector("input:not(:disabled)")?.focus();
        else if (concurrency.contains(document.activeElement)) summary.focus();
      };
      const onEscape = event => {
        if (event.key !== "Escape" || !concurrency.open) return;
        event.preventDefault();
        event.stopPropagation();
        close();
        summary.focus();
        summary.classList.add("tooltip-dismissed");
      };
      summary.addEventListener("click", onClick);
      concurrency.addEventListener("toggle", onToggle);
      concurrency.addEventListener("keydown", onEscape);
      clearConcurrency = () => {
        clearDescription();
        summary.removeEventListener("click", onClick);
        concurrency.removeEventListener("toggle", onToggle);
        concurrency.removeEventListener("keydown", onEscape);
        settingsPanel.replaceWith(...settingsPanel.childNodes);
      };
      disclosures.push({ close });
      actions.append(concurrency);
    }
    actions.append(theme, refresh);
    const applied = Object.entries(options.filters || {}).filter(([, value]) => value);
    if (applied.length) {
      header.append(h("div", { class: "toolbar-applied", "aria-label": "Applied search and filters" },
        applied.map(([name, value]) => h("span", { title: label(name) + ": " + value,
          text: (name === "q" ? "Search" : label(name)) + ": " + value }))));
    }
    root.append(body);

    function applyTheme() {
      let choice = "auto";
      try { choice = localStorage.getItem(THEME_KEY) || "auto"; } catch { /* Storage may be unavailable. */ }
      if (choice === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = choice;
      const description = "Theme: " + choice + ". Change to " +
        ({ auto: "dark", dark: "light", light: "auto" }[choice] || "dark");
      theme.setAttribute("aria-label", description);
      theme.title = description;
      theme.dataset.tooltip = description;
      theme.replaceChildren(toolbarIcon(choice === "dark" ? "moon" : choice === "light" ? "theme" : "auto"));
    }

    theme.addEventListener("click", () => {
      let choice = "auto";
      try { choice = localStorage.getItem(THEME_KEY) || "auto"; } catch { /* Ignore. */ }
      const next = { auto: "dark", dark: "light", light: "auto" }[choice] || "auto";
      try { localStorage.setItem(THEME_KEY, next); } catch { /* Ignore. */ }
      if (!document.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) {
        applyTheme();
        return;
      }
      // The stylesheet spills the new theme outward from the toggle.
      const box = theme.getBoundingClientRect();
      const x = box.left + box.width / 2;
      const y = box.top + box.height / 2;
      const style = document.documentElement.style;
      style.setProperty("--ink-x", x + "px");
      style.setProperty("--ink-y", y + "px");
      style.setProperty("--ink-r", Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y)) + "px");
      document.startViewTransition(applyTheme);
    });

    // Looping animations start at minus the page clock so rebuilt rows stay in step.
    function syncClock(node) {
      node.style.setProperty("--clock", -Math.round(performance.now()) + "ms");
    }

    async function guarded(target, read) {
      if (disposed || target.request) return undefined;
      const current = new AbortController();
      target.request = current;
      let timer;
      try {
        const deadline = new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("Read timed out")), READ_TIMEOUT_MS);
        });
        const data = await Promise.race([read(current.signal), deadline]);
        if (current.signal.aborted) return undefined;
        target.failed = false;
        target.updated = Date.now();
        return data;
      } catch {
        if (!current.signal.aborted) target.failed = true;
        return undefined;
      } finally {
        clearTimeout(timer);
        current.abort();
        if (target.request === current) target.request = null;
      }
    }

    async function loadList(kind) {
      const target = state[kind];
      const limit = kind === "open" ? OPEN_PAGE : CLOSED_PAGE;
      const data = await guarded(target, signal =>
        source.list({ kind, cursor: target.cursors.at(-1), limit, signal }));
      if (data) {
        target.rows = data.items;
        target.next = data.nextCursor ?? null;
        noteChanges(data.items);
      }
      render();
    }

    async function loadFlags() {
      const data = await guarded(state.flags, signal => source.flags({ signal }));
      if (data) state.flags.rows = data.flags;
      render();
    }

    async function loadCaptains() {
      const data = await guarded(state.captains, signal => source.captains({ signal }));
      if (data) state.captains.rows = data.captains;
      render();
    }

    function detailState(id) {
      let detail = state.details.get(id);
      if (!detail) {
        detail = { ...slot(), data: null };
        state.details.set(id, detail);
      }
      return detail;
    }

    async function loadDetail(id) {
      const detail = detailState(id);
      const data = await guarded(detail, signal =>
        source.detail({ id, cursor: detail.cursors.at(-1), limit: DETAIL_PAGE, signal }));
      if (data && state.details.get(id) === detail) {
        detail.data = data;
        detail.next = data.nextCursor ?? null;
      }
      if (state.expanded === id) render();
    }

    async function loadRoute() {
      const target = state.route;
      const data = await guarded(target, async signal => {
        const id = options.itemId ? (await source.currentItem(options.itemId, signal)).id : options.executionId;
        if (target.data && target.data.item.id !== id) target.cursors = [null];
        return source.detail({ id, cursor: target.cursors.at(-1), limit: DETAIL_PAGE, signal });
      });
      if (data) {
        target.data = data;
        target.next = data.nextCursor ?? null;
      }
      render();
    }

    function noteChanges(items) {
      for (const item of items) {
        const signature = [item.version, item.stage, item.status, item.outcome].join("|");
        const previous = state.seen.get(item.id);
        if (previous !== undefined && previous !== signature) state.changed.set(item.id, Date.now());
        state.seen.set(item.id, signature);
      }
    }

    function created(item) {
      // Include the acknowledged intake immediately, even on a later queue page.
      // Normal polling subsequently reconciles it with server-owned state.
      if (disposed || state.open.rows.some(row => row.id === item.id)) return;
      state.open.request?.abort();
      state.open.request = null;
      state.open.rows.push(item);
      state.changed.set(item.id, Date.now());
      const { scrollX: x, scrollY: y } = window;
      render();
      window.scrollTo(x, y);
      announce.textContent = "Work item queued: " + item.title;
    }

    function delivery(item, now) {
      const merge = item.delivery;
      if (!merge) return null;
      const parts = ["Merged on GitHub"];
      if (epoch(merge.mergedAt)) parts.push(ago(merge.mergedAt, now));
      if (merge.method) parts.push(label(merge.method));
      parts.push(h("code", { text: merge.mergeCommit.slice(0, 12), title: merge.mergeCommit }));
      return h("span", {}, ...parts.flatMap((part, index) => index ? [" · ", part] : [part]));
    }

    function toggle(id) {
      const previous = state.expanded;
      if (previous) {
        state.details.get(previous)?.request?.abort();
        state.details.delete(previous);
      }
      state.expanded = previous === id ? null : id;
      state.expandedAt = Date.now();
      render();
      if (state.expanded) loadDetail(state.expanded);
    }

    function changePage(kind, advance) {
      const target = kind === "detail" ? (routed ? state.route : detailState(state.expanded)) : state[kind];
      target.request?.abort();
      target.request = null;
      if (advance) target.cursors.push(target.next);
      else target.cursors.pop();
      target.next = null;
      if (kind === "detail") {
        if (routed) loadRoute();
        else {
          target.data = null;
          loadDetail(state.expanded);
        }
      } else {
        options.pageChanged?.(kind, target.cursors.at(-1));
        target.rows = [];
        target.updated = 0;
        loadList(kind);
      }
      render();
    }

    // Flags keyed by execution so rows can carry them as chips.
    function flagIndex() {
      const index = new Map();
      for (const flag of state.flags.rows) {
        const key = flag.executionId || flag.itemId;
        if (!key) continue;
        if (!index.has(key)) index.set(key, []);
        index.get(key).push(flag);
      }
      return index;
    }

    function flagsFor(index, item) {
      return [...(index.get(item.id) || []), ...(item.itemId ? index.get(item.itemId) || [] : [])];
    }

    function rowReason(item, flags) {
      return firstLine(item.waitReason) ||
        firstLine(item.writer?.progressNote) || firstLine(flags[0]?.reason) ||
        firstLine(item.resumeTrigger) || label(item.status);
    }

    function writerCell(item) {
      const writer = item.writer;
      if (!writer) return h("span", { class: "writer faint", text: "no writer" });
      const text = writer.state === "launching" ? "unconfirmed" : label(writer.state);
      const attrs = { class: "writer w-" + (writer.state || "unknown"),
        title: "Writer " + label(writer.state) + " · " + (writer.runtime || "runtime unknown") +
          (writer.captainId ? " · Captain " + short(writer.captainId) : "") };
      const url = safeUrl(writer.sessionUrl);
      return url ? h("a", { ...attrs, href: url, text, "data-focus": "writer:" + item.id }) : h("span", { ...attrs, text });
    }

    function prCell(item) {
      const url = safeUrl(item.primaryPrUrl || (item.output?.kind === "pr" ? item.output.url : null));
      if (url) return h("a", { class: "pr", href: url, text: prNumber(url), "data-focus": "pr:" + item.id });
      if (item.output?.kind === "report") {
        if (source.openReport) {
          const button = h("button", { type: "button", class: "link pr", text: "report",
            "data-report": item.id, "data-focus": "report:" + item.id });
          button.addEventListener("click", event => {
            event.stopPropagation();
            source.openReport(item.output);
          });
          return button;
        }
        const report = safeUrl(item.output.url);
        if (report) return h("a", { class: "pr", href: report, text: "report", "data-focus": "report:" + item.id });
      }
      return h("span", { class: "pr faint", text: "—" });
    }

    function chips(flags) {
      return flags.map(flag => h("span", { class: "chip " +
        (flag.kind === "deployed" ? "c-good" : flag.notifyOwner ? "c-attn" :
          flag.kind === "queue_backlogged" ? "c-info" : "c-warn"),
        title: flag.reason + " · " + flag.recovery }, flagGlyph(flag.notifyOwner, flag.kind === "queue_backlogged"), " ",
        h("span", { text: label(flag.kind) })));
    }

    function glyph(kind, text, markKind) {
      return h("span", { class: "glyph t-" + kind, title: text },
        markKind || ["run", "wait", "idle"].includes(kind) ? mark(markKind || kind) : h("span", { "aria-hidden": "true", text: GLYPH[kind] }),
        h("span", { class: "sr-only", text }));
    }

    function flagGlyph(notify, informational = false) {
      const text = notify ? "notifies owner" : informational ? "information" : "flag";
      return h("span", { class: "glyph flag-mark", title: text },
        informational && !notify ? h("span", { "aria-hidden": "true", text: "i" }) :
          mark(notify ? "notify" : "flag"), h("span", { class: "sr-only", text }));
    }

    function captainReference(id) {
      const captain = state.captains.rows.find(row => row.id === id);
      const word = captain?.live ? "live" : captain?.stale ? "stale" : captain?.state === "ended" ? "ended" : "status unavailable";
      return h("span", { class: "captain-ref" + (word === "ended" ? " captain-ended" : "") },
        glyph(captain?.live ? "run" : captain?.stale ? "attn" : "muted", word, "captain"),
        " Captain " + short(id) + " · " + word);
    }

    function itemRow(item, kind, index, now) {
      const flags = flagsFor(index, item);
      const closed = kind === "closed";
      const itemTone = tone(item);
      const expanded = state.expanded === item.id;
      const title = h("button", { type: "button", class: "title", "data-item": item.id, "data-focus": "item:" + item.id,
        "aria-expanded": String(expanded), "aria-controls": "detail-" + item.id, title: item.title, text: item.title });
      const stateText = closed ? label(outcomeKey(item)) : label(item.status);
      const cells = closed ? [
        h("span", { class: "stage", text: stateText }),
        h("span", { class: "age" }, "closed ", ago(item.closedAt, now)),
        prCell(item),
        h("span", { class: "dur", title: "Intake " + stamp(item.createdAt) + " · Closed " + stamp(item.closedAt) },
          epoch(item.createdAt) && epoch(item.closedAt) ? "took " + span(epoch(item.closedAt) - epoch(item.createdAt)) : "—",
          h("span", { class: "sr-only", text: " · Intake " + stamp(item.createdAt) + " · Closed " + stamp(item.closedAt) })),
        h("span", { class: "reason faint", text: shortfall(item) })
      ] : [
        h("span", { class: "stage", title: "Stage " + label(item.stage) + " · " + label(item.status),
          text: label(item.stage) + (item.publicationMode === "draft" ? " · draft only" : "") }),
        ago(item.lastAdvancedAt, now, { class: "age", "aria-label": "In stage" }),
        prCell(item),
        writerCell(item),
        h("span", { class: "reason", title: item.waitReason || "", text: rowReason(item, flags) })
      ];
      const line = h("div", { class: "line" },
        glyph(itemTone, stateText),
        h("span", { class: "name" }, title, chips(flags)),
        h("span", { class: "repo", title: item.repo, text: item.repo }),
        cells);
      line.addEventListener("click", event => {
        if (!event.target.closest("a, button")) toggle(item.id);
      });
      title.addEventListener("click", () => toggle(item.id));
      const changedAt = state.changed.get(item.id);
      const row = h("li", { class: "row t-" + itemTone + (expanded ? " open" : "") +
        (changedAt != null ? " changed" : ""), "data-id": item.id }, line);
      // A rebuilt row resumes its change highlight instead of restarting it.
      if (changedAt != null) row.style.setProperty("--since", changedAt - now + "ms");
      if (expanded) row.append(renderDetail(item, flags, now));
      return row;
    }

    function facts(pairs) {
      const list = h("dl", { class: "facts" });
      for (const [key, value] of pairs) {
        if (value == null || value === "" || value === false) continue;
        list.append(h("div", {}, h("dt", { text: key }), h("dd", {}, value)));
      }
      return list;
    }

    function stagePath(events, item, now) {
      const ordered = [...events].reverse();
      const segments = [];
      for (const event of ordered) {
        const last = segments.at(-1);
        if (last?.stage === event.stage) continue;
        segments.push({ stage: event.stage, start: epoch(event.at), back: backward(last?.stage, event.stage) });
      }
      if (!segments.length) return null;
      const end = epoch(item.closedAt) ?? now;
      const rail = h("ol", { class: "rail", "aria-label": "Stage path in this page of history" });
      segments.forEach((segment, position) => {
        const until = segments[position + 1]?.start ?? end;
        rail.append(h("li", { class: segment.back ? "back" : position === segments.length - 1 ? "now" : "" },
          segment.back ? h("span", { class: "loop", title: "Returned to an earlier stage", text: "↺ " }) : null,
          label(segment.stage), " ",
          h("span", { class: "faint", text: segment.start != null ? span(until - segment.start) : "" })));
      });
      const returns = segments.filter(segment => segment.back).length;
      return h("div", { class: "block" }, h("h4", { text: "Stage path" + (returns ? " · " + returns + " returns" : "") }), rail);
    }

    function renderDetail(row, flags, now, detail = state.details.get(row.id)) {
      const box = h("div", { class: "detail", id: "detail-" + row.id, role: "region", "aria-label": "Details: " + row.title });
      box.style.setProperty("--since", state.expandedAt - now + "ms");
      const data = detail?.data;
      if (!data) {
        box.append(h("p", { class: "faint", text: detail?.failed ? "Could not load details. Refresh to retry." : "Loading details…" }));
        return box;
      }
      const item = { ...row, ...data.item };
      const writer = item.writer;
      const sessionUrl = safeUrl(writer?.sessionUrl);
      const pr = safeUrl(item.primaryPrUrl || (item.output?.kind === "pr" ? item.output.url : null));
      const links = h("nav", { class: "detail-links", "aria-label": "Detail links" },
        item.itemId && h("a", { href: "/operator/items/" + encodeURIComponent(item.itemId), text: "Current item" }),
        h("a", { href: "/operator/executions/" + encodeURIComponent(item.id), text: "Execution permalink" }));
      for (const [id, text] of [[data.previousExecutionId, "Previous execution"], [data.nextExecutionId, "Next execution"]]) {
        if (id) links.append(h("a", { href: "/operator/executions/" + encodeURIComponent(id), text }));
      }
      box.append(links);
      box.append(h("div", { class: "goal", text: item.goal || item.title }));
      box.append(facts([
        ["Status", item.outcome ? label(outcomeKey(item)) : label(item.status)],
        ["Stage", h("span", {}, label(item.stage), " · ", ago(item.lastAdvancedAt, now))],
        ["Target", item.target && label(item.target)],
        ["Publication", item.publicationMode === "draft" ? "Draft only; no promotion or merge" : "Ready"],
        ["Version", item.version != null && "v" + item.version],
        ["Age", ago(item.createdAt, now)],
        ["Last progress", epoch(item.lastProgressAt) && ago(item.lastProgressAt, now)],
        ["Waiting", epoch(item.waitSince) && ago(item.waitSince, now)],
        ["Retry", item.retryAt && stamp(item.retryAt)],
        ["Recheck", item.recheckAt && stamp(item.recheckAt)],
        ["Attempt", data.attempt && h("code", { text: short(data.attempt), title: data.attempt })],
        ["Writer", writer && h("span", {}, label(writer.state), " · ", writer.runtime || "runtime unknown",
          writer.captainId ? h("span", {}, " · ", captainReference(writer.captainId)) : "",
          writer.sessionId ? h("code", { text: " " + short(writer.sessionId), title: writer.sessionId }) : "",
          sessionUrl ? h("span", {}, " · ", h("a", { href: sessionUrl, text: "session" })) : "")],
        ["PR", pr && h("a", { href: pr, text: prNumber(pr) + " ↗" })],
        ["Delivery", delivery(item, now)],
        ["Output", item.output?.kind === "report" ? prCell(item) : null],
        ["IDs", h("code", { title: "item " + (item.itemId || "?") + " · execution " + item.id,
          text: short(item.itemId) + " / " + short(item.id) })],
        ...historicalWorkerFacts(item)
      ]));
      for (const [heading, text] of [
        ["Wait reason", item.waitReason], ["Resumes on", item.resumeTrigger],
        ["Next action", data.nextAction], ["Writer note", writer?.progressNote]
      ]) {
        if (text) box.append(h("div", { class: "block" },
          h("h4", { text: heading }), h("p", { class: "pre", text })));
      }
      if (flags.length) {
        const list = h("ul", { class: "plain" });
        for (const flag of flags) {
          list.append(h("li", {}, ...chips([flag]),
            " " + flag.reason + " ", h("span", { class: "faint", text: "→ " + flag.recovery + " · " }), ago(flag.firstSeen, now)));
        }
        box.append(h("div", { class: "block" }, h("h4", { text: "Flags" }), list));
      }
      if (data.question) {
        const question = h("div", { class: "block ask" }, h("h4", { text: "Question for the owner" }),
          h("p", { class: "pre", text: data.question.prompt }));
        if (data.question.options?.length) {
          question.append(h("ul", { class: "options" }, data.question.options.map(option =>
            h("li", { class: option === data.question.proposedDefault ? "default" : "", text: option }))));
        }
        if (data.question.proposedDefault) question.append(h("p", { class: "faint pre", text: "Proposed default: " + data.question.proposedDefault }));
        if (item.itemId && item.version) {
          question.append(h("p", {}, h("code", { class: "cmd", text: "python -m phalanx reply " + item.itemId + " " + item.id +
            " \"ANSWER\" --version " + item.version + " --key KEY" })));
        }
        box.append(question);
      }
      if (data.dependencies?.length) {
        box.append(h("div", { class: "block" }, h("h4", { text: "Prerequisites" }), h("ul", { class: "plain" },
          data.dependencies.map(dependency => h("li", { text: dependency.title + " · " + label(dependency.milestone) +
            " · " + (dependency.reason || label(dependency.status)) })))));
      }
      const path = stagePath(data.events, item, now);
      if (path) box.append(path);
      const table = h("table", { class: "timeline" },
        h("thead", {}, h("tr", {}, ["When", "Event", "Stage", "Status", "Reason"].map(name => h("th", { scope: "col", text: name })))));
      const rows = h("tbody");
      for (const event of data.events) {
        const back = backward(event.fromStage, event.stage);
        rows.append(h("tr", { class: back ? "back" : "" },
          h("td", {}, ago(event.at, now)),
          h("td", { text: label(event.event || "transition") }),
          h("td", {}, event.fromStage ? h("span", { class: "faint", text: label(event.fromStage) + " → " }) : null,
            back ? h("span", { class: "loop", text: "↺ " }) : null, label(event.stage)),
          h("td", { text: label(event.status) }),
          h("td", { class: "pre", text: event.reason || "" })));
      }
      if (!data.events.length) rows.append(h("tr", {}, h("td", { colspan: "5", class: "faint", text: "No transitions on this page." })));
      table.append(rows);
      box.append(h("div", { class: "block" }, h("h4", { text: "History" }), h("div", { class: "scroll" }, table)));
      const historyPager = pager("detail", detail, "history");
      if (historyPager) box.append(historyPager);
      return box;
    }

    function pager(kind, target, name) {
      const nav = h("nav", { class: "pager", "aria-label": name + " pages" });
      if (target.cursors[0] !== null) {
        const first = h("button", { type: "button", class: "ghost", text: "First page",
          "data-focus": "first:" + kind });
        first.addEventListener("click", () => {
          target.cursors = [null, target.cursors.at(-1)];
          changePage(kind, false);
        });
        nav.append(first);
      }
      if (target.cursors.length > 1) {
        const newer = h("button", { type: "button", class: "ghost", "data-focus": "newer:" + kind,
          "aria-label": "Previous " + name + " page", text: "‹ Newer" });
        newer.addEventListener("click", () => changePage(kind, false));
        nav.append(newer);
      }
      if (target.cursors.length > 1 || target.next) nav.append(h("span", { class: "faint", text: "page " + target.cursors.length }));
      if (target.next) {
        const older = h("button", { type: "button", class: "ghost", "data-focus": "older:" + kind,
          "aria-label": "Next " + name + " page", text: "More ›" });
        older.addEventListener("click", () => changePage(kind, true));
        nav.append(older);
      }
      return nav.childElementCount ? nav : null;
    }

    function section(id, title, count, toneName, content, extra) {
      const heading = h("h2", { id: "h-" + id },
        h("span", { text: title }), h("span", { class: "count" + (toneName ? " t-" + toneName : ""), text: String(count) }), extra);
      return h("section", { class: "sec sec-" + id, "aria-labelledby": "h-" + id }, heading, content);
    }

    function list(rows, empty, columns) {
      if (!rows.length) return h("p", { class: "empty", text: empty });
      return h("ul", { class: "rows " + columns }, rows);
    }

    function flagGroups(flags, titles, now) {
      const groups = new Map();
      for (const flag of flags) {
        if (!groups.has(flag.kind)) groups.set(flag.kind, []);
        groups.get(flag.kind).push(flag);
      }
      const ordered = [...groups.entries()].sort((a, b) =>
        Number(b[1].some(flag => flag.notifyOwner)) - Number(a[1].some(flag => flag.notifyOwner)) ||
        Number(a[0] === "queue_backlogged") - Number(b[0] === "queue_backlogged") || b[1].length - a[1].length);
      return ordered.map(([kind, members]) => {
        const notify = members.some(flag => flag.notifyOwner);
        const informational = kind === "queue_backlogged" && !notify;
        const oldest = Math.min(...members.map(flag => epoch(flag.firstSeen) ?? now));
        const group = h("details", { class: "fgroup t-" + (notify ? "attn" : informational ? "info" : "wait"), "data-kind": kind });
        if (state.flagGroups.has(kind)) group.open = true;
        group.addEventListener("toggle", () => {
          if (group.open) state.flagGroups.add(kind);
          else state.flagGroups.delete(kind);
        });
        const reasons = [...new Set(members.map(flag => flag.reason))];
        group.append(h("summary", { class: "line", "data-focus": "flag:" + kind },
          flagGlyph(notify, informational),
          h("span", { class: "name" }, h("strong", { text: label(kind) }), h("span", { class: "faint", text: " ×" + members.length })),
          h("span", { class: "reason", title: reasons.join("\n"), text: reasons.length === 1 ? reasons[0] : reasons.length + " distinct reasons" }),
          h("span", { class: "age" }, ago(oldest, now, { title: "Oldest first seen " + stamp(oldest) }))));
        group.append(h("ul", { class: "flist" }, members.map(flag => {
          const subject = titles.get(flag.executionId) || titles.get(flag.itemId) ||
            (flag.itemId ? "item " + short(flag.itemId) : flag.captainId ? "Captain " + short(flag.captainId) : "system");
          return h("li", { class: "line" }, h("span", {}),
            h("span", { class: "name", title: subject }, flag.captainId ? captainReference(flag.captainId) :
              flag.executionId || flag.itemId ? h("a", {
                href: "/operator/" + (flag.executionId ? "executions/" + encodeURIComponent(flag.executionId) :
                  "items/" + encodeURIComponent(flag.itemId)), text: subject
              }) : subject),
            h("span", { class: "reason", title: flag.reason + "\nRecovery: " + flag.recovery,
              text: flag.reason + " → " + flag.recovery }),
            ago(flag.firstSeen, now, { class: "age" }));
        })));
        return group;
      });
    }

    function captainRow(captain, titles, now) {
      const health = captain.live ? "run" : captain.stale ? "attn" : "muted";
      const word = captain.live ? "live" : captain.stale ? "stale" : label(captain.state);
      const focus = (captain.focus || []).map(id => titles.get(id) || short(id));
      return h("li", { class: "row t-" + health + (captain.state === "ended" ? " captain-ended" : "") }, h("div", { class: "line" },
        glyph(health, word, "captain"),
        h("span", { class: "name" }, h("strong", { text: "Captain " + short(captain.id) }),
          h("span", { class: "faint", text: " " + word })),
        h("span", { class: "stat" }, h("span", { class: captain.live ? "beat" : "", "aria-hidden": "true", text: "♥" }),
          " heartbeat ", ago(captain.heartbeatAt, now)),
        h("span", { class: "stat", text: "wakes " + (captain.wakeCount ?? 0) }),
        h("span", { class: "stat", text: "compactions " +
          (Number.isSafeInteger(captain.compactionCount) && captain.compactionCount >= 0
            ? captain.compactionCount : "unavailable") }),
        h("span", { class: "reason", title: [focus.join(", "), captain.handoffNote].filter(Boolean).join("\n"),
          text: captain.handoffNote ? "handoff: " + firstLine(captain.handoffNote)
            : focus.length ? "focus: " + focus.join(", ") : "no focus" })));
    }

    function freshness() {
      const visible = ["open", "closed", "route", "flags", "captains"].filter(kind => enabled[kind]).map(kind => state[kind]);
      const dated = visible.map(target => target.updated).filter(Boolean);
      const failed = visible.filter(target => target.failed).length;
      const oldest = dated.length ? Math.min(...dated) : 0;
      if (!dated.length) {
        return failed ? { kind: "down", text: "Disconnected", note: "Refresh to retry." } : { kind: "wait", text: "Connecting…", note: "" };
      }
      const note = "updated " + span(Date.now() - oldest) + " ago";
      if (failed === visible.length) return { kind: "down", text: "Disconnected", note: "showing data from " + absolute.format(oldest), oldest };
      if (failed) return { kind: "stale", text: "Partial", note: "some reads failed · " + note, oldest };
      if (Date.now() - oldest >= STALE_MS) return { kind: "stale", text: "Stale", note, oldest };
      return { kind: "live", text: "Live", note, oldest };
    }

    let lastAnnounced = "";
    function renderHealth() {
      const fresh = freshness();
      health.className = "health h-" + fresh.kind;
      syncClock(health);
      health.replaceChildren(h("span", { class: "dot", "aria-hidden": "true" }), h("strong", { text: fresh.text }),
        h("span", { class: "faint", title: fresh.oldest ? "Oldest displayed update " + stamp(fresh.oldest) : "", text: fresh.note }));
      const message = fresh.kind === "live" ? "Connected." : fresh.kind === "down"
        ? "Disconnected. Previously loaded data may be stale." : fresh.kind === "stale"
          ? "Previously loaded data may be stale." : "";
      if (message !== lastAnnounced) {
        lastAnnounced = message;
        announce.textContent = message;
      }
    }

    function render() {
      if (disposed) return;
      const endedGroup = body.querySelector(".ended-captains");
      if (endedGroup) state.endedOpen = endedGroup.open;
      const now = Date.now();
      for (const [id, at] of state.changed) if (now - at >= CHANGED_MS) state.changed.delete(id);
      const focused = root.contains(document.activeElement) ? document.activeElement.dataset?.focus : null;
      const index = flagIndex();
      const titles = new Map();
      for (const item of [...state.open.rows, ...state.closed.rows]) {
        titles.set(item.id, item.title);
        if (item.itemId) titles.set(item.itemId, item.title);
      }
      const needs = [];
      const active = [];
      const queued = [];
      const shown = new Set();
      for (const item of state.open.rows) {
        const flags = flagsFor(index, item);
        if (item.status === "waiting_for_input" || item.status === "failed" || flags.some(flag => flag.notifyOwner)) needs.push(item);
        else if (item.status === "running" || item.writer) active.push(item);
        else queued.push(item);
      }
      for (const item of needs) for (const flag of flagsFor(index, item)) shown.add(flag);
      const looseNotify = state.flags.rows.filter(flag => flag.notifyOwner && !shown.has(flag));
      for (const flag of looseNotify) shown.add(flag);
      const otherFlags = state.flags.rows.filter(flag => !shown.has(flag));
      const more = state.open.next ? "+" : "";

      const sections = [];
      if (routed) {
        const data = state.route.data;
        const content = data ? renderDetail(data.item, flagsFor(index, data.item), now, state.route)
          : h("p", { class: state.route.failed ? "warn" : "empty",
            text: state.route.failed ? "Could not load this detail. It may not exist. Refresh to retry." : "Loading details..." });
        sections.push(section("detail", data?.item.title || "Work detail", "", "", content));
        if (data && state.route.failed) content.append(h("p", {
          class: "warn", text: "Detail update failed. Showing the previous contents."
        }));
      }
      const needRows = needs.map(item => itemRow(item, "open", index, now));
      const needContent = h("div", {});
      if (looseNotify.length) needContent.append(h("div", { class: "rows flags" }, flagGroups(looseNotify, titles, now)));
      needContent.append(needRows.length || !looseNotify.length
        ? list(needRows, state.open.updated ? "Nothing needs you." : "Loading…", "items") : "");
      sections.push(section("needs", "Needs you", needs.length + looseNotify.length,
        needs.length + looseNotify.length ? "attn" : "", needContent));
      if (enabled.flags) {
        sections.push(section("flags", "Flags", otherFlags.length,
          otherFlags.some(flag => flag.kind !== "queue_backlogged") ? "wait" : "",
          otherFlags.length ? h("div", { class: "rows flags" }, flagGroups(otherFlags, titles, now))
            : h("p", { class: "empty", text: state.flags.failed ? "Flags unavailable." : "No flags. Nothing has drifted." })));
      }
      if (enabled.captains) {
        const live = state.captains.rows.filter(captain => captain.live).length;
        const current = state.captains.rows.filter(captain => captain.state !== "ended")
          .sort((a, b) => Number(b.live) - Number(a.live));
        const ended = state.captains.rows.filter(captain => captain.state === "ended");
        const content = h("div", {}, list(current.map(captain => captainRow(captain, titles, now)),
          state.captains.failed ? "Captains unavailable." : "No live Captain.", "captains"));
        if (ended.length) {
          const group = h("details", { class: "ended-captains", open: state.endedOpen });
          group.append(h("summary", { "data-focus": "ended-captains", text: "Ended (" + ended.length + ")" }),
            list(ended.map(captain => captainRow(captain, titles, now)), "", "captains"));
          group.addEventListener("toggle", () => { if (group.isConnected) state.endedOpen = group.open; });
          content.append(group);
        }
        sections.push(section("captains", "Captains", live + "/" + state.captains.rows.length, live ? "" : "attn",
          content));
      }
      sections.push(section("active", "Active", active.length + (active.length ? more : ""), "",
        list(active.map(item => itemRow(item, "open", index, now)), "Nothing running.", "items")));
      const queueRows = page === "queue" ? state.open.rows : queued;
      const queuedSection = section("queued", page === "queue" ? "Queue" : "Queued and waiting",
        queueRows.length + (queueRows.length ? more : ""), "",
        list(queueRows.map(item => itemRow(item, "open", index, now)),
          page === "queue" ? (state.open.updated ? "No open work on this page." : "Loading...") : "Nothing queued.", "items"));
      const openPager = pager("open", state.open, "open work");
      if (openPager) queuedSection.append(openPager);
      if (state.open.failed) queuedSection.append(h("p", { class: "warn", text: state.open.updated
        ? "Open work update failed. Showing the previous contents." : "Could not load open work. Check URL filters and Refresh to retry." }));
      sections.push(queuedSection);
      const closedSection = section("closed", page === "history" ? "History" : "Recently closed",
        state.closed.rows.length + (state.closed.next ? "+" : ""), "",
        list(state.closed.rows.map(item => itemRow(item, "closed", index, now)),
          state.closed.updated ? "No closed work on this page." : "Loading…", "items closed"));
      const closedPager = pager("closed", state.closed, "recent outcomes");
      if (closedPager) closedSection.append(closedPager);
      if (state.closed.failed) closedSection.append(h("p", { class: "warn", text: state.closed.updated
        ? "Closed work update failed. Showing the previous contents." : "Could not load closed work. Check URL filters and Refresh to retry." }));
      sections.push(closedSection);

      syncClock(body);
      body.replaceChildren(...sections.filter(section => {
        if (routed) return section.classList.contains("sec-detail");
        if (page === "queue") return section.classList.contains("sec-queued");
        if (page === "history") return section.classList.contains("sec-closed");
        if (["sec-needs", "sec-active", "sec-queued"].some(name => section.classList.contains(name))) return enabled.open;
        if (section.classList.contains("sec-closed")) return enabled.closed;
        return true;
      }));
      counts.replaceChildren(
        h("li", { class: needs.length + looseNotify.length ? "t-attn" : "" }, h("strong", { text: String(needs.length + looseNotify.length) }), " needs you"),
        h("li", {}, h("strong", { text: active.length + (active.length ? more : "") }), " active"),
        h("li", {}, h("strong", { text: queued.length + (queued.length ? more : "") }), " queued"),
        enabled.flags ? h("li", { class: state.flags.rows.some(flag => flag.notifyOwner || flag.kind !== "queue_backlogged") ? "t-wait" : "" },
          h("strong", { text: String(state.flags.rows.length) }), " flags") : null,
        enabled.captains ? h("li", {}, h("strong", { text: state.captains.rows.filter(captain => captain.live).length + "/" + state.captains.rows.length }), " Captains live") : null);
      renderHealth();
      if (page !== "home") counts.replaceChildren();
      if (focused) {
        const target = root.querySelector('[data-focus="' + CSS.escape(focused) + '"]');
        if (target && document.activeElement !== target) target.focus({ preventScroll: true });
      }
    }

    function refreshAll() {
      if (document.hidden) return;
      if (enabled.open) loadList("open");
      if (enabled.closed) loadList("closed");
      if (routed) loadRoute();
      if (enabled.flags) loadFlags();
      if (enabled.captains) loadCaptains();
      if (state.expanded) loadDetail(state.expanded);
    }

    function onKey(event) {
      const current = event.target.closest?.("button.title");
      if (event.key === "Escape" && state.expanded && !routed) {
        const id = state.expanded;
        toggle(id);
        root.querySelector('button.title[data-item="' + CSS.escape(id) + '"]')?.focus();
        return;
      }
      if (!current || !["ArrowDown", "ArrowUp", "j", "k"].includes(event.key)) return;
      const buttons = [...root.querySelectorAll("button.title")];
      const step = event.key === "ArrowDown" || event.key === "j" ? 1 : -1;
      const next = buttons[buttons.indexOf(current) + step];
      if (next) {
        event.preventDefault();
        next.focus();
      }
    }

    refresh.addEventListener("click", refreshAll);
    root.addEventListener("keydown", onKey);
    const interval = setInterval(refreshAll, REFRESH_MS);
    const ticker = setInterval(() => { if (!document.hidden) renderHealth(); }, TICK_MS);
    document.addEventListener("visibilitychange", refreshAll);
    applyTheme();
    render();
    refreshAll();

    return () => {
      disposed = true;
      clearConcurrency?.();
      clearInterval(interval);
      clearInterval(ticker);
      document.removeEventListener("visibilitychange", refreshAll);
      root.removeEventListener("keydown", onKey);
      for (const target of [state.open, state.closed, state.route, state.flags, state.captains, ...state.details.values()]) target.request?.abort();
      root.replaceChildren();
    };
  }

  window.PhalanxOperatorBoard = { mount, label };
})();
