(() => {
  "use strict";

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

  function safeUrl(value, { httpsOnly = false } = {}) {
    if (!value) return null;
    try {
      const url = httpsOnly ? new URL(value) : new URL(value, document.baseURI);
      if ((httpsOnly ? url.protocol === "https:" : ["http:", "https:"].includes(url.protocol)) &&
          !url.username && !url.password) return url.href;
    } catch {
      // Missing or invalid link.
    }
    return null;
  }

  function svg(tag, props = {}, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(props)) node.setAttribute(key, value);
    node.append(...children);
    return node;
  }

  const REQUEST_TIMEOUT_MS = 12000;
  const readSignal = signal => signal
    ? AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)])
    : AbortSignal.timeout(REQUEST_TIMEOUT_MS);

  async function copyText(text) {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(text);
  }

  window.PhalanxOperatorHelpers = { h, svg, safeUrl, readSignal, copyText };
})();
