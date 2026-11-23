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
    id: "run", itemId: "item-run", title: "Update telemetry", goal: "Update telemetry", target: "merged",
    repo: "example/service", stage: "pr_revision", status: "running", lastAdvancedAt: now, createdAt: hourAgo,
    primaryPrUrl: "https://example.com/pull/7", recheckAt: Date.now() + 60000,
    writer: { state: "launching", runtime: "copilot-app", captainId: "cap-1" },
    legacy: { workerRef: "opaque-worker-7" }
  };
  const queued = {
    id: "queue", itemId: "item-queue", title: "Tidy docs", repo: "example/docs",
    stage: "development", status: "queued", lastAdvancedAt: hourAgo
  };
  const failure = {
    id: "fail", title: "Audit build", repo: "example/service", stage: "report_drafting",
    status: "failed", outcome: "failed", closedAt: now, createdAt: hourAgo,
    output: { kind: "report", url: "https://example.com/reports/build" }
  };
  const success = {
    ...failure, id: "done", title: "Verify rollout", status: "completed", outcome: "checked_in",
    output: { kind: "pr", url: "https://example.com/pull/1" }
  };
  const flags = [
    { kind: "head_drift", executionId: "run", reason: "Head moved", recovery: "Reconcile", firstSeen: now, notifyOwner: false },
    { kind: "no_live_captain", reason: "No Captain heartbeat", recovery: "Start a Captain", firstSeen: now, notifyOwner: true }
  ];
  const captains = [{ id: "cap-1", sessionRef: "s", state: "busy", live: true, stale: false, heartbeatAt: now,
    wakeCount: 3, charterRevision: "abcdef1234", focus: ["item-run"] }];
  let offline = false;
  let pending;
  let pendingSignal;
  const calls = [];
  const detailData = (item, cursor) => ({
    item, attempt: "att2",
    question: item.id === "wait" ? { prompt: "Which target?", options: ["staging", "prod"], proposedDefault: "staging" } : null,
    dependencies: [{ title: "Build", milestone: "verified", status: "satisfied" }],
    events: [{
      at: now, event: "result", fromStage: cursor ? "development" : "pr_revision",
      stage: cursor ? "pr_revision" : "development", status: "queued", reason: "Review loop"
    }],
    nextCursor: cursor ? null : "older-events"
  });

  const source = {
    async list({ kind, cursor, limit }) {
      calls.push([kind, cursor, limit]);
      if (offline) throw new Error("API offline");
      if (kind === "open") {
        return { items: cursor ? [queued] : [waiting, running, queued], nextCursor: cursor ? null : "more-open" };
      }
      return { items: cursor ? [] : [success, failure], nextCursor: cursor ? null : "older-closed" };
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
      return detailData(id === "run" ? running : failure, cursor);
    }
  };

  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const button = name => root.querySelector('button[aria-label="' + name + '"]');
  const item = id => root.querySelector('button[data-item="' + id + '"]');
  const section = id => root.querySelector(".sec-" + id);
  const status = () => root.querySelector('[role="status"]').textContent;
  const refresh = () => root.querySelector('[data-action="refresh"]').click();
  const unmount = window.PhalanxOperatorBoard.mount(root, source);

  try {
    await tick();
    const lists = calls.filter(call => call[0] === "open" || call[0] === "closed");
    assert(lists.length === 2 && lists.every(call => call[2] > 0 && call[2] <= 100), "Initial reads are bounded lists");
    assert(section("needs").contains(item("wait")) && section("needs").textContent.includes(waiting.waitReason), "Input wait needs the owner");
    assert(section("needs").textContent.includes("no live captain"), "Owner-notify flag without an item needs the owner");
    assert(section("active").contains(item("run")) && section("queued").contains(item("queue")), "Active and queued split");
    assert(item("run").closest("li").textContent.includes("head drift"), "Row carries its flag chip");
    assert(item("run").closest("li").textContent.includes("unconfirmed"), "Launching writer reads as unconfirmed");
    assert(section("captains").textContent.includes("wakes 3") && section("captains").textContent.includes("abcdef1"), "Captain line");
    assert(item("wait").textContent === waiting.title && !root.querySelector("img"), "Untrusted text stays text");
    assert(!root.querySelector('a[href^="javascript:"]'), "Unsafe link omitted");
    assert(root.querySelector('a[href="' + success.output.url + '"]')?.textContent === "#1", "Closed PR link");
    item("wait").click();
    item("run").click();
    await tick();
    assert(pendingSignal.aborted, "Replaced detail aborted");
    assert(item("run").getAttribute("aria-expanded") === "true" && item("wait").getAttribute("aria-expanded") === "false", "One row expanded");
    pending(detailData(waiting, null));
    await tick();
    const detail = () => root.querySelector("#detail-run");
    assert(detail() && detail().previousElementSibling.contains(item("run")), "Detail opens in place under its row");
    assert(detail().textContent.includes("pr revision") && detail().textContent.includes("Prerequisites"), "Stage history and dependencies");
    assert(detail().textContent.includes("att2") && detail().textContent.includes("opaque-worker-7"), "Attempt and legacy worker");
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
    offline = true;
    refresh();
    await tick();
    assert(root.textContent.includes("Disconnected") && status().includes("stale"), "Disconnection and staleness shown");
    assert(item("fail") && item("run"), "Cached rows remain visible");
    offline = false;
    refresh();
    await tick();
    assert(status() === "Connected." && !root.textContent.includes("Disconnected"), "Refresh recovers");
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL – " + error.message;
    result.dataset.status = "failed";
  } finally {
    unmount();
  }
})();
