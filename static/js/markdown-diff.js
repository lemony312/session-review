/*
 * Rendered-markdown diff: shows a changed .md file as rendered markdown, with
 * added / removed / modified blocks marked and changed words highlighted inside
 * the rendered HTML.
 *
 * DOM-free (returns HTML strings) so test_markdown_diff.js can run it under node.
 */
(function () {
  'use strict';

  // The review only stores the new file and the unified diff; walk the hunks
  // backwards over the new file to get the old one.
  function reconstructOld(diffText, newContent) {
    const newLines = newContent.split('\n');
    const out = [];
    let n = 0; // index into newLines (0-based)
    let inHunk = false;

    for (const line of diffText.split('\n')) {
      const m = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        // A zero-length new side (pure deletion) reports the line *before* it.
        const start = Math.max(parseInt(m[1], 10) - 1, 0);
        const hunkNewCount = line.match(/\+\d+,0 @@/) ? start + 1 : start;
        while (n < hunkNewCount && n < newLines.length) out.push(newLines[n++]);
        inHunk = true;
        continue;
      }
      if (!inHunk || line.startsWith('\\')) continue;
      if (line.startsWith('+')) n++;
      else if (line.startsWith('-')) out.push(line.substring(1));
      else if (line.startsWith(' ')) { out.push(line.substring(1)); n++; }
    }
    while (n < newLines.length) out.push(newLines[n++]);
    return out.join('\n');
  }

  function escapeAttr(s) {
    return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Generic LCS over two arrays; returns ops [{type:'equal'|'delete'|'insert', a?, b?}].
  function lcsOps(a, b, eq) {
    const m = a.length, n = b.length;
    const dp = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
    for (let i = m - 1; i >= 0; i--) {
      for (let j = n - 1; j >= 0; j--) {
        dp[i][j] = eq(a[i], b[j]) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0, j = 0;
    while (i < m || j < n) {
      if (i < m && j < n && eq(a[i], b[j])) { ops.push({ type: 'equal', a: a[i], b: b[j] }); i++; j++; }
      else if (i < m && (j === n || dp[i + 1][j] >= dp[i][j + 1])) { ops.push({ type: 'delete', a: a[i] }); i++; }
      else { ops.push({ type: 'insert', b: b[j] }); j++; }
    }
    return ops;
  }

  // Split rendered HTML into tags and text, so word highlighting only touches text.
  function textRuns(html) {
    const parts = html.split(/(<[^>]*>)/);
    let text = '';
    const runs = parts.map(p => {
      if (p.startsWith('<')) return { tag: p };
      const run = { text: p, start: text.length };
      text += p;
      return run;
    });
    return { runs, text };
  }

  function tokens(text) {
    const out = [];
    const re = /\S+|\s+/g;
    let m;
    while ((m = re.exec(text))) out.push({ s: m[0], start: m.index, end: m.index + m[0].length });
    return out;
  }

  const MAX_WORD_CELLS = 4e6;

  // Wrap words of newHtml that aren't in oldHtml. Tags are left untouched.
  function highlightChangedWords(oldHtml, newHtml) {
    const oldT = tokens(textRuns(oldHtml).text).filter(t => t.s.trim());
    const { runs, text } = textRuns(newHtml);
    const newT = tokens(text).filter(t => t.s.trim());
    if (oldT.length * newT.length > MAX_WORD_CELLS) return newHtml;

    const ranges = lcsOps(oldT, newT, (x, y) => x.s === y.s)
      .filter(op => op.type === 'insert')
      .map(op => [op.b.start, op.b.end]);
    if (!ranges.length) return newHtml;

    return runs.map(r => {
      if (r.tag !== undefined) return r.tag;
      const end = r.start + r.text.length;
      let out = '', pos = r.start;
      for (const [s, e] of ranges) {
        if (e <= r.start || s >= end) continue;
        const cs = Math.max(s, r.start), ce = Math.min(e, end);
        out += r.text.slice(pos - r.start, cs - r.start);
        out += `<span class="md-word-change">${r.text.slice(cs - r.start, ce - r.start)}</span>`;
        pos = ce;
      }
      return out + r.text.slice(pos - r.start);
    }).join('');
  }

  function blocksOf(md, marked) {
    return marked.lexer(md).filter(t => t.type !== 'space')
      .map(t => ({ type: t.type, raw: t.raw.replace(/\s+$/, '') }));
  }

  // Dice coefficient over word sets: an edit if the blocks share most of their words.
  function similar(a, b) {
    if (a.type !== b.type) return false;
    const wa = new Set(a.raw.split(/\s+/)), wb = new Set(b.raw.split(/\s+/));
    let common = 0;
    for (const w of wa) if (wb.has(w)) common++;
    return (2 * common) / (wa.size + wb.size) >= 0.4;
  }

  function wrap(cls, inner, title) {
    const t = title ? ` title="${escapeAttr(title)}"` : '';
    return `<div class="md-block ${cls}"${t}>${inner}</div><!--/md-block-->`;
  }

  function render(oldMd, newMd, marked) {
    if (!oldMd.trim()) return marked.parse(newMd);

    const parse = raw => marked.parse(raw);
    const ops = lcsOps(blocksOf(oldMd, marked), blocksOf(newMd, marked), (x, y) => x.raw === y.raw);

    let html = '';
    for (let k = 0; k < ops.length;) {
      if (ops[k].type === 'equal') { html += parse(ops[k].b.raw); k++; continue; }

      // A run of changes: pair each deletion with a later, similar insertion as an
      // edit (like the line view); the rest are plain removals / additions.
      const dels = [], ins = [];
      while (k < ops.length && ops[k].type !== 'equal') {
        if (ops[k].type === 'delete') dels.push(ops[k].a); else ins.push(ops[k].b);
        k++;
      }
      let next = 0; // first insertion not yet emitted
      for (const d of dels) {
        let match = -1;
        for (let j = next; j < ins.length; j++) if (similar(d, ins[j])) { match = j; break; }
        if (match < 0) { html += wrap('md-removed', parse(d.raw)); continue; }
        for (; next < match; next++) html += wrap('md-added', parse(ins[next].raw));
        html += wrap('md-modified', highlightChangedWords(parse(d.raw), parse(ins[match].raw)), `Before:\n${d.raw}`);
        next = match + 1;
      }
      for (; next < ins.length; next++) html += wrap('md-added', parse(ins[next].raw));
    }
    return html;
  }

  // Scroll offset that puts the container's first changed block near its top, with
  // `pad` px of context above; null when nothing is marked (e.g. a brand-new file).
  function firstChangeOffset(container, pad) {
    const first = container.querySelector('.md-added, .md-removed, .md-modified');
    if (!first) return null;
    const p = pad == null ? 48 : pad;
    const y = first.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    return Math.max(0, Math.round(y - p));
  }

  window.MarkdownDiff = { reconstructOld, render, firstChangeOffset };
})();
