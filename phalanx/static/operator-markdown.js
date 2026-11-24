(() => {
  "use strict";

  function node(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }

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
      if (match[1]) parent.append(node("code", match[2]));
      else if (match[4] !== undefined) {
        let url;
        try {
          const parsed = new URL(match[5], document.baseURI);
          if (["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password) url = parsed.href;
        } catch { /* Invalid links remain plain text. */ }
        const link = node(url && !match[3] ? "a" : "span");
        if (link.tagName === "A") {
          link.href = url;
          link.rel = "noreferrer";
        }
        inline(link, match[4], depth + 1);
        parent.append(link);
      } else if (match[6] || match[8]) {
        const emphasis = node(match[6] ? "strong" : "em");
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
        const pre = node("pre");
        pre.append(node("code", code.join("\n")));
        fragment.append(pre);
        continue;
      }
      const heading = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        const title = node("h" + heading[1].length);
        inline(title, heading[2]);
        fragment.append(title);
        continue;
      }
      if (/^ {0,3}(?:-\s*){3,}$|^ {0,3}(?:\*\s*){3,}$/.test(line)) {
        fragment.append(node("hr"));
        continue;
      }
      if (line.includes("|") && i < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i])) {
        i++;
        const table = node("table");
        const head = node("thead");
        const row = node("tr");
        for (const cell of cells(line)) {
          const th = node("th");
          inline(th, cell);
          row.append(th);
        }
        head.append(row);
        table.append(head);
        const body = node("tbody");
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const tr = node("tr");
          for (const cell of cells(lines[i++])) {
            const td = node("td");
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
        const list = node(ordered ? "ol" : "ul");
        if (ordered) list.start = parseInt(bullet[1], 10);
        let entry = bullet;
        while (entry) {
          const li = node("li");
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
        const element = node("blockquote");
        inline(element, quote[1]);
        fragment.append(element);
        continue;
      }
      const paragraph = [line];
      while (i < lines.length && !block(lines[i]) &&
          !(lines[i].includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || ""))) paragraph.push(lines[i++]);
      const p = node("p");
      inline(p, paragraph.join("\n"));
      fragment.append(p);
    }
    return fragment;
  }

  window.PhalanxMarkdown = { render };
})();
