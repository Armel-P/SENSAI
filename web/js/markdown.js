import { $$, $ } from "./state.js";
import { escapeHTML, toast } from "./ui.js";

const codeHTML = ({ lang, code }) =>
  `<div class="code-wrap"><div class="code-header"><span>${escapeHTML(lang || "code")}</span>` +
  `<button class="copy-code" type="button">Copy code</button></div>` +
  `<pre><code>${escapeHTML(code.replace(/\n$/, ""))}</code></pre></div>`;

function inline(raw) {
  let s = escapeHTML(raw);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>").replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  return s.replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g, (match, prefix, url) => {
    if (s.includes(`href="${url}"`)) return match;
    const clean = url.replace(/[.,!?;:]$/, "");
    return `${prefix}<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean}</a>${url.slice(clean.length)}`;
  });
}

export function renderMarkdown(markdown) {
  const blocks = [];
  const text = String(markdown || "").replace(/\r\n?/g, "\n")
    .replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, code) => {
      blocks.push({ lang: lang.trim(), code });
      return `\u0000BLOCK${blocks.length - 1}\u0000`;
    });

  const out = [];
  let paragraph = [], listType = null, items = [], quote = [];
  const flush = () => {
    if (paragraph.length) out.push(`<p>${inline(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`);
    if (items.length) out.push(`<${listType}>${items.map(i => `<li>${inline(i)}</li>`).join("")}</${listType}>`);
    if (quote.length) out.push(`<blockquote>${quote.map(l => `<p>${inline(l)}</p>`).join("")}</blockquote>`);
    paragraph = []; items = []; quote = []; listType = null;
  };

  for (const line of text.split("\n")) {
    const block = line.match(/^\u0000BLOCK(\d+)\u0000$/);
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const quoted = line.match(/^\s*>\s?(.*)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (block) { flush(); out.push(codeHTML(blocks[+block[1]])); }
    else if (heading) { flush(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); }
    else if (quoted) { if (!quote.length) flush(); quote.push(quoted[1]); }
    else if (!line.trim()) flush();
    else if (bullet || numbered) {
      const type = bullet ? "ul" : "ol";
      if (listType !== type) flush();
      listType = type; items.push((bullet || numbered)[1]);
    } else { if (items.length || quote.length) flush(); paragraph.push(line); }
  }
  flush();
  return out.join("");
}

export function attachCopyButtons(root) {
  for (const button of $$(".copy-code", root)) {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("code", button.closest(".code-wrap"))?.textContent || "");
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy code"; }, 1400);
      } catch (_) { toast("Couldn't access the clipboard in this browser.", "error"); }
    });
  }
}
