(() => {
  "use strict";

  const { h, safeUrl } = window.PhalanxOperatorHelpers;

  // Only generated elements enter the live DOM. Report HTML is never parsed as HTML.
  function inline(parent, text, depth = 0) {
    if (depth > 8) {
      parent.append(document.createTextNode(text));
      return;
    }
    const tokens = /(`+)([\s\S]*?)\1|<!--[\s\S]*?(?:-->|$)|<\/?[a-zA-Z][^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>|(!?)\[([^\]\n]*)\]\(([^)\s]*)\)|(\*\*|__)(.+?)\6|(\*|_)([^*_\n]+)\8/g;
    let start = 0;
    for (const match of text.matchAll(tokens)) {
      parent.append(document.createTextNode(text.slice(start, match.index)));
      if (match[1]) parent.append(h("code", { text: match[2] }));
      else if (match[4] !== undefined) {
        const url = safeUrl(match[5] || document.baseURI);
        const link = h(url && !match[3] ? "a" : "span");
        if (link.tagName === "A") {
          link.href = url;
          link.rel = "noreferrer";
        }
        inline(link, match[4], depth + 1);
        parent.append(link);
      } else if (match[6] || match[8]) {
        const emphasis = h(match[6] ? "strong" : "em");
        inline(emphasis, match[7] || match[9], depth + 1);
        parent.append(emphasis);
      }
      start = match.index + match[0].length;
    }
    parent.append(document.createTextNode(text.slice(start)));
  }

  function render(markdown) {
    const fragment = document.createDocumentFragment();
    const lines = markdown.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
    const block = line => /^(?:\s*$| {0,3}(?:#{1,6}\s|`{3,}|~{3,}|>|[-+*]\s|\d+\.\s|(?:-\s*){3,}$|(?:\*\s*){3,}$))/.test(line);
    const cells = line => line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
    for (let i = 0; i < lines.length;) {
      const line = lines[i++];
      if (!line.trim()) continue;
      const fence = /^ {0,3}(`{3,}|~{3,})/.exec(line);
      if (fence) {
        const code = [];
        const end = new RegExp("^ {0,3}" + fence[1][0] + "{" + fence[1].length + ",}\\s*$");
        while (i < lines.length && !end.test(lines[i])) code.push(lines[i++]);
        if (i < lines.length) i++;
        const pre = h("pre");
        pre.append(h("code", { text: code.join("\n") }));
        fragment.append(pre);
        continue;
      }
      const heading = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        const title = h("h" + heading[1].length);
        inline(title, heading[2]);
        fragment.append(title);
        continue;
      }
      if (/^ {0,3}(?:-\s*){3,}$|^ {0,3}(?:\*\s*){3,}$/.test(line)) {
        fragment.append(h("hr"));
        continue;
      }
      if (line.includes("|") && i < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i])) {
        i++;
        const table = h("table");
        const head = h("thead");
        const row = h("tr");
        for (const cell of cells(line)) {
          const th = h("th");
          inline(th, cell);
          row.append(th);
        }
        head.append(row);
        table.append(head);
        const body = h("tbody");
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const tr = h("tr");
          for (const cell of cells(lines[i++])) {
            const td = h("td");
            inline(td, cell);
            tr.append(td);
          }
          body.append(tr);
        }
        table.append(body);
        fragment.append(table);
        continue;
      }
      const bullet = /^ {0,3}([-+*]|\d+\.)\s+(.+)$/.exec(line);
      if (bullet) {
        const ordered = /\d/.test(bullet[1]);
        const list = h(ordered ? "ol" : "ul");
        if (ordered) list.start = parseInt(bullet[1], 10);
        let entry = bullet;
        while (entry) {
          const li = h("li");
          inline(li, entry[2]);
          list.append(li);
          entry = i < lines.length ? /^ {0,3}([-+*]|\d+\.)\s+(.+)$/.exec(lines[i]) : null;
          if (!entry || /\d/.test(entry[1]) !== ordered) break;
          i++;
        }
        fragment.append(list);
        continue;
      }
      const quote = /^ {0,3}>\s?(.*)$/.exec(line);
      if (quote) {
        const element = h("blockquote");
        inline(element, quote[1]);
        fragment.append(element);
        continue;
      }
      const paragraph = [line];
      while (i < lines.length && !block(lines[i]) &&
          !(lines[i].includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || ""))) paragraph.push(lines[i++]);
      const p = h("p");
      inline(p, paragraph.join("\n"));
      fragment.append(p);
    }
    return fragment;
  }

  window.PhalanxMarkdown = { render };
})();
