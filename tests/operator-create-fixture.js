/* Serve the repo root with an HTTP static server and open tests/operator-create-fixture.html.
 * Uses the real shell, board and HTTP client; only API transport is faked. No dependencies.
 * ?live instead uses /operator/ and real API requests on an isolated local-board test server
 * with a registered repository. It creates one report item; never run it on a live muster.
 */
(async () => {
  const result = document.querySelector("#result");
  const frame = document.querySelector("#app");
  const ownerMode = new URLSearchParams(location.search).has("owner");
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const until = async predicate => {
    const deadline = Date.now() + 5000;
    while (!predicate()) {
      if (Date.now() > deadline) throw new Error("Timed out: " + predicate);
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  try {
    if (new URLSearchParams(location.search).has("live")) {
      // The production shell disallows framing; use its actual markup/assets without
      // weakening that CSP or replacing any API transport.
      const shell = await (await fetch("/operator/")).text();
      frame.contentDocument.open();
      frame.contentDocument.write(shell);
      frame.contentDocument.close();
      await until(() => frame.contentDocument?.querySelector(".new-item"));
      const doc = frame.contentDocument;
      const win = frame.contentWindow;
      const requests = [];
      const realFetch = win.fetch.bind(win);
      win.fetch = (path, options = {}) => {
        requests.push({ path, authorized: Boolean(options.headers?.Authorization) });
        return realFetch(path, options);
      };
      const button = doc.querySelector(".new-item");
      button.click();
      const form = doc.querySelector("#operator-create-form");
      await until(() => !form.hidden && form.elements.repository_id.options.length);
      assert(!doc.querySelector("#operator-create-token"), "No local creation token input");
      assert(doc.activeElement === form.elements.repository_id, "Repository receives focus");
      form.elements.goal.value = " ";
      form.requestSubmit();
      await until(() => form.elements.goal.getAttribute("aria-invalid") === "true");
      const goal = "Real localhost browser creation " + crypto.randomUUID();
      form.elements.goal.value = goal;
      form.elements.target.value = "report";
      form.requestSubmit();
      await until(() => !doc.querySelector("#operator-create").open);
      await until(() => doc.activeElement === button);
      assert(doc.querySelector("#operator-board").textContent.includes(goal), "Queued row inserted");
      const page = await (await realFetch("/v1/operator/executions")).json();
      const saved = page.items.find(row => row.goal === goal);
      assert(saved?.status === "queued" && saved.target === "report", "Real server persisted report target");
      assert((await realFetch("/v1/owner/status")).status === 401, "Unrelated owner API stays closed");
      assert((await realFetch("/v1/items", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repository_id: saved.repository_id, goal: "Must not create" })
      })).status === 401, "Tokenless /v1 intake stays closed");
      const settings = doc.querySelector("#operator-settings");
      assert(!doc.querySelector("#operator-settings-token"), "No local settings token input");
      settings.open = true;
      const maximum = doc.querySelector("#operator-max-writers");
      const building = doc.querySelector("#operator-max-building-writers");
      await until(() => maximum.value !== "");
      const before = await (await realFetch("/v1/dispatch/settings")).json();
      maximum.value = String(before.max_writers + 1);
      building.value = String(before.max_building_writers + 1);
      doc.querySelector("#operator-settings-form").requestSubmit();
      await until(() => doc.querySelector("#operator-settings-status").textContent === "Concurrency saved.");
      const after = await (await realFetch("/v1/dispatch/settings")).json();
      assert(after.max_writers === before.max_writers + 1 &&
        after.max_building_writers === before.max_building_writers + 1 &&
        after.paused === before.paused && after.aging_seconds === before.aging_seconds,
      "Settings save uses canonical state without overwriting pause/aging");
      assert((await realFetch("/v1/dispatch/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_writers: 1 })
      })).status === 401, "Tokenless /v1 settings writes stay closed");
      assert(requests.some(row => row.path === "/operator/api/repositories") &&
        requests.filter(row => row.path === "/operator/api/items").length === 2 &&
        requests.every(row => !row.authorized), "Real option loading and writes sent no bearer token");
      result.textContent = "PASS: real tokenless localhost options, validation, creation, persisted target, focus, canonical settings and /v1 boundaries";
      document.documentElement.dataset.result = "passed";
      return;
    }
    let shell = await (await fetch("../phalanx/static/operator-board.html")).text();
    shell = shell.replaceAll("/operator/assets/", new URL("../phalanx/static/", location.href).href);
    const win = frame.contentWindow;
    const doc = win.document;
    const posts = [];
    const settingsWrites = [];
    let rejectSettingsSave = true;
    let maximum = 5;
    let buildingMaximum = 2;
    let lostResponse;
    let saved;
    const now = new Date().toISOString();
    const rows = Array.from({ length: 60 }, (_, index) => ({
      id: index.toString(16).padStart(32, "0"), item_id: (index + 100).toString(16).padStart(32, "0"),
      title: "Existing queued item " + index, repository_id: "example", stage: "development",
      status: "queued", created_at: now, last_advanced_at: now
    }));
    doc.open();
    win.fetch = async (path, options = {}) => {
      const json = (body, status = 200) => new Response(JSON.stringify(body), {
        status, headers: { "content-type": "application/json" }
      });
      if (path === "/v1/owner/status") {
        return options.headers.Authorization === "Bearer " + "o".repeat(32)
          ? json({ role: "owner" }) : json({ detail: "Owner token required" }, 403);
      }
      if (path === "/operator/api/repositories") return json([{ id: "example" }, { id: "second-repo" }]);
      if (path === "/operator/api/dispatch/settings") {
        if (options.method === "PUT") {
          settingsWrites.push(options);
          if (ownerMode && options.headers.Authorization !== "Bearer " + "o".repeat(32)) return json({}, 403);
          if (rejectSettingsSave) {
            rejectSettingsSave = false;
            return json({}, 503);
          }
          maximum = JSON.parse(options.body).max_writers;
          buildingMaximum = JSON.parse(options.body).max_building_writers;
        }
        return json({ paused: true, aging_seconds: 99, max_writers: maximum, active_writers: 3,
          max_building_writers: buildingMaximum, building_writers: 2, delivering_writers: 1 });
      }
      if (path.startsWith("/v1/operator/executions?")) {
        if (ownerMode && !options.headers.Authorization) return json({ detail: "Bearer token required" }, 401);
        const limit = Number(new URL(path, location.origin).searchParams.get("limit"));
        return json({ items: path.includes("kind=open") ? rows.slice(0, limit) : [], next_cursor: null });
      }
      if (path === "/v1/flags") return json({ flags: [] });
      if (path.startsWith("/v1/captains?")) return json({ captains: [] });
      if (path === "/operator/api/items") {
        posts.push(options);
        const body = JSON.parse(options.body);
        if (body.label === "bad label") return json({
          detail: [{ loc: ["body", "label"], msg: "Label must match the intake pattern" }]
        }, 422);
        saved ??= {
          id: "a".repeat(32), ...body, execution: {
            id: "b".repeat(32), version: 1, created_at: now, updated_at: now,
            state: { target: body.target, publication_mode: body.publication_mode,
              stage: "development", status: "queued" }
          }
        };
        if (posts.length === 2) return new Promise((_, reject) => {
          lostResponse = () => reject(new TypeError("Connection lost after commit"));
        });
        return json(saved, 201);
      }
      throw new Error("Unexpected request: " + path);
    };
    doc.write(shell);
    doc.close();
    if (ownerMode) {
      await until(() => doc.querySelector("#operator-connect")?.hidden === false);
      doc.querySelector("#operator-token").value = "o".repeat(32);
      doc.querySelector("#operator-connect").requestSubmit();
    }
    await until(() => doc.querySelector(".new-item"));
    const button = doc.querySelector(".new-item");
    const modal = doc.querySelector("#operator-create");
    const form = doc.querySelector("#operator-create-form");
    const status = doc.querySelector("#operator-create-status");
    const fields = doc.querySelector("#operator-create-fields");
    const submit = doc.querySelector("#operator-create-submit");
    const close = () => doc.querySelector("#operator-create-close").click();
    const loaded = () => until(() => !form.hidden);
    assert(button.getAttribute("aria-label") === "New work item", "Header control has accessible name");
    button.click();
    await loaded();
    assert(!doc.querySelector("#operator-create-token") && !doc.querySelector("#operator-create-auth"),
      "Creation has no separate token gate");
    assert(doc.activeElement === form.elements.repository_id, "Creation focuses the usable form");
    assert(form.elements.repository_id.options.length === 2, "Repositories come from registration read");
    assert(form.elements.target.value === "checked_in" &&
      form.elements.target.selectedOptions[0].textContent === "Merged", "Intake default uses display mapping");
    form.elements.goal.value = "**Browser-created work**";
    form.elements.acceptance_criteria.value = "First criterion\nSecond criterion";
    form.elements.label.value = "bad label";
    form.requestSubmit();
    await until(() => !fields.disabled);
    assert(doc.querySelector("#operator-create-errors").textContent.includes("Label must match") &&
      form.elements.label.getAttribute("aria-invalid") === "true", "Intake validation shown inline");
    form.elements.label.value = "PH-29b";
    form.elements.dependencies.value = "upstream ready_for_check_in\nother";
    form.requestSubmit();
    await until(() => lostResponse);
    form.dispatchEvent(new win.Event("submit", { bubbles: true, cancelable: true }));
    assert(posts.length === 2 && submit.disabled, "Double submit is locked");
    lostResponse();
    await until(() => submit.textContent === "Retry queue request");
    assert(fields.disabled, "Uncertain response retains immutable payload");
    close();
    await until(() => !modal.open && doc.activeElement === button);
    button.click();
    await loaded();
    win.scrollTo(0, 300);
    const y = win.scrollY;
    const url = win.location.href;
    submit.click();
    await until(() => !modal.open);
    assert(posts.length === 3 && posts[1].body === posts[2].body &&
      posts[1].headers["Idempotency-Key"] === posts[2].headers["Idempotency-Key"],
    "Retry reuses the exact request and idempotency key");
    assert(posts.every(post => ownerMode ? post.headers.Authorization === "Bearer " + "o".repeat(32)
      : !("Authorization" in post.headers)), "Local creation never sends a bearer token");
    const payload = JSON.parse(posts[2].body);
    assert(payload.acceptance_criteria.length === 2 && payload.dependencies[1].milestone === "checked_in",
      "Criteria and dependency controls send intake shape");
    assert(doc.querySelector('button[data-item="' + saved.execution.id + '"]'), "Successful create adds queued row");
    assert(win.location.href === url && win.scrollY === y, "Creation preserves URL and scroll");
    await until(() => doc.activeElement === button);
    button.click();
    await loaded();
    assert(form.elements.goal.value === "" && !fields.disabled, "Success prepares a fresh form");
    close();
    const settings = doc.querySelector("#operator-settings");
    const capacity = doc.querySelector("#operator-capacity");
    const capacityStatus = doc.querySelector("#operator-settings-status");
    const capacityForm = doc.querySelector("#operator-settings-form");
    const capacityInput = doc.querySelector("#operator-max-writers");
    const buildingInput = doc.querySelector("#operator-max-building-writers");
    settings.open = true;
    await until(() => capacity.textContent.includes("Building 2/2 | Delivering 1 | Total 3/5"));
    assert(doc.querySelector('label[for="operator-max-writers"]'), "Concurrency input is labelled");
    assert(doc.querySelector('label[for="operator-max-building-writers"]'), "Building input is labelled");
    buildingInput.value = "6";
    capacityForm.requestSubmit();
    await until(() => capacityStatus.textContent.includes("Building no greater than Total"));
    assert(settingsWrites.length === 0, "Incoherent pair rejected without writing");
    capacityInput.value = "2";
    buildingInput.value = "1";
    assert(!doc.querySelector("#operator-settings-token"), "Settings has no separate token gate");
    capacityForm.requestSubmit();
    await until(() => capacityStatus.textContent.includes("HTTP 503"));
    assert(maximum === 5 && capacityStatus.textContent.includes("Refresh capacity"),
      "Settings save failure is explicit and preserves server state");
    doc.querySelector("#operator-settings-refresh").click();
    await until(() => capacityInput.value === "5");
    capacityInput.value = "2";
    buildingInput.value = "1";
    capacityForm.requestSubmit();
    await until(() => capacityStatus.textContent === "Concurrency saved.");
    assert(maximum === 2 && buildingMaximum === 1 &&
      capacity.textContent.includes("Building 2/1 | Delivering 1 | Total 3/2"),
      "Trusted dashboard can lower below current usage and sees authoritative result");
    assert(settingsWrites.every(write => ownerMode
      ? write.headers.Authorization === "Bearer " + "o".repeat(32)
      : !("Authorization" in write.headers)), "Local settings never sends a bearer token");
    const changed = JSON.parse(settingsWrites.at(-1).body);
    assert(Object.keys(changed).sort().join() === "max_building_writers,max_writers",
      "Saving sends exactly the atomic pair, not pause or aging");
    maximum = 7;
    doc.querySelector("#operator-settings-refresh").click();
    await until(() => capacityInput.value === "7");
    assert(capacity.textContent.includes("Building 2/1 | Delivering 1 | Total 3/7"),
      "Refresh reads persisted effective capacity");
    if (ownerMode) {
      doc.querySelector("#operator-board").append(settings);
      doc.querySelector("#operator-disconnect").click();
      assert(settings.parentElement.id === "operator-session" && settings.hidden,
        "Unmount returns a toolbar-reparented control to its home");
      doc.querySelector("#operator-token").value = "o".repeat(32);
      doc.querySelector("#operator-connect").requestSubmit();
      await until(() => !settings.hidden && capacityInput.value === "7");
      assert(doc.querySelector("#operator-settings") === settings,
        "Reconnect reuses the same authoritative settings form");
    }
    result.textContent = "PASS: owner authorization, inline validation, double submit, lost-response retry, queued row, focus and scroll";
    document.documentElement.dataset.result = "passed";
  } catch (error) {
    result.textContent = "FAIL: " + error.stack;
    document.documentElement.dataset.result = "failed";
  }
})();
