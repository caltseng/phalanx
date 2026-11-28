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
  const nativeId = "12345678-1234-1234-1234-123456789abc";
  const writerUrl = "ghapp://sessions/" + nativeId;
  const captainUrl = "codex://threads/" + nativeId;
  const row = { id: execution, item_id: id, title: "Searchable report", goal: "Keep the existing details",
    repository_id: "example", stage: "report_storage", status: "completed", created_at: now,
    last_advanced_at: now, active_started_at: now, stage_started_at: now,
    closed_at: now, outcome: "report_stored", output: { kind: "report" },
    delivery: { merged: true, merge_commit: "f".repeat(40), merged_at: now, merge_method: "merge" },
    writer: { state: "ended", runtime: "copilot-app", session_id: nativeId, session_url: writerUrl,
      session_availability: "unverified",
      session_link: { url: writerUrl, reason: "recorded session; existence unverified" } } };
  const report = [
    "# Field report", "", "**Bold** and *emphasis* with `code <img src=x>`.", "",
    "- First", "- Second", "", "1. Ordered", "2. List", "", "> Quoted", "",
    "| Check | Result |", "| --- | --- |", "| Tests | Passed |", "",
    "[Safe](https://example.com/report) [Unsafe](javascript:alert%281%29)",
    "[Relative](/relative-report) [Empty]() [Credentials](https://user:password@example.com/report)",
    "![No image](https://example.com/tracker.png)", "",
    "<script>window.reportExecuted=true</script><img src=x onerror=alert(1)>",
    '<svg onload="alert(1)"><a href="javascript:alert(1)">HTML link</a></svg>', "",
    "```html", "<img src=x onerror=alert(1)>", "```"
  ].join("\n") + ("\n\n## More findings\n\n" + "Long report paragraph. ".repeat(30)).repeat(50);
  const approval = "Approve the following draft?\n\n" +
    "- Keep the complete first requirement.\n- Preserve the second requirement.\n\n" +
    "https://example.com/" + "long-path-".repeat(40) + "\n\n" + "long-filename-".repeat(40) + ".md\n\n" +
    "A detailed approval paragraph with all of its context. ".repeat(40) + "\n\nEnd of approval draft.";
  let emptyBrief = false;
  const criteria = ["<img src=x onerror=alert(1)>", "Complete requirement. ".repeat(100)];
  const context = ["A plain note\nwith two lines", "docs\\work-items.md", "https://example.com/brief",
    "javascript:alert(1)", "https://user:password@example.com/private", "<script>alert(1)</script>"];
  const withBrief = item => ({ ...item, acceptance_criteria: emptyBrief ? [] : criteria,
    context_links: emptyBrief ? [] : context });
  let readingLayout = false;
  let ageOffset = 0;
  const waitingRow = () => ({ ...row, status: "waiting_for_input", outcome: null, output: null,
    stage: "development", version: 3, wait_reason: approval, resume_trigger: approval });
  let calls = [];
  let conditionalReads = 0;
  let allNotModified = false;
  let pollFailure = false;
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
    const intervals = [];
    const createdNodes = [];
    const renderListeners = [];
    const createElement = doc.createElement.bind(doc);
    doc.createElement = (...args) => {
      const node = createElement(...args);
      createdNodes.push(node);
      return node;
    };
    const addListener = win.EventTarget.prototype.addEventListener;
    win.EventTarget.prototype.addEventListener = function (...args) {
      if (this.matches?.("[data-page], [data-scout], .fgroup, .ended-captains")) renderListeners.push(this);
      return addListener.apply(this, args);
    };
    win.setInterval = (callback, delay) => intervals.push({ callback, delay });
    win.clearInterval = () => {};
    const clock = win.Date.now();
    win.Date.now = () => clock + ageOffset * 60000;
    win.history.replaceState(null, "", path);
    doc.open();
    win.onerror = message => errors.push(message);
    calls = [];
    win.fetch = async (path, options = {}) => {
      calls.push(String(path));
      if (pollFailure) throw new Error("Fixture poll disconnected");
      const json = (body, status = 200) => new win.Response(JSON.stringify(body), {
        status, headers: { "content-type": "application/json" }
      });
      if (allNotModified || path === "/v1/operator/overview" || path.startsWith("/v1/operator/executions?")) {
        if (options.headers["If-None-Match"] === 'W/"fixture"') {
          conditionalReads++;
          return new win.Response(null, { status: 304, headers: { ETag: 'W/"fixture"' } });
        }
      }
      const snapshot = body => new win.Response(JSON.stringify(body), {
        headers: { "content-type": "application/json", ETag: 'W/"fixture"' }
      });
      if (path === "/operator/api/repositories") return json([{ id: "example" }]);
      if (path === "/operator/api/items") {
        const payload = JSON.parse(options.body);
        return json({ id: "e".repeat(32), ...payload, execution: {
          id: "d".repeat(32), version: 1, created_at: now, updated_at: now,
          state: { target: payload.target, publication_mode: payload.publication_mode,
            stage: "development", status: "queued" }
        } }, 201);
      }
      if (path === "/v1/operator/overview") return snapshot({
        sections: { needs_you: 10, running: 200, queued: 9390, blocked: 0, waiting_on_checks: 400 }
      });
      if (path === "/v1/flags") return snapshot({ flags: [
        { kind: "head_drift", reason: "Head moved", recovery: "Reconcile the current head",
          first_seen: now, notify_owner: false }
      ] });
      if (path.startsWith("/v1/captains?")) return snapshot({ captains: [
        { id: "cap-live", session_ref: captainUrl,
          session_link: { url: captainUrl, reason: "recorded session; existence unverified" },
          state: "active", live: true, stale: false, heartbeat_at: now,
          wake_count: 7, compaction_count: 4, charter_revision: "opaque-hash", focus: [id] },
        { id: "cap-stale", state: "active", live: false, stale: true, heartbeat_at: now, compaction_count: null },
        { id: "cap-ended", state: "ended", live: false, stale: false, heartbeat_at: now, compaction_count: 0 }
      ] });
      if (path === "/v1/operator/items/" + id) return fail ? json({}, 404) : snapshot({ item: withBrief(row) });
      if (path.startsWith("/v1/operator/executions/" + execution + "/timeline?")) {
        if (fail) return json({}, 404);
        const cursor = new URL(path, location.origin).searchParams.get("cursor");
        return snapshot({ item: withBrief(readingLayout ? waitingRow() : row), attempt: null, prerequisites: [],
          question: readingLayout ? { prompt: approval, options: [approval], proposed_default: approval } : null,
          previous_execution_id: previous, next_execution_id: null,
          events: [{ created_at: now, stage: "report_storage", status: "completed", event: "result" }],
          next_cursor: cursor ? null : "history_cursor" });
      }
      if (path.startsWith("/v1/operator/executions?")) {
        const params = new URL(path, location.origin).searchParams;
        const cursor = params.get("cursor");
        return snapshot({ items: params.get("q") === "no matches" && !cursor ? [] : params.get("kind") === "closed" ?
          (readingLayout ? [9, 12, 180, 2880].map((minutes, index) => ({ ...row,
            id: String(index + 1).repeat(32), closed_at: new Date(Date.parse(now) - minutes * 60000).toISOString()
          })) : [row]) : [
          readingLayout ? waitingRow() : { ...row, status: "running", outcome: null, output: null,
            stage: "development", next_action: "A worker is running the development stage" }
        ], next_cursor: cursor ? null : "next_cursor" });
      }
      if (path.endsWith("/report")) return new win.Response(report, { headers: { "content-type": "text/plain" } });
      throw new Error("Unexpected request: " + path);
    };
    doc.write(shell.replace("<head>", "<head><script>history.replaceState(null, '', " +
      JSON.stringify(path) + ");</script>"));
    doc.close();
    await until(() => doc.querySelector(".board-navigation"));
    await new Promise(resolve => win.requestAnimationFrame(resolve));
    const sectionName = path.startsWith("/operator/queue") ? "sec-queued"
      : path.startsWith("/operator/history") ? "sec-closed"
        : /^\/operator\/(items|executions)\//.test(path) ? "sec-detail" : null;
    if (sectionName) assert(createdNodes.filter(node => node.matches(".sec"))
      .every(node => node.classList.contains(sectionName)), "Only visible sections are constructed on " + path);
    assert(!renderListeners.length, "Row/pager/scout/disclosure handlers are mounted once, not on render");
    return { win, doc, intervals };
  }

  try {
    shell = await (await fetch("../phalanx/static/operator-board.html")).text();
    shell = shell.replaceAll("/operator/assets/", new URL("../phalanx/static/", location.href).href);
    shell = shell.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" content="form-action \'none\'">');
    let app = await load("/operator/items/" + id);
    await until(() => app.doc.querySelector(".detail-links"));
    assert(calls.includes("/v1/operator/items/" + id), "Item route resolves the current execution");
    assert(app.doc.querySelector(".goal").textContent === row.goal, "Item route reuses full existing detail");
    const activeSince = [...app.doc.querySelectorAll(".facts dt")]
      .find(node => node.textContent === "Active since");
    assert(activeSince?.nextElementSibling.querySelector("time")?.dateTime === now,
      "Real HTTP adapter preserves authoritative activation for detail age");
    const assertBrief = () => {
      const blocks = [...app.doc.querySelectorAll(".work-brief")];
      assert(blocks.length === 2, "Both labeled brief blocks render");
      assert(blocks[0].querySelector("summary").textContent === "Acceptance criteria (2)",
        "Long criteria use native keyboard-accessible disclosure");
      assert(blocks[0].querySelector("li").textContent === criteria[0] &&
        !blocks[0].querySelector("img,script"), "User-supplied criteria remain literal text");
      assert(blocks[1].querySelectorAll("a").length === 1 &&
        blocks[1].querySelector("a").href === context[2], "Only explicit safe URLs become links");
      assert(blocks[1].textContent.includes(context[0]) && blocks[1].textContent.includes(context[1]),
        "Plain notes and paths remain readable text");
      blocks[0].querySelector("summary").focus();
      assert(app.doc.activeElement === blocks[0].querySelector("summary"), "Disclosure is keyboard focusable");
      blocks[0].open = true;
      assert(blocks[0].querySelectorAll("li")[1].textContent === criteria[1], "Long content is complete, not truncated");
      assert([...app.doc.querySelectorAll(".block h4")].some(node => node.textContent === "Observed usage"),
        "Observed usage remains alongside the brief");
    };
    assertBrief();
    assert(app.doc.querySelector('.facts a[href="' + writerUrl + '"]')?.title.includes("unverified"),
      "Real HTTP adapter preserves a native session and honest availability on item details");
    assert(app.doc.querySelector('a[href="/operator/executions/' + previous + '"]'), "Previous execution is one click away");
    assert(!app.doc.querySelector(".sec-queued"), "Detail route does not fetch or display backlog");
    assert(!calls.some(path => path.includes("limit=100")), "Detail reads are bounded independently of list pages");
    app.doc.querySelector('button[aria-label="Next history page"]').click();
    await until(() => calls.some(path => path.includes("cursor=history_cursor")));

    app = await load("/operator/executions/" + execution);
    await until(() => app.doc.querySelector(".detail-links"));
    assertBrief();
    for (const width of [1280, 390, 320]) {
      frame.style.width = width + "px";
      for (const block of app.doc.querySelectorAll(".work-brief")) block.open = true;
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      assert(app.doc.documentElement.scrollWidth <= width, "Full briefs wrap at " + width + "px");
    }
    emptyBrief = true;
    for (const path of ["/operator/items/" + id, "/operator/executions/" + execution]) {
      app = await load(path);
      await until(() => app.doc.querySelector(".detail-links"));
      assert([...app.doc.querySelectorAll(".work-brief p")].every(node => node.textContent === "None provided.") &&
        app.doc.querySelectorAll(".work-brief p").length === 2, "Absent criteria and context are explicit on " + path);
    }
    emptyBrief = false;
    assert(app.doc.querySelector('.facts a[href="' + writerUrl + '"]'), "Closed execution permalink retains writer link");
    assert(!calls.some(path => path.startsWith("/v1/operator/items/")), "Execution route preserves exact execution");
    app.doc.querySelector("[data-report]").focus();
    app.doc.querySelector("[data-report]").click();
    await until(() => app.doc.querySelector("#operator-report-content h1"));
    const content = app.doc.querySelector("#operator-report-content");
    const copy = app.doc.querySelector("#operator-report-copy");
    const copyStatus = app.doc.querySelector("#operator-report-copy-status");
    let copied;
    let rejectCopy = false;
    let finishCopy;
    Object.defineProperty(app.win.navigator, "clipboard", { configurable: true, value: {
      async writeText(text) {
        copied = text;
        if (rejectCopy) throw new Error("Clipboard denied");
      }
    } });
    assert(copy.getAttribute("aria-label") === "Copy full field report" && !copy.disabled &&
      copy.querySelector('svg[aria-hidden="true"]'), "Copy has an accessible name and decorative icon");
    copy.click();
    await until(() => copyStatus.textContent === "Full report copied.");
    assert(copied === report, "Copies complete original Markdown including long tail, tables, code and line breaks");
    rejectCopy = true;
    copy.click();
    await until(() => !copy.disabled);
    assert(copyStatus.textContent.includes("Could not copy") && !copyStatus.textContent.includes("copied."),
      "Clipboard rejection reports failure, never success, and permits retry");
    rejectCopy = false;
    Object.defineProperty(app.win.navigator, "clipboard", { configurable: true, value: undefined });
    copy.click();
    await until(() => !copy.disabled);
    assert(copyStatus.textContent.includes("Could not copy"), "Unavailable clipboard fails truthfully");
    Object.defineProperty(app.win.navigator, "clipboard", { configurable: true, value: {
      writeText: () => new Promise(resolve => { finishCopy = resolve; })
    } });
    assert(content.querySelector("strong") && content.querySelector("em") && content.querySelector("ul li") &&
      content.querySelector("ol li") && content.querySelector("blockquote") && content.querySelector("table td"), "Markdown renders semantic blocks");
    assert(content.querySelector("pre code").textContent.includes("<img"), "Fenced code is literal text");
    assert(!content.querySelector("script, img, svg, iframe, style") && !app.win.reportExecuted, "Report HTML never becomes DOM");
    assert(!content.querySelector('a[href^="javascript:"]') && content.querySelector("a").protocol === "https:",
      "Only safe generated links survive");
    const reportLinks = [...content.querySelectorAll("a")];
    assert(reportLinks.find(link => link.textContent === "Relative")?.href === location.origin + "/relative-report" &&
      reportLinks.find(link => link.textContent === "Empty")?.href === app.doc.baseURI &&
      !reportLinks.some(link => link.textContent === "Credentials"),
    "Shared link validation preserves relative and empty Markdown targets and rejects credentials");
    for (const width of [1280, 390, 320]) {
      frame.style.width = width + "px";
      frame.style.height = "600px";
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      const dialog = app.doc.querySelector("#operator-report");
      const body = app.doc.querySelector("#operator-report-body");
      const close = dialog.querySelector("#operator-report-close");
      const initial = close.getBoundingClientRect();
      assert(close.getAttribute("aria-label") === "Close field report" && close.textContent.trim() === "×",
        "Report has an explicitly labeled X button");
      const size = width <= 760 || app.win.matchMedia("(pointer: coarse)").matches ? 44 : 36;
      assert(initial.width === size && initial.height === size &&
        copy.getBoundingClientRect().width === size && copy.getBoundingClientRect().height === size,
      "Copy and Close are compact with accessible pointer targets at " + width);
      assert(app.win.getComputedStyle(close).padding === "0px", "Close has no unnecessary internal padding");
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
      result.dataset.status = "passed";
      return;
    }
    copy.click();
    assert(copy.disabled && copyStatus.textContent === "Copying report…", "Pending copy does not announce success");
    app.doc.querySelector("#operator-report-close").click();
    await until(() => !app.doc.querySelector("#operator-report").open);
    assert(app.doc.activeElement === opener, "Closing report returns focus to opener");
    opener.click();
    await until(() => app.doc.querySelector("#operator-report-content h1"));
    finishCopy();
    await new Promise(resolve => app.win.setTimeout(resolve, 0));
    assert(copyStatus.textContent === "" && !copy.disabled, "Old copy completion cannot affect a reopened report");
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
    const formation = app.doc.querySelector(".mark-run");
    const spears = [...formation.querySelectorAll(".spear")];
    const reduced = app.win.matchMedia("(prefers-reduced-motion: reduce)").matches;
    assert(spears.length === 3, "Running dashboard item renders the selected three-spear formation");
    for (const width of [1280, 390, 320]) {
      frame.style.width = width + "px";
      for (const theme of ["light", "dark"]) {
        app.doc.documentElement.dataset.theme = theme;
        await new Promise(resolve => app.win.requestAnimationFrame(resolve));
        const bounds = formation.getBoundingClientRect();
        assert(Math.abs(bounds.width - 12) < .1 && Math.abs(bounds.height - 16) < .1,
          "Formation remains legible at actual row-icon size: " + width + "px " + theme);
        assert(app.doc.documentElement.scrollWidth <= width, "Formation adds no horizontal overflow");
        const animations = spears.map(spear => spear.getAnimations()[0]);
        if (reduced) {
          assert(animations.every(animation => !animation), "Reduced motion leaves all three spears static");
        } else {
          assert(animations.every(animation => animation?.effect.getTiming().duration === 2200),
            "Each spear has a 2.2-second wave");
          const delays = animations.map(animation => animation.effect.getTiming().delay);
          assert(Math.abs(delays[2] - delays[0] - 220) < 1 &&
            Math.abs(delays[1] - delays[0] - 440) < 1, "Wave starts left, center, right at .22-second intervals");
          const line = formation.closest(".line");
          const title = line.querySelector(".title");
          const stationary = [formation, line, title].map(node => node.getBoundingClientRect().top);
          for (let index = 0; index < spears.length; index++) {
            const animation = animations[index];
            const delay = animation.effect.getTiming().delay;
            animation.pause();
            animation.currentTime = delay + 2200;
            const rest = spears[index].getBoundingClientRect();
            animation.currentTime = delay + 2860;
            const peak = spears[index].getBoundingClientRect();
            assert(Math.abs(rest.top - peak.top - 2) < .1 && Math.abs(rest.left - peak.left) < .1,
              "Spear travels 2 rendered pixels vertically, without the old horizontal march");
            assert(peak.top >= bounds.top && rest.bottom <= bounds.bottom,
              "The complete wave stays inside the icon frame without clipping");
            assert([formation, line, title].every((node, at) =>
              Math.abs(node.getBoundingClientRect().top - stationary[at]) < .1),
            "Icon frame, row and neighboring title stay stationary");
            animation.play();
          }
        }
        assert([...app.doc.querySelectorAll(".mark-captain, .mark-idle, .mark-wait")].every(icon =>
          icon.getAnimations().length === 0), "Non-work icons do not inherit the work animation");
      }
    }
    frame.style.width = "100%";
    if (preview === "formation") {
      result.textContent = "Formation checks passed (" + (reduced ? "reduced motion" : "animated") + "); preview retained.";
      result.dataset.status = "passed";
      return;
    }
    const filterTrigger = app.doc.querySelector('[data-action="filters"]');
    const searchTrigger = app.doc.querySelector('[data-action="search"]');
    const filterPanel = app.doc.getElementById(filterTrigger.getAttribute("aria-controls"));
    const searchPanel = app.doc.getElementById(searchTrigger.getAttribute("aria-controls"));
    assert(filterPanel.hidden && searchPanel.hidden &&
      filterTrigger.getAttribute("aria-expanded") === "false", "Secondary controls start collapsed");
    assert(filterTrigger.getAttribute("aria-label") === "Work filters (4 applied)" &&
      searchTrigger.getAttribute("aria-label") === "Search all work (1 applied)" &&
      app.doc.querySelector(".toolbar-applied").textContent.includes("PH-29c"),
    "Collapsed toolbar exposes applied filters and search, independent of pending edits");
    assert(app.doc.querySelector('.board-navigation a[aria-current="page"]').textContent === "Queue",
      "Navigation marks the current page");
    filterTrigger.click();
    assert(!filterPanel.hidden && filterTrigger.getAttribute("aria-expanded") === "true" &&
      app.doc.activeElement === filterPanel.querySelector("summary"), "Filters disclosure moves focus into controls");
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
    dropdowns[1].querySelector("summary").dispatchEvent(
      new app.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert(filterPanel.hidden && app.doc.activeElement === filterTrigger &&
      filterTrigger.getAttribute("aria-expanded") === "false", "Second Escape closes filters and restores its trigger");
    filterTrigger.click();
    searchTrigger.click();
    assert(filterPanel.hidden && !searchPanel.hidden && app.doc.activeElement === searchPanel.querySelector("input"),
      "Opening search closes filters and focuses search");
    searchPanel.querySelector("input").dispatchEvent(
      new app.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    assert(searchPanel.hidden && app.doc.activeElement === searchTrigger, "Escape returns from search to its trigger");
    assert(app.win.getComputedStyle(searchTrigger, "::after").display === "none",
      "Escape also dismisses the focus description without moving focus");
    filterTrigger.click();
    assert(labelInput.value === "draft filter", "Disclosure changes preserve unapplied filter edits");
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
    assert(app.doc.querySelector('.closed a.writer[href="' + writerUrl + '"]'), "History rows retain ended writer sessions");
    app.doc.querySelector('[data-action="refresh"]').click();
    await until(() => calls.filter(path => path.includes("cursor=saved_cursor")).length >= 2);

    app = await load("/operator/");
    await until(() => app.doc.querySelector("[data-item]"));
    await until(() => app.doc.querySelector(".sec-captains")?.textContent.includes("compactions 4"));
    await until(() => app.doc.querySelector(".h-live"));
    assert(app.doc.querySelector(".counts").textContent.includes("200 running") &&
      app.doc.querySelector(".counts").textContent.includes("9790 queued and waiting"),
    "Real adapter reads global totals from overview");
    assert(app.doc.querySelector(".sec-active .reason").textContent === "A worker is running the development stage",
      "Real adapter retains next_action on list rows");
    const unchangedPoll = async (expectedReads, renderExpected = false) => {
      await new Promise(resolve => app.win.requestAnimationFrame(() => app.win.requestAnimationFrame(resolve)));
      const mutations = [];
      const observer = new app.win.MutationObserver(records => mutations.push(...records));
      observer.observe(app.doc.querySelector("#operator-board"), {
        subtree: true, childList: true, attributes: true, characterData: true
      });
      const before = conditionalReads;
      allNotModified = true;
      app.intervals.find(timer => timer.delay === 15000).callback();
      await until(() => conditionalReads >= before + expectedReads);
      await new Promise(resolve => app.win.requestAnimationFrame(() => app.win.requestAnimationFrame(resolve)));
      mutations.push(...observer.takeRecords());
      observer.disconnect();
      allNotModified = false;
      assert(conditionalReads === before + expectedReads, "Every poll read returns a bodyless 304");
      assert(renderExpected ? mutations.length > 0 : mutations.length === 0,
        renderExpected ? "A cached poll reconciles changed local read state" :
          "All-304 poll makes no DOM mutations, including health and summary");
    };
    await unchangedPoll(5);
    app.doc.querySelector(".sec-active [data-item]").click();
    await until(() => app.doc.querySelector(".detail-links"));
    const retainedBrief = app.doc.querySelector(".work-brief");
    retainedBrief.open = true;
    await unchangedPoll(6);
    assert(retainedBrief.isConnected && retainedBrief.open,
      "Unchanged polling preserves brief disclosure DOM and user state");
    const ages = [...app.doc.querySelectorAll(".sections time[datetime]")].map(node => ({
      node, text: node.textContent, title: node.title, context: node.dataset.ageContext
    }));
    const ageTick = app.intervals.find(timer => timer.delay === 5000).callback;
    ageOffset = 2;
    ageTick();
    assert(ages.length > 0 && ages.every(({ node, text, title, context }) =>
      node.isConnected && node.textContent !== text && node.title === title &&
      node.getAttribute("aria-label").includes(node.textContent) &&
      (!context || node.getAttribute("aria-label").startsWith(context + " · "))),
    "Ticker updates unchanged stage, closed, merged, flag and Captain ages in place across a span boundary");
    const ageMutations = [];
    const ageObserver = new app.win.MutationObserver(records => ageMutations.push(...records));
    ageObserver.observe(app.doc.querySelector(".sections"), {
      subtree: true, childList: true, attributes: true, characterData: true
    });
    ageTick();
    ageMutations.push(...ageObserver.takeRecords());
    ageObserver.disconnect();
    assert(ageMutations.length === 0, "Ticker leaves row DOM untouched while displayed age spans are unchanged");
    ageOffset = 0;
    ageTick();
    await unchangedPoll(6);
    app.doc.querySelector(".sec-active [data-item]").click();
    pollFailure = true;
    app.doc.querySelector('[data-action="refresh"]').click();
    await until(() => app.doc.querySelector(".h-down"));
    pollFailure = false;
    await unchangedPoll(5, true);
    assert(app.doc.querySelector(".h-live") && !app.doc.querySelector(".sections .warn"),
      "Cached snapshots recover from failed reads without requiring a changed server body");
    app.doc.querySelector(".new-item").click();
    const createForm = app.doc.querySelector("#operator-create-form");
    await until(() => !createForm.hidden);
    createForm.elements.goal.value = "Optimistic intake outside the current cached page";
    createForm.requestSubmit();
    await until(() => app.doc.querySelector('[data-item="' + "d".repeat(32) + '"]'));
    await unchangedPoll(5, true);
    assert(!app.doc.querySelector('[data-item="' + "d".repeat(32) + '"]'),
      "Create does not mutate cached list projections; the next all-304 poll removes the optimistic row");
    const beforeConditional = conditionalReads;
    app.doc.querySelector('[data-action="refresh"]').click();
    await until(() => conditionalReads >= beforeConditional + 3);
    assert(app.doc.querySelector(".sec-active .reason").textContent === "A worker is running the development stage" &&
      app.doc.querySelector(".counts").textContent.includes("9790 queued and waiting"),
    "Polling sends If-None-Match and retains snapshots after bodyless 304 responses");
    const captainRows = app.doc.querySelector(".sec-captains");
    const captainLinks = captainRows.querySelectorAll('a[href="' + captainUrl + '"]');
    assert(captainLinks.length === 2 && [...captainLinks].every(link => link.tabIndex === 0) &&
      captainLinks[0].querySelector(".mark-captain"), "HTTP adapter links both Captain helmet and name to the recorded app");
    assert(captainRows.textContent.includes("compactions unavailable") && captainRows.textContent.includes("compactions 0") &&
      !captainRows.textContent.includes("opaque-hash"), "HTTP adapter preserves known, zero and unknown native counters");
    const heights = [];
    for (const [width, maxHeight] of [[1280, 100], [600, 200], [390, 210], [320, 210]]) {
      frame.style.width = width + "px";
      await new Promise(resolve => app.win.requestAnimationFrame(resolve));
      if (width <= 600) {
        assert([...app.doc.querySelectorAll("button.title, .writer[href], .captains a, .pager button, .fgroup > summary")]
          .filter(node => node.getClientRects().length).every(node => {
            const box = node.getBoundingClientRect();
            return box.width >= 44 && box.height >= 44;
          }), "Phone row, session, disclosure and pager touch targets are at least 44px");
      }
      const headerHeight = app.doc.querySelector(".sections").getBoundingClientRect().top -
        app.doc.querySelector("#operator-board").getBoundingClientRect().top;
      heights.push({ width, height: headerHeight });
      assert(headerHeight <= maxHeight, "Complete default header/control area stays compact at " + width + "px");
      const lockup = app.doc.querySelector(".brand-lockup");
      lockup.focus({ preventScroll: true });
      await new Promise(resolve => setTimeout(resolve, 650));
      // Canonical tip-outline: 10px stroke, with 12-by-29 chevron legs in a 64px viewBox.
      const tipOverhang = 5 * Math.hypot(12, 29) / 12 *
        lockup.querySelector(".brand-shield").getBoundingClientRect().width / 64;
      assert(lockup.querySelector(".brand-tip-outline").getBoundingClientRect().top >= tipOverhang,
        "Focused rim-break stroke stays inside the viewport at " + width + "px");
      lockup.blur();
      const controls = [...app.doc.querySelectorAll(".bar .brand-lockup, .bar .actions > button, .bar .actions > details > summary, .board-navigation a")]
        .filter(control => control.getBoundingClientRect().height);
      assert(controls.every(control => {
        const box = control.getBoundingClientRect();
        const size = control.classList.contains("tool-button") && width > 760 &&
          !app.win.matchMedia("(pointer: coarse)").matches ? 36 : 44;
        return box.width >= size && box.height >= size &&
          (control.getAttribute("aria-label") || control.textContent.trim());
      }), "Toolbar and navigation have named accessible pointer targets");
      const actions = app.doc.querySelector(".actions");
      assert(Math.abs(actions.getBoundingClientRect().right -
        app.doc.querySelector(".bar").getBoundingClientRect().right) < 1,
      "Controls are docked to the right edge at " + width + "px");
      const tools = [...actions.querySelectorAll(".tool-button")];
      const size = width <= 760 || app.win.matchMedia("(pointer: coarse)").matches ? 44 : 36;
      assert(tools.every(tool => tool.getBoundingClientRect().height === size),
        "Control row uses compact sizing at " + width + "px");
      assert(tools.map(tool => tool.dataset.action || "settings").join(",") === "create,search,filters,settings,theme,refresh",
        "Board action order is unchanged");
      assert(controls.every((control, index) => controls.slice(index + 1).every(other => {
        const a = control.getBoundingClientRect(), b = other.getBoundingClientRect();
        return a.right <= b.left + .01 || b.right <= a.left + .01 ||
          a.bottom <= b.top + .01 || b.bottom <= a.top + .01;
      })), "Toolbar controls do not overlap at " + width + "px");
      const toggles = [...app.doc.querySelectorAll('.bar button[aria-controls]')];
      assert(toggles.every(trigger => app.doc.getElementById(trigger.getAttribute("aria-controls")).hidden),
        "Default controls do not reserve space for collapsed panels");
      app.doc.querySelector('[data-action="filters"]').click();
      const filters = app.doc.querySelector(".board-filters");
      assert([...filters.querySelectorAll("summary, button, a")].every(control =>
        control.getBoundingClientRect().height >= 44), "Filter touch targets stay at least 44px");
      filters.querySelector("summary").click();
      await until(() => filters.querySelector("details").open);
      assert(filters.querySelector("input").getBoundingClientRect().height >= 44,
        "Expanded filter input keeps its touch target");
      assert(app.doc.documentElement.scrollWidth <= width, "Expanded filters do not overflow at " + width + "px");
      app.doc.querySelector('[data-action="filters"]').click();
      const searchToggle = app.doc.querySelector('[data-action="search"]');
      searchToggle.click();
      assert(app.doc.querySelector('.board-search input').getBoundingClientRect().height >= 44 &&
        app.doc.documentElement.scrollWidth <= width, "Expanded search fits and retains its touch target");
      searchToggle.click();
      assert(app.doc.documentElement.scrollWidth <= width, "Filters do not cause horizontal overflow");
      const captainStats = app.doc.querySelectorAll(".sec-captains > div > .captains .stat");
      assert(captainStats.length === 6, "Both current Captain rows expose their three telemetry fields");
      for (const stat of captainStats) {
        assert(stat.scrollWidth <= stat.clientWidth + 1, "Captain telemetry stays readable at " + width + "px");
      }
    }
    result.dataset.headerHeights = JSON.stringify(heights);
    app.doc.querySelector('[data-action="filters"]').click();
    const longLabel = app.doc.querySelector('.board-filters input[name="label"]');
    longLabel.value = "long-label-".repeat(9);
    longLabel.dispatchEvent(new app.win.Event("input", { bubbles: true }));
    assert(app.doc.documentElement.scrollWidth <= 320 &&
      longLabel.closest("details").querySelector("summary").title.endsWith(longLabel.value),
    "Long selected values stay available without horizontal overflow");
    frame.style.width = "100%";
    app.doc.querySelector('[data-action="filters"]').click();
    const themeButton = app.doc.querySelector('[data-action="theme"]');
    const initialTheme = app.win.localStorage.getItem("phalanx-board-theme") || "auto";
    const nextTheme = { auto: "dark", dark: "light", light: "auto" };
    let expectedTheme = initialTheme;
    for (let index = 0; index < 3; index++) {
      themeButton.click();
      expectedTheme = nextTheme[expectedTheme];
      await until(() => themeButton.getAttribute("aria-label").startsWith("Theme: " + expectedTheme + "."));
      assert(app.win.localStorage.getItem("phalanx-board-theme") === expectedTheme &&
        (app.doc.documentElement.dataset.theme || "auto") === expectedTheme,
      "Theme cycles and persists without changing its semantics");
    }

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
      result.dataset.status = "passed";
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
    for (const width of [1280, 600, 500, 390, 320]) {
      frame.style.width = width + "px";
      await new Promise(resolve => {
        frame.onload = resolve;
        frame.src = "operator-board-fixture.html?preview=1";
      });
      await until(() => ["passed", "failed"].includes(
        frame.contentDocument.querySelector("#result")?.dataset.status));
      const doc = frame.contentDocument;
      assert(doc.querySelector("#result").dataset.status === "passed", "Canonical settings board fixture passes");
      doc.querySelector("#operator-settings summary").click();
      await until(() => doc.querySelector("#operator-settings").open);
      const panel = doc.querySelector(".toolbar-settings-panel").getBoundingClientRect();
      assert(panel.left >= 0 && panel.right <= doc.documentElement.clientWidth &&
        panel.top >= doc.querySelector(".bar").getBoundingClientRect().bottom,
      "Settings panel fits below the complete toolbar at " + width + "px");
      assert([...doc.querySelectorAll(".toolbar-settings-panel input, .toolbar-settings-panel button")].every(control => {
        const box = control.getBoundingClientRect();
        return box.width >= 44 && box.height >= 44;
      }), "Canonical settings retains 44px input/save targets");
    }
    result.textContent = "PASS";
    result.dataset.status = "passed";
  } catch (error) {
    result.textContent = "FAIL - " + error.message + " Requests: " + JSON.stringify(calls) +
      " Errors: " + JSON.stringify(errors) + " URL: " + frame.contentWindow.location.href +
      " Page: " + frame.contentDocument.body.textContent.slice(0, 200);
    result.dataset.status = "failed";
  } finally {
    if (result.dataset.status === "passed" && !["formation", "needs", "report"].includes(preview)) frame.remove();
  }
})();
