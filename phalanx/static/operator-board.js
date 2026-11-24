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
  const STAGES = ["development", "pr_creation", "self_review", "pre_pr_validation", "pr_promotion",
    "pr_validation", "adversarial_review", "pr_reconciliation", "pr_revision", "check_in",
    "report_drafting", "report_review", "report_storage"];
  const GLYPH = { attn: "!", ok: "✓", fail: "✕", muted: "–" };
  const DISPLAY_LABEL = { checked_in: "Merged" };
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
      path("M8 .5 11.5 8 8 10.5 4.5 8Z", { fill: "currentColor" });
      path("M8 10V15", { stroke: "currentColor", "stroke-width": "1.5" });
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
    return Boolean(from) && (STAGES.indexOf(to) < STAGES.indexOf(from) || to === "pr_revision");
  }

  // Old runner path (worker reference, chat, slots). Delete with direct sessions step 5.
  function legacyRunnerFacts(item) {
    const legacy = item.legacy || {};
    const chat = safeUrl(legacy.chatUrl);
    return [
      legacy.workerRef && ["Worker", legacy.workerRef],
      chat && ["Worker chat", h("a", { href: chat, text: "open" })],
      legacy.slotNote && ["Slot", legacy.slotNote]
    ].filter(Boolean);
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
   * legacy?: {workerRef?, chatUrl?, slotNote?} for the old runner path.
   * Optional source.flags({signal}) resolves to {flags: [{kind, itemId?, executionId?, captainId?, reason,
   * recovery, firstSeen, notifyOwner}]}, and source.captains({signal}) to {captains: [{id, sessionRef,
   * state, live, stale, heartbeatAt, wakeCount, charterRevision?, focus, handoffNote?}]}.
   * source.detail({id, cursor, limit, signal}) resolves to
   * {item, attempt?, nextAction?, question?, dependencies?, events, nextCursor}.
   * question is {prompt, options, proposedDefault?}. Dependencies are {title, milestone, status, reason?}.
   * Events are newest first: {at, event?, fromStage?, stage, status, reason?}. Dates are ISO strings.
   * source.openReport?(output) opens a stored report in the host. Reject failed reads.
   */
  function mount(root, source) {
    const slot = () => ({ rows: [], cursors: [null], next: null, updated: 0, failed: false, request: null });
    const state = {
      open: slot(), closed: slot(), flags: slot(), captains: slot(),
      expanded: null, expandedAt: 0, endedOpen: false, details: new Map(), flagGroups: new Set(), seen: new Map(), changed: new Map()
    };
    const enabled = { open: true, closed: true, flags: Boolean(source.flags), captains: Boolean(source.captains) };
    let disposed = false;

    const announce = h("p", { class: "sr-only", role: "status" });
    const health = h("div", { class: "health" });
    const counts = h("ul", { class: "counts", "aria-label": "Summary" });
    const theme = h("button", { type: "button", class: "ghost", "data-action": "theme" });
    const refresh = h("button", { type: "button", class: "ghost", "data-action": "refresh", text: "Refresh" });
    const header = h("header", { class: "bar" },
      h("div", { class: "brand" }, h("span", { class: "mark", "aria-hidden": "true" }),
        h("h1", { text: "Phalanx", tabindex: "-1" }), h("span", { class: "faint sub", text: "Scribe" })),
      health, counts, h("div", { class: "actions" }, theme, refresh));
    const body = h("div", { class: "sections" });
    root.replaceChildren(header, announce, body);

    function applyTheme() {
      let choice = "auto";
      try { choice = localStorage.getItem(THEME_KEY) || "auto"; } catch { /* Storage may be unavailable. */ }
      if (choice === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = choice;
      theme.textContent = { auto: "◐ Auto", dark: "● Dark", light: "○ Light" }[choice] || "◐ Auto";
      theme.setAttribute("aria-label", "Theme: " + choice + ". Change theme");
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

    function noteChanges(items) {
      for (const item of items) {
        const signature = [item.version, item.stage, item.status, item.outcome].join("|");
        const previous = state.seen.get(item.id);
        if (previous !== undefined && previous !== signature) state.changed.set(item.id, Date.now());
        state.seen.set(item.id, signature);
      }
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
      const target = kind === "detail" ? detailState(state.expanded) : state[kind];
      target.request?.abort();
      target.request = null;
      if (advance) target.cursors.push(target.next);
      else target.cursors.pop();
      target.next = null;
      if (kind === "detail") {
        target.data = null;
        loadDetail(state.expanded);
      } else {
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
        (flag.kind === "deployed" ? "c-good" : flag.notifyOwner ? "c-attn" : "c-warn"),
        title: flag.reason + " · " + flag.recovery }, flagGlyph(flag.notifyOwner), " ",
        h("span", { text: label(flag.kind) })));
    }

    function glyph(kind, text) {
      return h("span", { class: "glyph t-" + kind, title: text },
        ["run", "wait", "idle"].includes(kind) ? mark(kind) : h("span", { "aria-hidden": "true", text: GLYPH[kind] }),
        h("span", { class: "sr-only", text }));
    }

    function flagGlyph(notify) {
      const text = notify ? "notifies owner" : "flag";
      return h("span", { class: "glyph flag-mark", title: text },
        mark(notify ? "notify" : "flag"), h("span", { class: "sr-only", text }));
    }

    function captainReference(id) {
      const captain = state.captains.rows.find(row => row.id === id);
      const word = captain?.live ? "live" : captain?.stale ? "stale" : captain?.state === "ended" ? "ended" : "status unavailable";
      return h("span", { class: "captain-ref" + (word === "ended" ? " captain-ended" : "") },
        glyph(captain?.live ? "run" : captain?.stale ? "attn" : "muted", word),
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

    function renderDetail(row, flags, now) {
      const detail = state.details.get(row.id);
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
        ["Wait reason", item.waitReason && h("span", { class: "pre", text: item.waitReason })],
        ["Resumes on", item.resumeTrigger],
        ["Next action", data.nextAction],
        ["Retry", item.retryAt && stamp(item.retryAt)],
        ["Recheck", item.recheckAt && stamp(item.recheckAt)],
        ["Attempt", data.attempt && h("code", { text: short(data.attempt), title: data.attempt })],
        ["Writer", writer && h("span", {}, label(writer.state), " · ", writer.runtime || "runtime unknown",
          writer.captainId ? h("span", {}, " · ", captainReference(writer.captainId)) : "",
          writer.sessionId ? h("code", { text: " " + short(writer.sessionId), title: writer.sessionId }) : "",
          sessionUrl ? h("span", {}, " · ", h("a", { href: sessionUrl, text: "session" })) : "")],
        ["Writer note", writer?.progressNote],
        ["PR", pr && h("a", { href: pr, text: prNumber(pr) + " ↗" })],
        ["Delivery", delivery(item, now)],
        ["Output", item.output?.kind === "report" ? prCell(item) : null],
        ["IDs", h("code", { title: "item " + (item.itemId || "?") + " · execution " + item.id,
          text: short(item.itemId) + " / " + short(item.id) })],
        ...legacyRunnerFacts(item)
      ]));
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
        if (data.question.proposedDefault) question.append(h("p", { class: "faint", text: "Proposed default: " + data.question.proposedDefault }));
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
        Number(b[1].some(flag => flag.notifyOwner)) - Number(a[1].some(flag => flag.notifyOwner)) || b[1].length - a[1].length);
      return ordered.map(([kind, members]) => {
        const notify = members.some(flag => flag.notifyOwner);
        const oldest = Math.min(...members.map(flag => epoch(flag.firstSeen) ?? now));
        const group = h("details", { class: "fgroup t-" + (notify ? "attn" : "wait"), "data-kind": kind });
        if (state.flagGroups.has(kind)) group.open = true;
        group.addEventListener("toggle", () => {
          if (group.open) state.flagGroups.add(kind);
          else state.flagGroups.delete(kind);
        });
        const reasons = [...new Set(members.map(flag => flag.reason))];
        group.append(h("summary", { class: "line", "data-focus": "flag:" + kind },
          flagGlyph(notify),
          h("span", { class: "name" }, h("strong", { text: label(kind) }), h("span", { class: "faint", text: " ×" + members.length })),
          h("span", { class: "reason", title: reasons.join("\n"), text: reasons.length === 1 ? reasons[0] : reasons.length + " distinct reasons" }),
          h("span", { class: "age" }, ago(oldest, now, { title: "Oldest first seen " + stamp(oldest) }))));
        group.append(h("ul", { class: "flist" }, members.map(flag => {
          const subject = titles.get(flag.executionId) || titles.get(flag.itemId) ||
            (flag.itemId ? "item " + short(flag.itemId) : flag.captainId ? "Captain " + short(flag.captainId) : "system");
          return h("li", { class: "line" }, h("span", {}),
            h("span", { class: "name", title: subject }, flag.captainId ? captainReference(flag.captainId) : subject),
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
        glyph(health, word),
        h("span", { class: "name" }, h("strong", { text: "Captain " + short(captain.id) }),
          h("span", { class: "faint", text: " " + word })),
        h("span", { class: "stat" }, h("span", { class: captain.live ? "beat" : "", "aria-hidden": "true", text: "♥" }),
          " heartbeat ", ago(captain.heartbeatAt, now)),
        h("span", { class: "stat", text: "wakes " + (captain.wakeCount ?? 0) }),
        h("span", { class: "stat", title: captain.charterRevision || "", text: captain.charterRevision ? "charter " + captain.charterRevision.slice(0, 7) : "no charter" }),
        h("span", { class: "reason", title: [focus.join(", "), captain.handoffNote].filter(Boolean).join("\n"),
          text: captain.handoffNote ? "handoff: " + firstLine(captain.handoffNote)
            : focus.length ? "focus: " + focus.join(", ") : "no focus" })));
    }

    function freshness() {
      const visible = ["open", "closed", "flags", "captains"].filter(kind => enabled[kind]).map(kind => state[kind]);
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
      const needRows = needs.map(item => itemRow(item, "open", index, now));
      const needContent = h("div", {});
      if (looseNotify.length) needContent.append(h("div", { class: "rows flags" }, flagGroups(looseNotify, titles, now)));
      needContent.append(needRows.length || !looseNotify.length
        ? list(needRows, state.open.updated ? "Nothing needs you." : "Loading…", "items") : "");
      sections.push(section("needs", "Needs you", needs.length + looseNotify.length,
        needs.length + looseNotify.length ? "attn" : "", needContent));
      if (enabled.flags) {
        sections.push(section("flags", "Flags", otherFlags.length, otherFlags.length ? "wait" : "",
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
      const queuedSection = section("queued", "Queued and waiting", queued.length + (queued.length ? more : ""), "",
        list(queued.map(item => itemRow(item, "open", index, now)), "Nothing queued.", "items"));
      const openPager = pager("open", state.open, "open work");
      if (openPager) queuedSection.append(openPager);
      if (state.open.failed) queuedSection.append(h("p", { class: "warn", text: "Open work update failed. Showing the previous contents." }));
      sections.push(queuedSection);
      const closedSection = section("closed", "Recently closed", state.closed.rows.length + (state.closed.next ? "+" : ""), "",
        list(state.closed.rows.map(item => itemRow(item, "closed", index, now)),
          state.closed.updated ? "No closed work on this page." : "Loading…", "items closed"));
      const closedPager = pager("closed", state.closed, "recent outcomes");
      if (closedPager) closedSection.append(closedPager);
      if (state.closed.failed) closedSection.append(h("p", { class: "warn", text: "Closed work update failed. Showing the previous contents." }));
      sections.push(closedSection);

      syncClock(body);
      body.replaceChildren(...sections);
      counts.replaceChildren(
        h("li", { class: needs.length + looseNotify.length ? "t-attn" : "" }, h("strong", { text: String(needs.length + looseNotify.length) }), " needs you"),
        h("li", {}, h("strong", { text: active.length + (active.length ? more : "") }), " active"),
        h("li", {}, h("strong", { text: queued.length + (queued.length ? more : "") }), " queued"),
        enabled.flags ? h("li", { class: state.flags.rows.length ? "t-wait" : "" }, h("strong", { text: String(state.flags.rows.length) }), " flags") : null,
        enabled.captains ? h("li", {}, h("strong", { text: state.captains.rows.filter(captain => captain.live).length + "/" + state.captains.rows.length }), " Captains live") : null);
      renderHealth();
      if (focused) {
        const target = root.querySelector('[data-focus="' + CSS.escape(focused) + '"]');
        if (target && document.activeElement !== target) target.focus({ preventScroll: true });
      }
    }

    function refreshAll() {
      if (document.hidden) return;
      loadList("open");
      loadList("closed");
      if (enabled.flags) loadFlags();
      if (enabled.captains) loadCaptains();
      if (state.expanded) loadDetail(state.expanded);
    }

    function onKey(event) {
      const current = event.target.closest?.("button.title");
      if (event.key === "Escape" && state.expanded) {
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
      clearInterval(interval);
      clearInterval(ticker);
      document.removeEventListener("visibilitychange", refreshAll);
      root.removeEventListener("keydown", onKey);
      for (const target of [state.open, state.closed, state.flags, state.captains, ...state.details.values()]) target.request?.abort();
      root.replaceChildren();
    };
  }

  window.PhalanxOperatorBoard = { mount };
})();
