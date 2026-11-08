/* Minimal, safe Markdown for chat replies.
 * Everything is HTML-escaped first; only the constructs below become markup.
 * Supports fenced code (with a copy button), inline code, headings, bold,
 * italic, links (http/https only), bare URLs, lists, blockquotes, rules.
 */

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function inline(text) {
  const codes = [];
  let s = text.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = esc(s);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, url) =>
    `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
}

export function renderMarkdown(src) {
  const text = String(src || "").replace(/\r\n?/g, "\n");
  const out = [];
  const lines = text.split("\n");
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```\s*([\w+-]*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      const lang = fence[1] ? `<span class="lang">${esc(fence[1])}</span>` : "";
      out.push(`<div class="codeBlock">${lang}<button class="copy" type="button" data-copy="code">คัดลอก</button>` +
               `<pre><code>${esc(body.join("\n"))}</code></pre></div>`);
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      flush();
      out.push(`<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`);
      i++;
      continue;
    }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      flush();
      out.push("<hr>");
      i++;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flush();
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${body.map(inline).join("<br>")}</blockquote>`);
      continue;
    }
    const li = line.match(/^\s*([-*•]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*•]|\d+[.)])\s+(.*)$/);
        if (!m || /\d/.test(m[1]) !== ordered) break;
        items.push(`<li>${inline(m[2])}</li>`);
        i++;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (!line.trim()) {
      flush();
      i++;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out.join("");
}

export function escapeHTML(s) {
  return esc(String(s ?? ""));
}
