/* Open operator-board-fixture.html in a browser. Sample data lives only here. */
(async () => {
  const root = document.querySelector("main");
  const result = document.querySelector("#result");
  const now = new Date().toISOString();
  const hourAgo = new Date(Date.now() - 3600000).toISOString();
  const waiting = {
    id: "wait", itemId: "item-wait", title: "<img src=x> Choose deployment target",
    goal: "<img src=x> Choose deployment target.\nTwo targets match.", repo: "example/service",
    stage: "development", status: "waiting_for_input", lastAdvancedAt: hourAgo, createdAt: hourAgo,
    waitReason: "Two targets match the work request.", primaryPrUrl: "javascript:alert(1)"
  };
  const running = {
    id: "run", itemId: "item-run", title: "Update telemetry", goal: "Update telemetry", target: "checked_in",
    repo: "example/service", stage: "self_review", status: "running", lastAdvancedAt: now, createdAt: hourAgo,
    primaryPrUrl: "https://example.com/pull/7", recheckAt: Date.now() + 60000,
    writer: { state: "launching", runtime: "copilot-app", captainId: "cap-1" },
    historicalWorker: { workerRef: "opaque-worker-7" }
  };
  const queued = {
    id: "queue", itemId: "item-queue", title: "Tidy docs", repo: "example/docs",
    stage: "development", status: "queued", lastAdvancedAt: hourAgo, publicationMode: "draft"
  };
  const external = { ...queued, id: "external", title: "Await CI", status: "waiting_for_external" };
  const failure = {
    id: "fail", title: "Audit build", repo: "example/service", stage: "report_drafting",
    status: "failed", outcome: "failed", target: "checked_in", closedAt: now, createdAt: hourAgo,
    output: { kind: "report", url: "https://example.com/reports/build" }
  };
  const success = {
    ...failure, id: "done", title: "Verify rollout", status: "completed", outcome: "checked_in",
    output: { kind: "pr", url: "https://example.com/pull/1" },
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
    { id: "cap-stale", state: "active", live: false, stale: true, heartbeatAt: hourAgo },
    { id: "cap-1", sessionRef: "s", state: "active", live: true, stale: false, heartbeatAt: now,
      wakeCount: 3, compactionCount: 0, charterRevision: "abcdef1234", focus: ["item-run"] }
  ];
  let offline = false;
  let pending;
  let pendingSignal;
  const calls = [];
  const detailData = (item, cursor) => ({
    item, attempt: "att2",
    question: item.id === "wait" ? { prompt: "Which target?", options: ["staging", "prod"], proposedDefault: "staging" } : null,
    dependencies: [{ title: "Build", milestone: "verified", status: "satisfied" }],
    events: [{
      at: now, event: "result", fromStage: cursor ? "development" : "self_review",
      stage: cursor ? "self_review" : "development", status: "queued", reason: "Review loop"
    }],
    nextCursor: cursor ? null : "older-events"
  });

  const source = {
    concurrencyControl: document.querySelector("#operator-settings"),
    async list({ kind, cursor, limit }) {
      calls.push([kind, cursor, limit]);
      if (offline) throw new Error("API offline");
      if (kind === "open") {
        return { items: cursor ? [queued] : [waiting, running, queued, external], nextCursor: cursor ? null : "more-open" };
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

  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const button = name => root.querySelector('button[aria-label="' + name + '"]');
  const item = id => root.querySelector('button[data-item="' + id + '"]');
  const section = id => root.querySelector(".sec-" + id);
  const status = () => root.querySelector('[role="status"]').textContent;
  const refresh = () => root.querySelector('[data-action="refresh"]').click();
  let unmount = window.PhalanxOperatorBoard.mount(root, source);

  try {
    await tick();
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
    assert(item("run").closest("li").querySelector(".mark-run") &&
      !section("captains").querySelector(".mark-run"), "Running work retains its spearhead, distinct from Captains");
    for (const [health, word] of [["run", "live"], ["attn", "stale"], ["muted", "ended"]]) {
      const row = section("captains").querySelector(".row.t-" + health);
      const glyph = row.querySelector(".glyph");
      const helmet = glyph.querySelector("svg.mark-captain");
      assert(helmet && helmet.getAttribute("aria-hidden") === "true" &&
        helmet.getAttribute("focusable") === "false" &&
        glyph.classList.contains("t-" + health) && glyph.title === word &&
        glyph.querySelector(".sr-only").textContent === word,
      "Captain helmet preserves " + word + " tone and accessible state text");
    }
    assert(item("queue").closest("li").querySelector(".mark-idle") &&
      item("external").closest("li").querySelector(".mark-wait"), "Queued and waiting have distinct shields");
    assert(root.querySelector(".fgroup .mark-notify") && root.querySelector(".fgroup .mark-flag") &&
      item("run").closest("li").querySelector(".chip .mark-flag"), "Red flags on both group kinds and chips");
    for (const id of ["done", "observed", "ready", "report"]) {
      assert(!item(id).closest("li").querySelector(".line > .reason").textContent, "Met target has no shortfall: " + id);
    }
    assert(item("observed").closest("li").querySelector(".stage").textContent === "Merged", "Observed merged PR overrides ready outcome");
    assert(item("short").closest("li").querySelector(".reason").textContent === "wanted: merged", "Ready shortfall visible");
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
    unmount();
    unmount = window.PhalanxOperatorBoard.mount(root, source);
    settingsSummary.click();
    await tick();
    assert(concurrency.open, "Reusing canonical settings after unmount does not duplicate toggle handlers");
    concurrency.open = false;
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL – " + error.message;
    result.dataset.status = "failed";
  } finally {
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
