// A small, safe markdown renderer.
//
// It never builds HTML strings. It parses the text and creates DOM nodes, and
// every piece of text goes in through a text node, so markup in the input is
// shown as text and can never run. Links are kept only for http(s) URLs.
//
// Supported: ATX headings, paragraphs, hard line breaks (two trailing spaces),
// bold, italic, inline code, fenced code, ordered and unordered lists (nested
// by indentation), blockquotes, horizontal rules, simple pipe tables, and
// [text](url) links.

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const QUOTE = /^ {0,3}>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function el(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

function indentOf(s) {
  let n = 0;
  for (const ch of s) {
    if (ch === " ") n += 1;
    else if (ch === "\t") n += 4;
    else break;
  }
  return n;
}

export function safeUrl(raw) {
  try {
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Inline
// ---------------------------------------------------------------------------

const INLINE = [
  // `code`, also ``co`de``
  { re: /(`+)([\s\S]*?[^`])\1(?!`)/y, make: (m) => { const c = el("code"); c.textContent = m[2].replace(/^ (.*) $/s, "$1"); return c; } },
  // [text](http://url)
  { re: /\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/y, make: (m, inline) => {
    const href = safeUrl(m[2]);
    if (!href) return document.createTextNode(m[0]); // not http(s): show the source as text
    const a = el("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.append(...inline(m[1]));
    return a;
  } },
  // **bold** and __bold__
  { re: /\*\*(?=\S)([\s\S]*?\S)\*\*/y, make: (m, inline) => { const b = el("strong"); b.append(...inline(m[1])); return b; } },
  { re: /__(?=\S)([\s\S]*?\S)__(?!\w)/y, make: (m, inline) => { const b = el("strong"); b.append(...inline(m[1])); return b; }, wordStart: true },
  // *italic* and _italic_ (underscores only at word edges, so snake_case stays)
  { re: /\*(?=[^\s*])([^*\n]*?[^\s*])\*/y, make: (m, inline) => { const i = el("em"); i.append(...inline(m[1])); return i; } },
  { re: /_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/y, make: (m, inline) => { const i = el("em"); i.append(...inline(m[1])); return i; }, wordStart: true },
  // bare https:// links
  { re: /https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/y, make: (m) => {
    const href = safeUrl(m[0]);
    if (!href) return document.createTextNode(m[0]);
    const a = el("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = m[0];
    return a;
  }, wordStart: true },
];

const TRIGGERS = new Set(["`", "[", "*", "_", "h"]);

export function renderInline(text) {
  const out = [];
  let buf = "";
  const flush = () => {
    if (buf) out.push(document.createTextNode(buf));
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    // Hard line break: two spaces or a backslash before a newline.
    if (ch === "\n") {
      if (/ {2,}$/.test(buf) || buf.endsWith("\\")) {
        buf = buf.replace(/(?: {2,}|\\)$/, "");
        flush();
        out.push(el("br"));
      } else {
        buf = buf.replace(/ +$/, "");
        buf += " ";
      }
      i += 1;
      continue;
    }
    if (ch === "\\" && i + 1 < text.length && /[\\`*_[\]()#+\-.!>|]/.test(text[i + 1])) {
      buf += text[i + 1];
      i += 2;
      continue;
    }
    if (TRIGGERS.has(ch)) {
      let hit = null;
      for (const rule of INLINE) {
        if (rule.wordStart && i > 0 && /\w/.test(text[i - 1])) continue;
        rule.re.lastIndex = i;
        const m = rule.re.exec(text);
        if (m) {
          hit = { m, rule };
          break;
        }
      }
      if (hit) {
        flush();
        out.push(hit.rule.make(hit.m, renderInline));
        i += hit.m[0].length;
        continue;
      }
    }
    buf += ch;
    i += 1;
  }
  flush();
  return out;
}

// ---------------------------------------------------------------------------
// Blocks
// ---------------------------------------------------------------------------

function isBlockStart(line) {
  return FENCE.test(line) || HEADING.test(line) || HR.test(line) || ITEM.test(line) || QUOTE.test(line);
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function renderList(items, opts) {
  // items: [{indent, ordered, start, lines: [..], children: []}]
  const frag = [];
  let list = null;
  let kind = null;
  for (const it of items) {
    if (!list || kind !== it.ordered) {
      list = el(it.ordered ? "ol" : "ul");
      if (it.ordered && it.start !== 1) list.start = it.start;
      kind = it.ordered;
      frag.push(list);
    }
    const li = el("li");
    const text = it.lines.join("\n");
    // A blank line inside an item means separate paragraphs.
    const paras = text.split(/\n\s*\n/);
    paras.forEach((p, idx) => {
      if (idx === 0) li.append(...renderInline(p.trim()));
      else {
        const para = el("p");
        para.append(...renderInline(p.trim()));
        li.append(para);
      }
    });
    if (it.children.length) li.append(...renderList(it.children, opts));
    list.append(li);
  }
  return frag;
}

function parseBlocks(lines, opts, depth = 0) {
  const out = [];
  let i = 0;
  const shift = opts.headingShift ?? 1;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    // Fenced code
    let m = FENCE.exec(line);
    if (m) {
      const close = new RegExp(`^ {0,3}${m[1][0] === "`" ? "`" : "~"}{${m[1].length},}\\s*$`);
      const body = [];
      i += 1;
      while (i < lines.length && !close.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence
      const pre = el("pre");
      const code = el("code");
      if (m[2]) code.dataset.lang = m[2];
      code.textContent = body.join("\n");
      pre.append(code);
      out.push(pre);
      continue;
    }

    // Heading
    m = HEADING.exec(line);
    if (m) {
      const level = Math.min(6, m[1].length + shift);
      const h = el(`h${level}`, `md-h${m[1].length}`);
      let text = m[2];
      // A finding title that ends with a severity ("— high") gets a badge.
      const sev = /\s+[—–-]\s+(high|medium|low|critical)\s*$/i.exec(text);
      if (sev && opts.severity !== false) text = text.slice(0, sev.index);
      h.append(...renderInline(text));
      if (sev && opts.severity !== false) {
        const b = el("span", `badge b-${sev[1].toLowerCase() === "critical" ? "high" : sev[1].toLowerCase()}`);
        b.textContent = sev[1].toLowerCase();
        h.append(b);
      }
      out.push(h);
      i += 1;
      continue;
    }

    // Horizontal rule
    if (HR.test(line)) {
      out.push(el("hr"));
      i += 1;
      continue;
    }

    // Blockquote
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && lines[i].trim() && (QUOTE.test(lines[i]) || !isBlockStart(lines[i]))) {
        const q = QUOTE.exec(lines[i]);
        body.push(q ? q[1] : lines[i]);
        i += 1;
      }
      const bq = el("blockquote");
      if (depth < 6) bq.append(...parseBlocks(body, opts, depth + 1));
      out.push(bq);
      continue;
    }

    // List
    m = ITEM.exec(line);
    if (m) {
      const root = { indent: -1, children: [] };
      const stack = [root];
      let last = null;
      while (i < lines.length) {
        const ln = lines[i];
        const im = ITEM.exec(ln);
        if (im && !HR.test(ln)) {
          const indent = indentOf(im[1]);
          while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
          const marker = im[2];
          const ordered = /\d/.test(marker);
          const item = { indent, ordered, start: ordered ? parseInt(marker, 10) : 1, lines: [im[3]], children: [] };
          stack[stack.length - 1].children.push(item);
          stack.push(item);
          last = item;
          i += 1;
          continue;
        }
        if (!ln.trim()) {
          // A blank line ends the list unless the list goes on after it.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j += 1;
          if (j < lines.length && (ITEM.test(lines[j]) || (indentOf(lines[j]) >= 2 && last))) {
            const nextIsItem = ITEM.test(lines[j]);
            if (!nextIsItem && last) last.lines.push("");
            i = j;
            continue;
          }
          break;
        }
        // Continuation: indented text, or lazy text right after an item.
        if (last && (indentOf(ln) > 0 || !isBlockStart(ln))) {
          last.lines.push(ln.trim());
          i += 1;
          continue;
        }
        break;
      }
      out.push(...renderList(root.children, opts));
      continue;
    }

    // Table: a header row, then a separator row.
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const wrap = el("div", "md-table");
      const table = el("table");
      const thead = el("thead");
      const tr = el("tr");
      for (const c of head) {
        const th = el("th");
        th.append(...renderInline(c));
        tr.append(th);
      }
      thead.append(tr);
      table.append(thead);
      const tbody = el("tbody");
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        const row = el("tr");
        for (const c of splitRow(lines[i])) {
          const td = el("td");
          td.append(...renderInline(c));
          row.append(td);
        }
        tbody.append(row);
        i += 1;
      }
      table.append(tbody);
      wrap.append(table);
      out.push(wrap);
      continue;
    }

    // Paragraph
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i]))) {
      if (para.length && lines[i].includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) break;
      para.push(lines[i].replace(/^ {0,3}/, ""));
      i += 1;
    }
    const p = el("p");
    p.append(...renderInline(para.join("\n")));
    out.push(p);
  }
  return out;
}

/** Render markdown text into a new element (default: div.prose). */
export function renderMarkdown(text, opts = {}) {
  const root = el("div", opts.className ?? "prose");
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  root.append(...parseBlocks(lines, opts));
  return root;
}
