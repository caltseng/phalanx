/* Serve the repository root and open this fixture's HTML. Uses real board/client/renderer. */
(async () => {
  if (new URLSearchParams(location.search).has("child")) return;
  const result = document.querySelector("#result");
  const frame = document.querySelector("#app");
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const until = async predicate => {
    const deadline = Date.now() + 5000;
    while (!predicate()) {
      if (Date.now() > deadline) throw new Error("Timed out: " + predicate);
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  const id = "a".repeat(32), execution = "b".repeat(32), previous = "c".repeat(32);
  const now = new Date().toISOString();
  const row = { id: execution, item_id: id, title: "Searchable report", goal: "Keep the existing details",
    repository_id: "example", stage: "report_storage", status: "completed", created_at: now,
    last_advanced_at: now, closed_at: now, outcome: "report_stored", output: { kind: "report" } };
  const report = [
    "# Field report", "", "**Bold** and *emphasis* with `code <img src=x>`.", "",
    "- First", "- Second", "", "1. Ordered", "2. List", "", "> Quoted", "",
    "| Check | Result |", "| --- | --- |", "| Tests | Passed |", "",
    "[Safe](https://example.com/report) [Unsafe](javascript:alert%281%29)",
    "![No image](https://example.com/tracker.png)", "",
    "<script>window.reportExecuted=true</script><img src=x onerror=alert(1)>",
    '<svg onload="alert(1)"><a href="javascript:alert(1)">HTML link</a></svg>', "",
    "```html", "<img src=x onerror=alert(1)>", "```"
  ].join("\n");
  let calls = [];
  let fail = false;
  let shell;
  const errors = [];

  async function load(path) {
    await new Promise(resolve => {
      frame.onload = resolve;
      frame.src = "operator-routes-fixture.html?child";
    });
    const win = frame.contentWindow;
    const doc = win.document;
    win.history.replaceState(null, "", path);
    doc.open();
    win.onerror = message => errors.push(message);
    calls = [];
    win.fetch = async (path, options = {}) => {
      calls.push(String(path));
      const json = (body, status = 200) => new win.Response(JSON.stringify(body), {
        status, headers: { "content-type": "application/json" }
      });
      if (path === "/v1/flags") return json({ flags: [] });
      if (path.startsWith("/v1/captains?")) return json({ captains: [] });
      if (path === "/v1/operator/items/" + id) return fail ? json({}, 404) : json({ item: row });
      if (path.startsWith("/v1/operator/executions/" + execution + "/timeline?")) {
        if (fail) return json({}, 404);
        const cursor = new URL(path, location.origin).searchParams.get("cursor");
        return json({ item: row, attempt: null, prerequisites: [], question: null,
          previous_execution_id: previous, next_execution_id: null,
          events: [{ created_at: now, stage: "report_storage", status: "completed", event: "result" }],
          next_cursor: cursor ? null : "history_cursor" });
      }
      if (path.startsWith("/v1/operator/executions?")) {
        const params = new URL(path, location.origin).searchParams;
        const cursor = params.get("cursor");
        return json({ items: params.get("q") === "no matches" && !cursor ? [] : params.get("kind") === "closed" ? [row] : [
          { ...row, status: "running", outcome: null, output: null, stage: "development" }
        ], next_cursor: cursor ? null : "next_cursor" });
      }
      if (path.endsWith("/report")) return new win.Response(report, { headers: { "content-type": "text/plain" } });
      throw new Error("Unexpected request: " + path);
    };
    doc.write(shell.replace("<head>", "<head><script>history.replaceState(null, '', " +
      JSON.stringify(path) + ");</script>"));
    doc.close();
    await until(() => doc.querySelector(".board-navigation"));
    return { win, doc };
  }

  try {
    shell = await (await fetch("../phalanx/static/operator-board.html")).text();
    shell = shell.replaceAll("/operator/assets/", new URL("../phalanx/static/", location.href).href);
    shell = shell.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" content="form-action \'none\'">');
    let app = await load("/operator/items/" + id);
    await until(() => app.doc.querySelector(".detail-links"));
    assert(calls.includes("/v1/operator/items/" + id), "Item route resolves the current execution");
    assert(app.doc.querySelector(".goal").textContent === row.goal, "Item route reuses full existing detail");
    assert(app.doc.querySelector('a[href="/operator/executions/' + previous + '"]'), "Previous execution is one click away");
    assert(!app.doc.querySelector(".sec-queued"), "Detail route does not fetch or display backlog");
    assert(!calls.some(path => path.includes("limit=100")), "Detail reads are bounded independently of list pages");
    app.doc.querySelector('button[aria-label="Next history page"]').click();
    await until(() => calls.some(path => path.includes("cursor=history_cursor")));

    app = await load("/operator/executions/" + execution);
    await until(() => app.doc.querySelector(".detail-links"));
    assert(!calls.some(path => path.startsWith("/v1/operator/items/")), "Execution route preserves exact execution");
    app.doc.querySelector("[data-report]").click();
    await until(() => app.doc.querySelector("#operator-report-content h1"));
    const content = app.doc.querySelector("#operator-report-content");
    assert(content.querySelector("strong") && content.querySelector("em") && content.querySelector("ul li") &&
      content.querySelector("ol li") && content.querySelector("blockquote") && content.querySelector("table td"), "Markdown renders semantic blocks");
    assert(content.querySelector("pre code").textContent.includes("<img"), "Fenced code is literal text");
    assert(!content.querySelector("script, img, svg, iframe, style") && !app.win.reportExecuted, "Report HTML never becomes DOM");
    assert(!content.querySelector('a[href^="javascript:"]') && content.querySelector("a").protocol === "https:",
      "Only safe generated links survive");

    app = await load("/operator/queue?repository=example&label=PH-29c&stage=development&status=running&q=search&cursor=saved_cursor");
    await until(() => app.doc.querySelector("[data-item]"));
    let listCalls = calls.filter(path => path.startsWith("/v1/operator/executions?") && !path.includes("limit=1&"));
    assert(listCalls.some(path => ["repository=example", "label=PH-29c", "stage=development", "status=running", "q=search",
      "cursor=saved_cursor"].every(value => path.includes(value))), "Queue URL filters and keyset cursor reach server");
    assert(!listCalls.some(path => path.includes("kind=closed")), "Queue does not load history");
    assert(app.doc.querySelector(".sec-queued [data-item]") && !app.doc.querySelector(".sec-active"),
      "Full queue includes running work, without home sections");
    assert(app.doc.querySelector('input[name="repository"]').value === "example", "URL filters populate controls");
    app.doc.querySelector(".pager button").click();
    await until(() => !app.win.location.search.includes("cursor="));
    const filterForm = app.doc.querySelector(".board-filters");
    filterForm.elements.label.value = "PH-29c-updated";
    filterForm.requestSubmit();
    await until(() => app.win.location.search.includes("label=PH-29c-updated"));
    assert(!app.win.location.search.includes("cursor=") && app.win.location.search.includes("q=search"),
      "Filter submission works under strict CSP, keeps query and resets cursor");

    app = await load("/operator/queue?q=no+matches");
    await until(() => app.doc.querySelector('button[aria-label="Next open work page"]'));
    assert(!app.doc.querySelector("[data-item]"), "A bounded filtered page may be empty");
    app.doc.querySelector('button[aria-label="Next open work page"]').click();
    await until(() => app.doc.querySelector("[data-item]"));
    assert(app.win.location.search.includes("cursor=next_cursor") && app.win.location.search.includes("q=no+matches"),
      "Empty filtered continuation pages remain navigable and preserve URL query");

    app = await load("/operator/history?outcome=report_stored&closed_after=2026-01-01&q=%237&cursor=saved_cursor");
    await until(() => app.doc.querySelector("[data-item]"));
    assert(calls.some(path => path.includes("kind=closed") && path.includes("outcome=report_stored") &&
      path.includes("closed_after=2026-01-01") && path.includes("q=%237") && path.includes("cursor=saved_cursor")),
    "History restores filters and cursor from URL");
    assert(!app.doc.querySelector(".sec-queued"), "History is a dedicated closed page");
    app.doc.querySelector('[data-action="refresh"]').click();
    await until(() => calls.filter(path => path.includes("cursor=saved_cursor")).length >= 2);

    for (const query of [id, execution, "#42", "https://github.com/a/b/pull/42", "title words"]) {
      app = await load("/operator/?q=" + encodeURIComponent(query));
      await until(() => app.doc.querySelector(".sec-closed [data-item]"));
      for (const kind of ["open", "closed"]) {
        assert(calls.some(path => path.startsWith("/v1/operator/executions?") &&
          new URL(path, location.origin).searchParams.get("q") === query && path.includes("kind=" + kind)),
        "Global search spans both lists for " + query);
      }
      assert(app.doc.querySelector('form[role="search"]').getAttribute("action") === "/operator/", "Search always targets all work");
    }
    const searchForm = app.doc.querySelector('form[role="search"]');
    searchForm.elements.q.value = "#99";
    searchForm.requestSubmit();
    await until(() => app.win.location.search === "?q=%2399");
    assert(app.win.location.pathname === "/operator/", "Global search navigates under strict CSP");

    for (const [status, kind] of [["running", "open"], ["completed", "closed"]]) {
      app = await load("/operator/?status=" + status);
      await until(() => app.doc.querySelector("[data-item]"));
      const lists = calls.filter(path => new URL(path, location.origin).searchParams.has("kind"));
      assert(lists.length === 1 && lists[0].includes("kind=" + kind) && lists[0].includes("status=" + status),
        "Home status filter never shows the opposite unfiltered list");
    }

    fail = true;
    app = await load("/operator/executions/" + execution);
    await until(() => app.doc.querySelector(".sec-detail .warn"));
    assert(app.doc.querySelector(".sec-detail").textContent.includes("may not exist"), "Missing detail has a visible failure");
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL - " + error.message + " Requests: " + JSON.stringify(calls) +
      " Errors: " + JSON.stringify(errors) + " URL: " + frame.contentWindow.location.href +
      " Page: " + frame.contentDocument.body.textContent.slice(0, 200);
    result.dataset.status = "failed";
  } finally {
    frame.remove();
  }
})();
