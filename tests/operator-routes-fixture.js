/* Serve the repository root and open this fixture's HTML. Uses real board/client/renderer. */
(async () => {
  if (new URLSearchParams(location.search).has("child")) return;
  const result = document.querySelector("#result");
  const frame = document.querySelector("#app");
  const preview = new URLSearchParams(location.search).get("preview");
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
  ].join("\n") + ("\n\n## More findings\n\n" + "Long report paragraph. ".repeat(30)).repeat(50);
  const approval = "Approve the following draft?\n\n" +
    "- Keep the complete first requirement.\n- Preserve the second requirement.\n\n" +
    "https://example.com/" + "long-path-".repeat(40) + "\n\n" + "long-filename-".repeat(40) + ".md\n\n" +
    "A detailed approval paragraph with all of its context. ".repeat(40) + "\n\nEnd of approval draft.";
  let readingLayout = false;
  let ageOffset = 0;
  const waitingRow = () => ({ ...row, status: "waiting_for_input", outcome: null, output: null,
    stage: "development", version: 3, wait_reason: approval, resume_trigger: approval });
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
    const clock = win.Date.now.bind(win.Date);
    win.Date.now = () => clock() + ageOffset * 60000;
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
      if (path.startsWith("/v1/captains?")) return json({ captains: [
        { id: "cap-live", state: "active", live: true, stale: false, heartbeat_at: now,
          wake_count: 7, compaction_count: 4, charter_revision: "opaque-hash", focus: [id] },
        { id: "cap-stale", state: "active", live: false, stale: true, heartbeat_at: now, compaction_count: null },
        { id: "cap-ended", state: "ended", live: false, stale: false, heartbeat_at: now, compaction_count: 0 }
      ] });
      if (path === "/v1/operator/items/" + id) return fail ? json({}, 404) : json({ item: row });
      if (path.startsWith("/v1/operator/executions/" + execution + "/timeline?")) {
        if (fail) return json({}, 404);
        const cursor = new URL(path, location.origin).searchParams.get("cursor");
        return json({ item: readingLayout ? waitingRow() : row, attempt: null, prerequisites: [],
          question: readingLayout ? { prompt: approval, options: [approval], proposed_default: approval } : null,
          previous_execution_id: previous, next_execution_id: null,
          events: [{ created_at: now, stage: "report_storage", status: "completed", event: "result" }],
          next_cursor: cursor ? null : "history_cursor" });
      }
      if (path.startsWith("/v1/operator/executions?")) {
        const params = new URL(path, location.origin).searchParams;
        const cursor = params.get("cursor");
        return json({ items: params.get("q") === "no matches" && !cursor ? [] : params.get("kind") === "closed" ?
          (readingLayout ? [9, 12, 180, 2880].map((minutes, index) => ({ ...row,
            id: String(index + 1).repeat(32), closed_at: new Date(Date.parse(now) - minutes * 60000).toISOString()
          })) : [row]) : [
          readingLayout ? waitingRow() : { ...row, status: "running", outcome: null, output: null, stage: "development" }
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
    app.doc.querySelector("[data-report]").focus();
    app.doc.querySelector("[data-report]").click();
    await until(() => app.doc.querySelector("#operator-report-content h1"));
    const content = app.doc.querySelector("#operator-report-content");
    assert(content.querySelector("strong") && content.querySelector("em") && content.querySelector("ul li") &&
      content.querySelector("ol li") && content.querySelector("blockquote") && content.querySelector("table td"), "Markdown renders semantic blocks");
    assert(content.querySelector("pre code").textContent.includes("<img"), "Fenced code is literal text");
    assert(!content.querySelector("script, img, svg, iframe, style") && !app.win.reportExecuted, "Report HTML never becomes DOM");
    assert(!content.querySelector('a[href^="javascript:"]') && content.querySelector("a").protocol === "https:",
      "Only safe generated links survive");
    for (const width of [1280, 390, 320]) {
      frame.style.width = width + "px";
      frame.style.height = "600px";
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      const dialog = app.doc.querySelector("#operator-report");
      const body = app.doc.querySelector("#operator-report-body");
      const close = dialog.querySelector("button");
      const initial = close.getBoundingClientRect();
      assert(close.getAttribute("aria-label") === "Close field report" && close.textContent.trim() === "×",
        "Report has an explicitly labeled X button");
      assert(initial.width >= 44 && initial.height >= 44, "Close touch target at " + width);
      assert(body.scrollHeight > body.clientHeight * 3, "Actual report is long enough to scroll");
      for (const fraction of [0, .5, 1]) {
        body.scrollTop = (body.scrollHeight - body.clientHeight) * fraction;
        await new Promise(resolve => app.win.requestAnimationFrame(resolve));
        const bounds = close.getBoundingClientRect();
        const modal = dialog.getBoundingClientRect();
        assert(Math.abs(bounds.top - initial.top) < 1 && Math.abs(bounds.right - initial.right) < 1,
          "Report close stays anchored at " + width + "px, scroll " + fraction);
        assert(bounds.top >= modal.top && bounds.right <= modal.right && bounds.bottom <= body.getBoundingClientRect().top,
          "Close is visible above report text and scrollbar");
        assert(body.scrollWidth <= body.clientWidth && app.doc.documentElement.scrollWidth <= width,
          "Report does not overflow horizontally at " + width);
        assert(app.doc.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2)?.closest("button") === close,
          "Close remains clickable above the scrolling report");
      }
      close.focus();
      assert(app.doc.activeElement === close && [...app.doc.styleSheets].some(sheet =>
        [...sheet.cssRules].some(rule => rule.selectorText === "#operator-report :focus-visible" &&
          rule.style.outline.includes("solid"))), "Close retains focus and a visible keyboard focus outline");
    }
    const opener = app.doc.querySelector("[data-report]");
    if (preview === "report") {
      result.textContent = "Report layout checks passed; preview retained.";
      return;
    }
    app.doc.querySelector("#operator-report button").click();
    await until(() => !app.doc.querySelector("#operator-report").open);
    assert(app.doc.activeElement === opener, "Closing report returns focus to opener");
    opener.click();
    await until(() => app.doc.querySelector("#operator-report-content h1"));
    assert(app.doc.querySelector("#operator-report-body").scrollTop === 0, "Reopened report starts at the beginning");
    app.doc.querySelector("#operator-report").close();
    frame.style.width = "100%";
    frame.style.height = "900px";

    app = await load("/operator/queue?repository=example&label=PH-29c&stage=development&status=running&q=search&cursor=saved_cursor");
    await until(() => app.doc.querySelector("[data-item]"));
    let listCalls = calls.filter(path => path.startsWith("/v1/operator/executions?") && !path.includes("limit=1&"));
    assert(listCalls.some(path => ["repository=example", "label=PH-29c", "stage=development", "status=running", "q=search",
      "cursor=saved_cursor"].every(value => path.includes(value))), "Queue URL filters and keyset cursor reach server");
    assert(!listCalls.some(path => path.includes("kind=closed")), "Queue does not load history");
    assert(app.doc.querySelector(".sec-queued [data-item]") && !app.doc.querySelector(".sec-active"),
      "Full queue includes running work, without home sections");
    assert(app.doc.querySelector('input[name="repository"]').value === "example", "URL filters populate controls");
    const dropdowns = [...app.doc.querySelectorAll(".filter-dropdown")];
    assert(dropdowns.length === 4 && dropdowns.every(dropdown => !dropdown.open),
      "Queue filters start collapsed");
    assert(dropdowns[0].querySelector("summary").textContent === "repository: example",
      "Collapsed filter keeps its readable label and selected URL value");
    dropdowns[0].querySelector("summary").click();
    await until(() => dropdowns[0].open);
    dropdowns[1].querySelector("summary").click();
    await until(() => dropdowns[1].open && !dropdowns[0].open);
    const labelInput = dropdowns[1].querySelector("input");
    labelInput.value = "draft filter";
    labelInput.dispatchEvent(new app.win.Event("input", { bubbles: true }));
    assert(dropdowns[1].querySelector("summary").textContent === "label: draft filter",
      "Dropdown reflects pending selected state");
    labelInput.focus();
    labelInput.dispatchEvent(new app.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert(!dropdowns[1].open && app.doc.activeElement === dropdowns[1].querySelector("summary"),
      "Escape closes dropdown and returns keyboard focus");
    assert(app.doc.querySelector(".board-filters > a").getAttribute("href") === "/operator/queue",
      "Clear resets queue filters and search");
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

    app = await load("/operator/");
    await until(() => app.doc.querySelector("[data-item]"));
    await until(() => app.doc.querySelector(".sec-captains")?.textContent.includes("compactions 4"));
    const captainRows = app.doc.querySelector(".sec-captains");
    assert(captainRows.textContent.includes("compactions unavailable") && captainRows.textContent.includes("compactions 0") &&
      !captainRows.textContent.includes("opaque-hash"), "HTTP adapter preserves known, zero and unknown native counters");
    for (const [width, maxHeight] of [[1280, 44], [600, 140], [390, 150], [320, 195]]) {
      frame.style.width = width + "px";
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      const filters = app.doc.querySelector(".board-filters");
      assert(filters.getBoundingClientRect().height <= maxHeight,
        "Six collapsed filters stay compact at " + width + "px");
      assert([...filters.querySelectorAll("summary, button, a")].every(control =>
        control.getBoundingClientRect().height >= 44), "Filter touch targets stay at least 44px");
      assert(app.doc.documentElement.scrollWidth <= width, "Filters do not cause horizontal overflow");
      const captainStats = app.doc.querySelectorAll(".sec-captains > div > .captains .stat");
      assert(captainStats.length === 6, "Both current Captain rows expose their three telemetry fields");
      for (const stat of captainStats) {
        assert(stat.scrollWidth <= stat.clientWidth + 1, "Captain telemetry stays readable at " + width + "px");
      }
    }
    const longLabel = app.doc.querySelector('.board-filters input[name="label"]');
    longLabel.value = "long-label-".repeat(9);
    longLabel.dispatchEvent(new app.win.Event("input", { bubbles: true }));
    assert(app.doc.documentElement.scrollWidth <= 320 &&
      longLabel.closest("details").querySelector("summary").title.endsWith(longLabel.value),
    "Long selected values stay available without horizontal overflow");
    frame.style.width = "100%";

    readingLayout = true;
    app = await load("/operator/");
    await until(() => app.doc.querySelector(".sec-needs [data-item]"));
    app.doc.querySelector(".sec-needs [data-item]").click();
    await until(() => app.doc.querySelector(".ask"));
    for (const width of [1280, 390, 320]) {
      frame.style.width = width + "px";
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      const detail = app.doc.querySelector(".detail");
      const facts = detail.querySelector(".facts");
      for (const heading of ["Wait reason", "Resumes on", "Question for the owner"]) {
        const block = [...detail.querySelectorAll(".block")].find(node => node.querySelector("h4")?.textContent === heading);
        const text = block.querySelector(".pre");
        assert(text.textContent === approval, "Complete multiline approval retained in " + heading);
        assert(block.parentElement === detail && block.getBoundingClientRect().top >= facts.getBoundingClientRect().bottom,
          heading + " is beneath compact metadata");
        assert(text.getBoundingClientRect().width >= facts.getBoundingClientRect().width * .85,
          heading + " uses available detail width at " + width);
        assert(app.win.getComputedStyle(text).whiteSpace === "pre-wrap", "Paragraphs and lists retain line breaks");
      }
      assert(detail.querySelector(".cmd")?.textContent.includes('"ANSWER"'), "Existing reply command retained");
      assert(!detail.querySelector("img") && app.doc.documentElement.scrollWidth <= width,
        "Long approval options, URLs and text fit at " + width);
    }
    frame.style.width = "1280px";
    await new Promise(resolve => app.win.requestAnimationFrame(resolve));
    const closedStarts = () => [...app.doc.querySelectorAll(".closed .age")].map(cell => {
      const range = app.doc.createRange();
      range.selectNodeContents(cell);
      return range.getBoundingClientRect().left;
    });
    const before = closedStarts();
    assert(before.length === 4 && Math.max(...before) - Math.min(...before) < 1,
      "Closed label starts align for single/double-digit minutes, hours and days");
    assert([...app.doc.querySelectorAll(".closed .age time")].every(time => time.title && time.getAttribute("aria-label")),
      "Closed ages retain absolute-time accessibility");
    ageOffset = 1;
    app.doc.querySelector('[data-action="refresh"]').click();
    await until(() => app.doc.querySelector(".closed .age").textContent.includes("10m"));
    const after = closedStarts();
    assert(after.every((left, index) => Math.abs(left - before[index]) < 1),
      "Closed start alignment survives live relative-time refresh");
    if (preview === "needs") {
      result.textContent = "Needs-you and timestamp layout checks passed; preview retained.";
      return;
    }
    readingLayout = false;
    frame.style.width = "100%";

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
    if (result.dataset.status === "passed") frame.remove();
  }
})();
