/* Serve the repo root with an HTTP static server and open tests/operator-create-fixture.html.
 * Uses the real shell, board and HTTP client; only API transport is faked. No dependencies.
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
    let shell = await (await fetch("../phalanx/static/operator-board.html")).text();
    shell = shell.replaceAll("/operator/assets/", new URL("../phalanx/static/", location.href).href);
    const win = frame.contentWindow;
    const doc = win.document;
    const posts = [];
    const settingsWrites = [];
    let maximum = 5;
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
      if (path === "/v1/repositories") return json([{ id: "example" }, { id: "second-repo" }]);
      if (path === "/v1/dispatch/settings") {
        if (options.method === "PUT") {
          settingsWrites.push(options);
          if (options.headers.Authorization !== "Bearer " + "o".repeat(32)) return json({}, 403);
          maximum = JSON.parse(options.body).max_writers;
        }
        return json({ paused: true, aging_seconds: 99, max_writers: maximum, active_writers: 3 });
      }
      if (path.startsWith("/v1/operator/executions?")) {
        if (ownerMode && !options.headers.Authorization) return json({ detail: "Bearer token required" }, 401);
        const limit = Number(new URL(path, location.origin).searchParams.get("limit"));
        return json({ items: path.includes("kind=open") ? rows.slice(0, limit) : [], next_cursor: null });
      }
      if (path === "/v1/flags") return json({ flags: [] });
      if (path.startsWith("/v1/captains?")) return json({ captains: [] });
      if (path === "/v1/items") {
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
    const auth = doc.querySelector("#operator-create-auth");
    const token = doc.querySelector("#operator-create-token");
    const close = () => doc.querySelector("#operator-create-close").click();
    const unlock = async () => {
      if (ownerMode) {
        await until(() => !form.hidden);
        return;
      }
      token.value = "o".repeat(32);
      auth.requestSubmit();
      await until(() => !form.hidden);
    };
    assert(button.getAttribute("aria-label") === "New work item", "Header control has accessible name");
    button.click();
    if (!ownerMode) {
      assert(modal.open && doc.activeElement === token, "Local board asks for owner token with focus");
      token.value = "r".repeat(32);
      auth.requestSubmit();
      await until(() => status.textContent.includes("Could not unlock"));
      assert(form.hidden && posts.length === 0, "Runner credential never unlocks intake");
    }
    await unlock();
    assert(token.value === "", "Token cleared from the input");
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
    assert(ownerMode ? auth.hidden : !auth.hidden && form.hidden,
      "Only the owner-connected board retains its write credential");
    await unlock();
    win.scrollTo(0, 300);
    const y = win.scrollY;
    const url = win.location.href;
    submit.click();
    await until(() => !modal.open);
    assert(posts.length === 3 && posts[1].body === posts[2].body &&
      posts[1].headers["Idempotency-Key"] === posts[2].headers["Idempotency-Key"],
    "Retry reuses the exact request and idempotency key");
    const payload = JSON.parse(posts[2].body);
    assert(payload.acceptance_criteria.length === 2 && payload.dependencies[1].milestone === "checked_in",
      "Criteria and dependency controls send intake shape");
    assert(doc.querySelector('button[data-item="' + saved.execution.id + '"]'), "Successful create adds queued row");
    assert(win.location.href === url && win.scrollY === y, "Creation preserves URL and scroll");
    await until(() => doc.activeElement === button);
    button.click();
    await unlock();
    assert(form.elements.goal.value === "" && !fields.disabled, "Success prepares a fresh form");
    close();
    const settings = doc.querySelector("#operator-settings");
    const capacity = doc.querySelector("#operator-capacity");
    const capacityStatus = doc.querySelector("#operator-settings-status");
    const capacityForm = doc.querySelector("#operator-settings-form");
    const capacityToken = doc.querySelector("#operator-settings-token");
    const capacityInput = doc.querySelector("#operator-max-writers");
    settings.open = true;
    await until(() => capacity.textContent.includes("3 active / 5 maximum"));
    assert(doc.querySelector('label[for="operator-max-writers"]'), "Concurrency input is labelled");
    capacityInput.value = "2";
    if (!ownerMode) {
      capacityToken.value = "r".repeat(32);
      capacityForm.requestSubmit();
      await until(() => capacityStatus.textContent.includes("HTTP 403"));
      assert(maximum === 5 && capacityToken.value === "", "Runner cannot save; password cleared");
      capacityToken.value = "o".repeat(32);
    }
    capacityForm.requestSubmit();
    await until(() => capacityStatus.textContent === "Concurrency saved.");
    assert(maximum === 2 && capacity.textContent.includes("3 active / 2 maximum"),
      "Owner can lower below current usage and sees authoritative result");
    const changed = JSON.parse(settingsWrites.at(-1).body);
    assert(Object.keys(changed).join() === "max_writers", "Saving does not overwrite pause or aging");
    maximum = 7;
    doc.querySelector("#operator-settings-refresh").click();
    await until(() => capacityInput.value === "7");
    assert(capacity.textContent.includes("3 active / 7 maximum"), "Refresh reads persisted effective capacity");
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
