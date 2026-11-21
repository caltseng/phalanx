/* Open operator-board-fixture.html in a browser. Sample data lives only here. */
(async () => {
  const root = document.querySelector("main");
  const result = document.querySelector("#result");
  const now = "2026-11-20T12:00:00Z";
  const waiting = {
    id: "wait", title: "<img src=x> Choose deployment target", repo: "example/service",
    stage: "development", status: "waiting_for_input", lastAdvancedAt: now,
    waitReason: "Two targets match the work request.", chatUrl: "https://example.com/chat",
    draftUrl: "javascript:alert(1)"
  };
  const running = {
    id: "run", title: "Update telemetry", repo: "example/service", stage: "review",
    status: "running", lastAdvancedAt: now
  };
  const failure = {
    id: "fail", title: "Audit build", repo: "example/service", stage: "report",
    status: "closed", outcome: "Failed", closedAt: now,
    output: { kind: "report", url: "https://example.com/reports/build" }
  };
  const success = {
    ...failure, id: "done", title: "Verify rollout", outcome: "Checked in",
    output: { kind: "pr", url: "https://example.com/pull/1" }
  };
  let offline = false;
  let pending;
  let pendingSignal;
  const calls = [];
  const detailData = (item, cursor) => ({
    item, attempt: "Attempt 2", nextAction: "Review revision", resumeTrigger: "CI completes",
    dependencies: [{ title: "Build", milestone: "Verified", status: "satisfied" }],
    events: [{
      at: now, fromStage: cursor ? "development" : "review",
      stage: cursor ? "review" : "development", status: "running", reason: "Review loop"
    }],
    nextCursor: cursor ? null : "older-events"
  });

  const source = {
    async list({ kind, cursor, limit }) {
      calls.push([kind, cursor, limit]);
      if (offline) throw new Error("API offline");
      if (kind === "open") {
        return { items: cursor ? [running] : [waiting, running], nextCursor: cursor ? null : "more-open" };
      }
      return { items: cursor ? [] : [success, failure], nextCursor: cursor ? null : "older-closed" };
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
  const refresh = () => root.querySelector("header button").click();
  const unmount = window.PhalanxOperatorBoard.mount(root, source);

  try {
    await tick();
    assert(calls.length === 2 && calls.every(call => call[2] === 25), "Initial reads are bounded lists");
    assert(root.textContent.includes("waiting for input") && root.textContent.includes(waiting.waitReason), "Actionable wait");
    assert(item("wait").textContent === waiting.title && !root.querySelector("img"), "Untrusted text stays text");
    assert(!root.querySelector('a[href^="javascript:"]'), "Unsafe link omitted");
    assert(root.querySelector('a[href="' + failure.output.url + '"]')?.textContent === "Field report", "Failure output");
    assert(root.querySelector('a[href="' + success.output.url + '"]')?.textContent === "Verified PR", "Success output");
    item("wait").click();
    item("run").click();
    await tick();
    assert(pendingSignal.aborted, "Replaced selection aborted");
    pending(detailData(waiting, null));
    await tick();
    const detail = root.querySelector('[aria-label="Selected item"]');
    assert(document.activeElement.textContent === running.title, "Selection transfers focus to detail");
    assert(detail.textContent.includes("review → development") && detail.textContent.includes("Prerequisites"), "Current detail renders the stage loop and dependencies");
    assert(detail.textContent.includes("Attempt 2") && detail.textContent.includes("CI completes"), "Attempt and resume trigger");
    assert(!detail.textContent.includes(waiting.title), "Late detail ignored");
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
    offline = true;
    refresh();
    await tick();
    assert(root.textContent.includes("Disconnected") && root.textContent.includes("stale"), "Disconnection and staleness shown");
    assert(item("fail") && detail.textContent.includes("Transition history"), "Cached outcomes and timeline remain visible");
    offline = false;
    refresh();
    await tick();
    assert(root.textContent.includes("Connected.") && !root.textContent.includes("stale"), "Refresh recovers");
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL – " + error.message;
    result.dataset.status = "failed";
  } finally {
    unmount();
  }
})();
