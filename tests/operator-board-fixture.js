/* Open operator-board-fixture.html in a browser. Sample data lives only here. */
(async () => {
  const root = document.querySelector("main");
  const result = document.querySelector("#result");
  const now = new Date().toISOString();
  const hourAgo = new Date(Date.now() - 3600000).toISOString();
  const nativeId = "12345678-1234-1234-1234-123456789abc";
  const copilotUrl = "ghapp://sessions/" + nativeId;
  const codexUrl = "codex://threads/" + nativeId;
  const waiting = {
    id: "wait", itemId: "item-wait", title: "<img src=x> Choose deployment target",
    goal: "<img src=x> Choose deployment target.\nTwo targets match.", repo: "example/service",
    stage: "development", status: "waiting_for_input", lastAdvancedAt: hourAgo, createdAt: hourAgo,
    waitReason: "Two targets match the work request.", primaryPrUrl: "javascript:alert(1)"
  };
  const running = {
    id: "run", itemId: "item-run", title: "Update telemetry", goal: "Update telemetry", target: "checked_in",
    repo: "example/service", stage: "self_review", status: "running", lastAdvancedAt: now, createdAt: hourAgo,
    primaryPrUrl: "https://example.com/pull/7",
    nextAction: "A worker is running the self_review stage",
    writer: { state: "launching", runtime: "copilot-app", captainId: "cap-1", sessionId: nativeId }
  };
  const queued = {
    id: "queue", itemId: "item-queue", title: "Tidy docs", repo: "example/docs",
    stage: "development", status: "queued", lastAdvancedAt: hourAgo, publicationMode: "draft"
  };
  const external = { ...queued, id: "external", title: "Await CI", status: "waiting_for_external" };
  const resumed = { ...queued, id: "resumed", title: "Resumed work",
    writer: { state: "ended", runtime: "codex", sessionId: nativeId } };
  const failure = {
    id: "fail", title: "Audit build", repo: "example/service", stage: "report_drafting",
    status: "failed", outcome: "failed", target: "checked_in", closedAt: now, createdAt: hourAgo,
    output: { kind: "report", url: "https://example.com/reports/build" }
  };
  const success = {
    ...failure, id: "done", title: "Verify rollout", status: "completed", outcome: "checked_in",
    output: { kind: "pr", url: "https://example.com/pull/1" },
    writer: { state: "ended", runtime: "codex", sessionId: nativeId },
    delivery: { mergeCommit: "a40536e" + "0".repeat(33), method: "squash_or_rebase", mergedAt: now }
  };
  const draft = {
    ...success, id: "draft", title: "Reviewable draft", outcome: "draft_published",
    publicationMode: "draft", delivery: undefined,
    output: { kind: "pr", url: "https://example.com/pull/2" }
  };
  const observed = { ...success, id: "observed", title: "Ready, now merged on GitHub",
    outcome: "ready_for_check_in", prState: "merged", delivery: undefined };
  const ready = { ...observed, id: "ready", title: "Ready-only rerun", target: "ready_for_check_in", prState: "open" };
  const short = { ...ready, id: "short", title: "Ready but wanted merge", target: "checked_in" };
  const cancelled = { ...failure, id: "cancelled", title: "Cancelled rollout", outcome: "cancelled", status: "cancelled" };
  const report = { ...failure, id: "report", title: "Stored report", outcome: "report_stored", target: "report", status: "completed" };
  const flags = [
    { kind: "deployed", executionId: "done", reason: "deploy succeeded", recovery: "Informational", firstSeen: now, notifyOwner: false },
    { kind: "queue_backlogged", itemId: "item-queue", executionId: "queue", reason: "Waiting for writer capacity: 5 of 5 writer slots occupied",
      recovery: "Wait for a registered writer to finish or release its slot", firstSeen: hourAgo, notifyOwner: false },
    { kind: "captain_idle_with_work", itemId: "item-overdue", executionId: "overdue",
      reason: "Dispatch overdue: runnable for 30 min; 1 of 5 writer slots occupied",
      recovery: "A Captain launches it", firstSeen: hourAgo, notifyOwner: false },
    { kind: "head_drift", executionId: "run", reason: "Head moved", recovery: "Reconcile", firstSeen: now, notifyOwner: false },
    { kind: "no_live_captain", reason: "No Captain heartbeat", recovery: "Start a Captain", firstSeen: now, notifyOwner: true },
    { kind: "captain_session", captainId: "cap-ended", reason: "Ended session", recovery: "Review handoff", firstSeen: now, notifyOwner: false }
  ];
  const captains = [
    { id: "cap-ended", state: "ended", live: false, stale: false, heartbeatAt: hourAgo, compactionCount: 2 },
    { id: "cap-stale", sessionRef: codexUrl, state: "active", live: false, stale: true, heartbeatAt: hourAgo },
    { id: "cap-1", sessionRef: copilotUrl, state: "active", live: true, stale: false, heartbeatAt: now,
      wakeCount: 3, compactionCount: 0, charterRevision: "abcdef1234", focus: ["item-run"] }
  ];
  let offline = false;
  let pending;
  let pendingSignal;
  const calls = [];
  const openedReports = [];
  const usageTokens = { input: 123, output: 0, cache_read: null, cache_write: null, reasoning: null, total: null };
  const usageSummary = { tokens: usageTokens, field_sessions: { input: 1, output: 1, cache_read: 0,
    cache_write: 0, reasoning: 0, total: 0 }, covered_sessions: 1, bound_sessions: 2, status: "partial" };
  const usageFixture = {
    execution: usageSummary, item: usageSummary,
    overhead: "Captain/shared reviewer usage unallocated; not included",
    capabilities: { "codex-app": "Automatic App-bound export unverified" },
    sessions: [{
      execution_id: "run", runtime: "copilot-app", session_id: nativeId, status: "pending_final",
      reason: "<img src=x> Final native turn not observed",
      snapshot: { provider: "github-copilot", model: null, source: "app_session_metadata",
        source_version: "fixture-v1", origin: "local", window_start: hourAgo, cutoff: now,
        observed_at: now, scope: "item_exclusive", tokens: usageTokens,
        credits: { amount: "1.25", unit: "provider credit", basis: "estimated", source: "native UI" },
        money: null }
    }]
  };
  const detailData = (item, cursor) => ({
    item, attempt: "att2",
    usage: item.id === "run" ? usageFixture : undefined,
    question: item.id === "wait" ? { prompt: "Which target?", options: ["staging", "prod"], proposedDefault: "staging" } : null,
    dependencies: [{ title: "Build", milestone: "verified", status: "satisfied" }],
    events: [{
      at: now, event: "result", fromStage: cursor ? "development" : "self_review",
      stage: cursor ? "self_review" : "development", status: "queued", reason: "Review loop"
    }],
    nextCursor: cursor ? null : "older-events"
  });
  let releaseOpen;
  const firstOpen = new Promise(resolve => { releaseOpen = resolve; });
  let openReady = false;

  const source = {
    openReport(output) { openedReports.push(output); },
    concurrencyControl: document.querySelector("#operator-settings"),
    async list({ kind, cursor, limit }) {
      calls.push([kind, cursor, limit]);
      if (offline) throw new Error("API offline");
      if (kind === "open") {
        if (!openReady) await firstOpen;
        return { items: cursor ? [queued] : [waiting, running, queued, external, resumed], nextCursor: cursor ? null : "more-open" };
      }
      return { items: cursor ? [] : [success, observed, ready, short, failure, draft, cancelled, report], nextCursor: cursor ? null : "older-closed" };
    },
    async flags() {
      if (offline) throw new Error("API offline");
      return { flags };
    },
    async captains() {
      if (offline) throw new Error("API offline");
      return { captains };
    },
    async detail({ id, cursor, limit, signal }) {
      calls.push([id, cursor, limit]);
      if (offline) throw new Error("API offline");
      if (id === "wait") {
        pendingSignal = signal;
        return new Promise(resolve => { pending = resolve; });
      }
      return detailData([running, success, observed, ready, short, failure, draft].find(row => row.id === id), cursor);
    }
  };
  let releaseOverview;
  const firstOverview = new Promise(resolve => { releaseOverview = resolve; });
  let overviewReady = false;
  source.overview = async () => {
    if (offline) throw new Error("API offline");
    if (!overviewReady) await firstOverview;
    return { sections: { needs_you: 10, running: 200, queued: 9390, blocked: 0, waiting_on_checks: 400 } };
  };

  const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const button = name => root.querySelector('button[aria-label="' + name + '"]');
  const item = id => root.querySelector('button[data-item="' + id + '"]');
  const section = id => root.querySelector(".sec-" + id);
  const status = () => root.querySelector('[role="status"]').textContent;
  const refresh = () => root.querySelector('[data-action="refresh"]').click();
  const contrastEvidence = [];
  function checkPalette() {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const luminance = color => {
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      const rgb = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3)
        .map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
      return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
    };
    const ratio = (a, b) => {
      const x = luminance(a), y = luminance(b);
      return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
    };
    const background = node => {
      for (let parent = node; parent; parent = parent.parentElement) {
        const color = getComputedStyle(parent).backgroundColor;
        if (color !== "rgba(0, 0, 0, 0)" && color !== "transparent") return color;
      }
      throw new Error("Missing actual icon surface");
    };
    const priorTheme = document.documentElement.dataset.theme;
    const surface = document.createElement("span");
    root.append(surface);
    for (const theme of ["light", "dark"]) {
      document.documentElement.dataset.theme = theme;
      const colors = selector => getComputedStyle(root.querySelector(selector)).color;
      assert(colors(".captains .t-run .mark-captain") !== colors(".mark-run"), theme + ": Captain is not Active blue");
      surface.style.color = theme === "light" ? "#886000" : "#ecc46d";
      assert(colors(".captains .t-run .mark-captain") === getComputedStyle(surface).color, theme + ": approved gold reaches the live helmet");
      surface.style.color = theme === "light" ? "#1d5fbf" : "#79b8ff";
      assert(colors(".mark-run") === getComputedStyle(surface).color, theme + ": approved blue reaches Active spears");
      assert(colors(".glyph.t-wait") === colors(".glyph.t-idle") &&
        colors(".glyph.t-wait") !== colors(".glyph.t-attn"), theme + ": automatic waits are quiet, not attention");
      assert(colors(".glyph.t-handoff") !== colors(".glyph.t-ok") &&
        colors(".glyph.t-fail") !== colors(".glyph.t-attn"), theme + ": handoff/success and failure/attention differ");
      let iconMinimum = Infinity, textMinimum = Infinity;
      for (const token of ["--bg", "--surface", "--raised", "--hover"]) {
        surface.style.background = "var(" + token + ")";
        const bg = getComputedStyle(surface).backgroundColor;
        for (const text of ["--faint", "--accent", "--captain"]) {
          surface.style.color = "var(" + text + ")";
          assert(ratio(getComputedStyle(surface).color, bg) >= 4.5,
            theme + ": secondary text and every brand glint stop meet AA on " + token);
        }
        surface.style.color = "var(--line-strong)";
        assert(ratio(getComputedStyle(surface).color, bg) >= 3,
          theme + ": control boundaries meet non-text AA on " + token);
      }
      for (const glyph of root.querySelectorAll(".rows .glyph")) {
        const icon = glyph.querySelector("svg") || glyph;
        const color = getComputedStyle(icon).color;
        const adjacent = [background(glyph)];
        for (const token of ["--bg", "--surface", "--raised", "--hover"]) {
          surface.style.background = "var(" + token + ")";
          adjacent.push(getComputedStyle(surface).backgroundColor);
        }
        for (const bg of adjacent) {
          const value = ratio(color, bg);
          iconMinimum = Math.min(iconMinimum, value);
          assert(value >= 3, theme + ": " + glyph.title + " icon contrast " + value.toFixed(2));
        }
      }
      for (const node of root.querySelectorAll(".stage, .stat, .repo, .writer, .reason, .chip, .faint, .count, .counts")) {
        if (!node.getClientRects().length || !node.textContent.trim()) continue;
        const value = ratio(getComputedStyle(node).color, background(node));
        textMinimum = Math.min(textMinimum, value);
        assert(value >= 4.5, theme + ": text contrast " + value.toFixed(2) + " for " + node.className);
      }
      contrastEvidence.push({ theme, iconMinimum, textMinimum });
    }
    surface.remove();
    if (priorTheme) document.documentElement.dataset.theme = priorTheme;
    else delete document.documentElement.dataset.theme;
  }
  const addListener = EventTarget.prototype.addEventListener;
  const removeListener = EventTarget.prototype.removeEventListener;
  let rowClickRegistrations = 0;
  let rowClickRemovals = 0;
  let perRowRegistrations = 0;
  EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (this === root && type === "click") rowClickRegistrations++;
    if (type === "click" && this.matches?.(".line, button.title, button[data-report]")) perRowRegistrations++;
    return addListener.call(this, type, listener, options);
  };
  EventTarget.prototype.removeEventListener = function(type, listener, options) {
    if (this === root && type === "click") rowClickRemovals++;
    return removeListener.call(this, type, listener, options);
  };
  let unmount = window.PhalanxOperatorBoard.mount(root, source);

  try {
    await tick();
    assert(root.querySelector(".health").textContent.includes("Connecting") &&
      !root.querySelector(".h-live"), "Live waits for every initial read, including overview");
    overviewReady = true;
    releaseOverview();
    await tick();
    assert(!root.querySelector(".h-live"), "A delayed initial open read cannot show Live");
    openReady = true;
    releaseOpen();
    await tick();
    assert(root.querySelector(".h-live"), "Live appears after all enabled reads succeed");
    assert(root.querySelector(".counts").textContent.includes("200 running") &&
      root.querySelector(".counts").textContent.includes("9790 queued and waiting"),
    "Home counts cover the entire queue, not the loaded page");
    assert(item("run").closest("li").querySelector(".reason").textContent === running.nextAction,
      "Running rows retain the server's next action");
    const target = window.PhalanxOperatorBoard.sessionTarget;
    assert(target({ runtime: "copilot-app", sessionId: nativeId }).url === copilotUrl &&
      target({ runtime: "codex", sessionId: nativeId }).url === codexUrl, "Known native IDs route only to their host");
    for (const reference of [nativeId, "javascript:alert(1)", "file:///tmp/chat", "unknown://sessions/" + nativeId,
      "ghapp://sessions/" + nativeId + "?run=1", "ghapp://sessions/" + nativeId + "#x",
      "ghapp://sessions/../" + nativeId, "ghapp://user@sessions/" + nativeId,
      "ghapp://sessions/" + nativeId + "/", "ghapp://sessions/%31" + nativeId,
      " ghapp://sessions/" + nativeId, "https://user:pass@example.com/chat", "https://[bad",
      "https://example.com/\\chat", "/operator/new", "codex://threads/not-a-thread"]) {
      assert(!target({ runtime: "copilot-app", sessionId: nativeId, sessionUrl: reference }).url,
        "Unsafe or malformed recorded link is not replaced: " + reference);
    }
    assert(!target({ sessionId: nativeId }).url && !target({ sessionRef: nativeId }).url &&
      !target({ runtime: "copilot-cli", sessionId: nativeId }).url, "Legacy identity never guesses its provider");
    assert(!target({ runtime: "copilot-app", sessionUrl: codexUrl }).url &&
      !target({ sessionUrl: copilotUrl, sessionId: "other" }).url, "Provider and identity conflicts never translate sessions");
    for (const availability of ["missing", "deleted"]) {
      assert(!target({ sessionUrl: copilotUrl, sessionAvailability: availability }).url &&
        target({ sessionUrl: copilotUrl, sessionAvailability: availability }).reason.includes(availability),
      "Known " + availability + " session is explicitly unavailable");
    }
    assert(target({ sessionUrl: copilotUrl }).reason.includes("unverified"), "Recorded URL does not claim verified existence");
    const captainLinks = root.querySelectorAll('a[href="' + copilotUrl + '"]');
    assert(captainLinks.length >= 3 && [...captainLinks].every(link => link.tabIndex === 0),
      "Helmet, Captain name and writer are native keyboard-accessible links");
    const helmetLink = [...captainLinks].find(link => link.querySelector(".mark-captain"));
    helmetLink.focus();
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    helmetLink.dispatchEvent(enter);
    assert(document.activeElement === helmetLink && !enter.defaultPrevented,
      "Keyboard activation retains native browser external-app confirmation");
    assert(item("run").closest("li").querySelector('a.writer[href="' + copilotUrl + '"]') &&
      item("resumed").closest(".sec-queued").querySelector('a.writer[href="' + codexUrl + '"]') &&
      item("done").closest("li").querySelector('a.writer[href="' + codexUrl + '"]') &&
      !item("queue").closest("li").querySelector("a.writer"), "Active, resumed and closed sessions link; unlaunched queue stays unlinked");
    const concurrency = source.concurrencyControl;
    const settingsSummary = concurrency.querySelector("summary");
    const settingsInput = concurrency.querySelector("input");
    assert(root.querySelector(".actions #operator-settings") === concurrency &&
      settingsSummary.getAttribute("aria-label") === "Writer concurrency settings",
    "Toolbar reparents the canonical settings node instead of duplicating settings");
    settingsSummary.click();
    await tick();
    assert(concurrency.open && document.activeElement === settingsInput, "Settings disclosure focuses its existing input");
    assert(concurrency.querySelector("p").getBoundingClientRect().bottom <=
      concurrency.querySelector("form").getBoundingClientRect().top, "Canonical settings body stays in one non-overlapping panel");
    settingsInput.value = "7";
    root.querySelector('[data-action="search"]').click();
    assert(!concurrency.open, "Opening search closes canonical settings");
    settingsSummary.click();
    await tick();
    assert(root.querySelector("#board-search").hidden && settingsInput.value === "7",
      "Settings closes search and retains its canonical form state");
    settingsInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert(!concurrency.open && document.activeElement === settingsSummary, "Settings Escape restores summary focus");
    const lists = calls.filter(call => call[0] === "open" || call[0] === "closed");
    assert(lists.length === 2 && lists.every(call => call[2] > 0 && call[2] <= 100), "Initial reads are bounded lists");
    assert(section("needs").contains(item("wait")) && section("needs").textContent.includes(waiting.waitReason), "Input wait needs the owner");
    assert(section("needs").textContent.includes("no live captain"), "Owner-notify flag without an item needs the owner");
    assert(section("active").contains(item("run")) && section("queued").contains(item("queue")), "Active and queued split");
    assert(item("queue").closest("li").textContent.includes("draft only"), "Open draft-only policy is visible");
    const backlogGroup = section("flags").querySelector('[data-kind="queue_backlogged"]');
    assert(backlogGroup.classList.contains("t-info") && backlogGroup.textContent.includes("Queue backlogged") &&
      backlogGroup.textContent.includes("5 of 5"), "Full-capacity backlog is visible and informational");
    assert(backlogGroup.querySelector(".sr-only").textContent === "information" &&
      !backlogGroup.querySelector(".pennant"), "Normal backlog has a labeled, non-alarm information marker");
    assert(backlogGroup.querySelector('a[href="/operator/executions/queue"]') &&
      backlogGroup.querySelector(".age time"), "Backlog retains its execution link and age");
    assert(item("queue").closest("li").querySelector(".chip.c-info"), "Backlog row chip uses restrained information styling");
    const overdueGroup = section("flags").querySelector('[data-kind="captain_idle_with_work"]');
    assert(overdueGroup.classList.contains("t-wait") && overdueGroup.textContent.includes("Dispatch overdue"),
      "Legacy overdue flag remains compatible and gets an actionable display label");
    const savedFlags = flags.slice();
    const capacityFlag = flags.find(flag => flag.kind === "queue_backlogged");
    flags.splice(0, flags.length, capacityFlag);
    refresh();
    await tick();
    assert(!section("flags").querySelector("h2 .t-wait") &&
      [...root.querySelectorAll(".counts li")].find(node => node.textContent.includes(" flags")).className === "",
      "Backlog alone does not turn the section or summary count into a warning");
    flags[0] = savedFlags.find(flag => flag.kind === "deployed");
    refresh();
    await tick();
    assert(!section("flags").querySelector("h2 .t-wait") &&
      [...root.querySelectorAll(".counts li")].find(node => node.textContent.includes(" flags")).className === "" &&
      section("flags").querySelector(".fgroup.t-ok .mark-flag"),
      "Successful deployment flags retain their shape without warning severity");
    flags[0] = { ...capacityFlag, kind: "captain_idle_with_work", reason: "Dispatch overdue: capacity released" };
    refresh();
    await tick();
    assert(item("queue").closest("li").querySelector(".chip.c-warn") &&
      section("flags").textContent.includes("Dispatch overdue"), "Slot release refresh replaces information with overdue attention");
    flags.splice(0, flags.length, ...savedFlags);
    refresh();
    await tick();
    assert(item("draft").closest("li").textContent.includes("draft published"), "Draft delivery is not labeled ready");
    assert(item("run").closest("li").textContent.includes("head drift"), "Row carries its flag chip");
    assert(rowClickRegistrations === 1 && perRowRegistrations === 0,
      "Row clicks use one mount listener, not new listeners on rebuilt rows");
    assert(item("run").closest("li").textContent.includes("unconfirmed"), "Launching writer reads as unconfirmed");
    const captainLine = () => section("captains").querySelector(".captains li");
    assert(captainLine().textContent.includes("wakes 3") && captainLine().textContent.includes("compactions 0") &&
      captainLine().textContent.includes("focus:") && !section("captains").textContent.includes("abcdef1"),
      "Captain line labels native zero and retains wakes/focus without charter hash");
    assert(section("captains").textContent.includes("compactions unavailable"), "Missing runtime telemetry is not zero");
    const ended = () => section("captains").querySelector(".ended-captains");
    assert(!ended().open && ended().querySelector("summary").textContent === "Ended (1)", "Ended Captains collapsed by default");
    assert(ended().textContent.includes("compactions 2"), "Ended Captain retains session count");
    captains[2].compactionCount = 1;
    refresh();
    await tick();
    assert(captainLine().textContent.includes("compactions 1"), "Compaction telemetry refreshes on normal polling");
    assert(section("captains").querySelector(".captains li").textContent.includes("cap-1"), "Live Captains sort first");
    assert(/heartbeat \d+s ago/.test(section("captains").textContent), "Heartbeat age has context and ago");
    assert(section("captains").querySelector("time").getAttribute("aria-label").includes("T"), "Absolute time accessible");
    const formation = item("run").closest("li").querySelector(".mark-run");
    assert(formation?.querySelectorAll(".spear").length === 3 &&
      formation.getAttribute("aria-hidden") === "true" && formation.getAttribute("focusable") === "false" &&
      formation.parentElement.title === "running" &&
      formation.parentElement.querySelector(".sr-only").textContent === "running" &&
      !section("captains").querySelector(".mark-run"),
    "Running work has three spears, distinct from Captains, with unchanged accessible state");
    for (const [health, word] of [["run", "live"], ["attn", "stale"], ["muted", "ended"]]) {
      const row = section("captains").querySelector(".row.t-" + health);
      const glyph = row.querySelector(".glyph");
      const helmet = glyph.querySelector("svg.mark-captain");
      assert(helmet && helmet.getAttribute("aria-hidden") === "true" &&
        helmet.getAttribute("viewBox") === "0 0 64 64" &&
        [...helmet.querySelectorAll("path")].map(path => path.getAttribute("d")).join(";") ===
          "M7 29C4 15 15 3 31 3C43 3 52 9 57 17L50 22C43 14 35 12 28 15C21 18 19 23 19 29Z;" +
          "M17 33C17 23 24 17 33 17C44 17 50 24 50 34V36H37V43L46 47L41 61L24 52L29 37H16Z;" +
          "M14 37H23L18 52H9Z" &&
        [...helmet.querySelectorAll("path")].every(path => path.getAttribute("fill") === "currentColor") &&
        helmet.getAttribute("focusable") === "false" &&
        glyph.classList.contains("t-" + health) && glyph.title === word &&
        glyph.querySelector(".sr-only").textContent === word,
      "Captain helmet preserves " + word + " tone and accessible state text");
    }
    assert(item("queue").closest("li").querySelector(".mark-idle") &&
      item("external").closest("li").querySelector(".mark-wait"), "Queued and waiting have distinct shields");
    for (const id of ["ready", "short", "draft"]) {
      const glyph = item(id).closest("li").querySelector(".glyph");
      assert(glyph.querySelector(".mark-handoff") && glyph.querySelector(".sr-only").textContent ===
        (id === "draft" ? "draft published" : "ready for check in"), "Unmerged handoff keeps its outcome label: " + id);
    }
    for (const id of ["done", "observed", "report"]) {
      assert(item(id).closest("li").querySelector(".glyph.t-ok")?.textContent.includes("✓"),
        "Verified success retains the check mark: " + id);
    }
    assert(item("cancelled").closest("li").querySelector(".glyph.t-muted"), "Cancelled stays muted");
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const spears = [...formation.querySelectorAll(".spear")].map(node => getComputedStyle(node));
    assert(spears.every(style => style.animationName === (reduced ? "none" : "formation")),
      "Active formation respects the actual reduced-motion preference");
    if (!reduced) {
      assert(spears.every(style => style.animationDuration === "2.2s") &&
        Math.abs(parseFloat(spears[1].animationDelay) - parseFloat(spears[0].animationDelay) - .44) < .001 &&
        Math.abs(parseFloat(spears[2].animationDelay) - parseFloat(spears[0].animationDelay) - .22) < .001,
      "Existing restrained wave retains its duration and left/center/right stagger");
    }
    assert(Math.abs(formation.getBoundingClientRect().width - 12) < .05 &&
      Math.abs(formation.getBoundingClientRect().height - 16) < .05,
      "Active keeps its normal dashboard size");
    checkPalette();
    assert(document.documentElement.scrollWidth <= innerWidth + 1, "Integrated icon rows do not widen the viewport");
    result.dataset.contrast = JSON.stringify(contrastEvidence);
    result.dataset.motion = reduced ? "reduced" : "normal";
    result.dataset.width = innerWidth;
    assert(root.querySelector(".fgroup .mark-notify") && root.querySelector(".fgroup .mark-flag") &&
      item("run").closest("li").querySelector(".chip .mark-flag"), "Red flags on both group kinds and chips");
    for (const id of ["done", "observed", "ready", "report"]) {
      assert(!item(id).closest("li").querySelector(".line > .reason").textContent.includes("wanted:"), "Met target has no shortfall: " + id);
    }
    assert(item("observed").closest("li").querySelector(".stage").textContent === "Merged", "Observed merged PR overrides ready outcome");
    assert(item("short").closest("li").querySelector(".reason").textContent.endsWith("wanted: merged"), "Ready shortfall visible");
    assert(item("fail").closest("li").querySelector(".reason").textContent === "wanted: merged", "Failed shortfall visible");
    assert(item("cancelled").closest("li").querySelector(".reason").textContent === "wanted: merged", "Cancelled shortfall visible");
    assert(root.querySelector(".fgroup .captain-ref.captain-ended .t-muted .mark-captain") &&
      root.querySelector(".fgroup .captain-ref.captain-ended").textContent.includes("ended"),
    "Flag Captain reference uses a muted helmet and distinguishes ended");
    assert(/closed \d+s ago/.test(item("done").closest("li").querySelector(".age").textContent) &&
      item("done").closest("li").querySelector(".dur").textContent.startsWith("took 1h"), "Closed times explain age and duration");
    ended().open = true;
    refresh();
    await tick();
    assert(ended().open, "Ended expansion survives refresh");
    assert(item("wait").textContent === waiting.title && !root.querySelector("img"), "Untrusted text stays text");
    assert(!root.querySelector('a[href^="javascript:"]'), "Unsafe link omitted");
    assert(root.querySelector('a[href="' + success.output.url + '"]')?.textContent === "#1", "Closed PR link");
    assert(item("done").closest("li").querySelector(".chip.c-good > span:last-child")?.textContent === "deployed",
           "Successful post-merge deployment is a non-warning chip on the closed row");
    item("wait").click();
    item("run").click();
    await tick();
    assert(pendingSignal.aborted, "Replaced detail aborted");
    assert(item("run").getAttribute("aria-expanded") === "true" && item("wait").getAttribute("aria-expanded") === "false", "One row expanded");
    pending(detailData(waiting, null));
    await tick();
    const detail = () => root.querySelector("#detail-run");
    assert(detail() && detail().previousElementSibling.contains(item("run")), "Detail opens in place under its row");
    const usageText = detail().textContent;
    for (const text of ["Observed usage", "Current execution writer subtotal", "All executions item writer subtotal",
      "input tokens: 123", "output tokens: 0", "cache write tokens: unavailable", "1/2 bound sessions",
      "model unavailable", "Window ", "observed ", "1.25 provider credit (estimated", "money: unavailable",
      "Captain/shared reviewer usage unallocated", "App-bound export unverified", "Final native turn not observed"]) {
      assert(usageText.includes(text), "Observed usage detail renders " + text);
    }
    assert(!detail().querySelector("img"), "Usage provenance stays text, never HTML");
    const history = detail().querySelector(".timeline").textContent;
    assert(history.includes("self review") && history.includes("development") &&
      detail().textContent.includes("Prerequisites"), "Stage history and dependencies");
    assert(detail().textContent.includes("att2") && detail().textContent.includes("opaque-worker-7"), "Attempt and legacy worker");
    assert(detail().querySelector(".captain-ref .t-run .mark-captain") && ended().open,
      "Captain reference uses a live helmet and Ended state is retained in details");
    running.writer.captainId = "cap-stale";
    refresh();
    await tick();
    assert(detail().querySelector(".captain-ref .t-attn .mark-captain") &&
      detail().querySelector(".captain-ref").textContent.includes("stale"),
    "Stale Captain reference keeps its helmet and explicit state");
    running.writer.captainId = "cap-unlisted";
    refresh();
    await tick();
    assert(detail().querySelector(".captain-ref .t-muted .mark-captain") &&
      detail().querySelector(".captain-ref").textContent.includes("status unavailable"),
    "Unlisted Captain reference keeps its helmet and unavailable state");
    running.writer.captainId = "cap-1";
    assert(detail().textContent.includes("Merged") && detail().querySelector(".chip .mark-flag"), "Mapped target and flag in details");
    assert(!detail().textContent.includes("Which target?"), "Late detail ignored");
    button("Next history page").click();
    button("Next open work page").click();
    button("Next recent outcomes page").click();
    await tick();
    assert(["older-events", "more-open", "older-closed"].every(cursor => calls.some(call => call[1] === cursor)), "All views page with cursors");
    button("Previous open work page").click();
    button("Previous recent outcomes page").click();
    await tick();
    assert(item("wait") && item("fail"), "Previous pages restore their cursor");
    item("run").focus();
    refresh();
    await tick();
    assert(document.activeElement === item("run"), "Refresh restores focus to the work item");
    item("run").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    assert(document.activeElement !== item("run") && document.activeElement.matches("button.title"), "Arrow keys move between rows");
    item("run").focus();
    item("run").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert(!detail() && document.activeElement === item("run"), "Escape collapses and keeps focus");
    item("run").closest(".line").querySelector(".stage").click();
    await tick();
    assert(detail(), "Nested non-control row cells delegate to the row toggle");
    item("run").closest(".line").querySelector(".stage").click();
    assert(!detail(), "Delegated row cells toggle exactly once");
    const linkClick = new MouseEvent("click", { bubbles: true, cancelable: true });
    const prLink = item("run").closest(".line").querySelector("a.pr");
    prLink.addEventListener("click", event => event.preventDefault(), { once: true });
    prLink.dispatchEvent(linkClick);
    assert(!detail(), "Native row links do not toggle details");
    item("fail").closest("li").querySelector("[data-report]").click();
    assert(openedReports.length === 1 && openedReports[0] === failure.output &&
      item("fail").getAttribute("aria-expanded") === "false",
    "Delegated report button opens the current output without toggling its row");
    item("done").click();
    await tick();
    const done = root.querySelector("#detail-done")?.textContent || "";
    assert(done.includes("Merged on GitHub") && done.includes("squash or rebase · a40536e00000"),
           "GitHub delivery shown apart from the Phalanx outcome");
    item("done").click();
    for (const kind of ["deployment_pending", "deployment_failed"]) {
      flags[0] = { ...flags[0], kind };
      refresh();
      await tick();
      assert(item("done").closest("li").querySelector(".chip.c-warn > span:last-child")?.textContent === kind.replaceAll("_", " "),
             "Deployment refresh updates the closed row without changing its outcome");
      assert(item("done").closest("li").textContent.includes("Merged"), "Deployment is separate from check-in");
    }
    offline = true;
    refresh();
    await tick();
    assert(root.textContent.includes("Disconnected") && status().includes("stale"), "Disconnection and staleness shown");
    assert(item("fail") && item("run"), "Cached rows remain visible");
    offline = false;
    refresh();
    await tick();
    assert(status() === "Connected." && !root.textContent.includes("Disconnected"), "Refresh recovers");
    assert(rowClickRegistrations === 1 && perRowRegistrations === 0,
      "Renders and refreshes never register row listeners");
    unmount();
    assert(rowClickRemovals === 1, "Unmount removes the delegated row listener");
    unmount = window.PhalanxOperatorBoard.mount(root, source);
    assert(rowClickRegistrations === 2, "Remount registers one fresh row listener");
    settingsSummary.click();
    await tick();
    assert(concurrency.open, "Reusing canonical settings after unmount does not duplicate toggle handlers");
    concurrency.open = false;
    item("run").click();
    await tick();
    assert(item("run").getAttribute("aria-expanded") === "true",
      "Remounted row toggles once without an old delegated listener");
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL – " + error.message;
    result.dataset.status = "failed";
  } finally {
    EventTarget.prototype.addEventListener = addListener;
    EventTarget.prototype.removeEventListener = removeListener;
    const preview = new URLSearchParams(location.search);
    if (!preview.has("preview")) unmount();
    else {
      document.documentElement.dataset.theme = preview.get("theme") || "light";
      document.documentElement.dataset.viewport = innerWidth;
      const ended = root.querySelector(".ended-captains");
      if (ended) ended.open = preview.get("ended") === "open";
    }
  }
})();
