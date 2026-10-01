/*
 * Validation for split-diff.js — the side-by-side diff row model and its HTML.
 *
 *   node test_split_diff.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + '  ' + (detail === undefined ? '' : detail)); }
}

// Load the script the way review.html does: it assigns window.SplitDiff.
function load() {
  const sandbox = { window: {} };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  const file = path.join(__dirname, 'static', 'js', 'split-diff.js');
  new vm.Script(fs.readFileSync(file, 'utf8')).runInContext(sandbox);
  return sandbox.window.SplitDiff;
}

const SplitDiff = load();
check('split-diff.js exposes window.SplitDiff', !!SplitDiff);
if (!SplitDiff) { console.log(`\n${pass} passed, ${fail} failed`); process.exit(1); }

const D = (...lines) => ['diff --git a/f b/f', '--- a/f', '+++ b/f', ...lines].join('\n');
const lineRows = rows => rows.filter(r => r.type === 'line');
const cells = trHtml => trHtml.split('<td').slice(1).map(c => '<td' + c);
const trs = (html, prefix) => {
  const re = new RegExp('<tr class="' + prefix + '[^>]*>[\\s\\S]*?</tr>', 'g');
  return html.match(re) || [];
};

console.log('\n1. Exports');
check('COLSPAN is 6', SplitDiff.COLSPAN === 6);
for (const name of ['parseHunks', 'computeWordDiff', 'pairHunkLines', 'buildRows', 'render',
                    'renderContextRow', 'renderWordDiffSides', 'wrapTable', 'escapeHtml']) {
  check('exports ' + name, typeof SplitDiff[name] === 'function');
}

console.log('\n2. parseHunks');
{
  const h = SplitDiff.parseHunks(['@@ -1,3 +1,4 @@ ctx', ' a']);
  check('header fields', h.length === 1 && h[0].oldStart === 1 && h[0].oldCount === 3 &&
    h[0].newStart === 1 && h[0].newCount === 4 && h[0].context === 'ctx', JSON.stringify(h));
  const h1 = SplitDiff.parseHunks(['@@ -5 +5 @@', ' a']);
  check('absent counts default to 1', h1[0].oldCount === 1 && h1[0].newCount === 1, JSON.stringify(h1));
  const h2 = SplitDiff.parseHunks(['@@ -1,2 +1,2 @@', '-----', '++++x', ' a']);
  check('deleted "----" is kept', h2[0].lines[0].type === 'deletion' && h2[0].lines[0].content === '----',
    JSON.stringify(h2[0].lines));
  check('added "+++x" is kept', h2[0].lines[1].type === 'addition' && h2[0].lines[1].content === '+++x',
    JSON.stringify(h2[0].lines));
  const h3 = SplitDiff.parseHunks(['diff --git a/f b/f', '--- a/f', '+++ b/f', '@@ -1 +1 @@', '-a', '+b', '\\ No newline at end of file']);
  check('file headers and "\\ No newline" ignored', h3.length === 1 && h3[0].lines.length === 2, JSON.stringify(h3));
}

console.log('\n3. buildRows alignment');
{
  let r = lineRows(SplitDiff.buildRows(D('@@ -1,2 +1,1 @@', '-a', '-b', '+A'), { fileId: 1 }));
  check('-a -b +A: change then del', r.length === 2 && r[0].kind === 'change' && r[0].oldText === 'a' &&
    r[0].newText === 'A' && r[0].oldNum === 1 && r[0].newNum === 1 &&
    r[1].kind === 'del' && r[1].oldText === 'b' && r[1].oldNum === 2 && r[1].newNum === null, JSON.stringify(r));
  r = lineRows(SplitDiff.buildRows(D('@@ -1,1 +1,3 @@', '-a', '+A', '+B', '+C'), { fileId: 1 }));
  check('-a +A +B +C: change then two adds', r.length === 3 && r[0].kind === 'change' &&
    r[1].kind === 'add' && r[2].kind === 'add' && r[1].oldNum === null && r[2].oldNum === null &&
    r[1].newNum === 2 && r[2].newNum === 3, JSON.stringify(r));
  r = lineRows(SplitDiff.buildRows(D('@@ -1,3 +1,3 @@', '-a', '+A', ' mid', '-b', '+B'), { fileId: 1 }));
  check('context between blocks', r.length === 3 && r[1].kind === 'context' && r[1].oldText === 'mid' &&
    r[1].newText === 'mid' && r[1].oldNum === 2 && r[1].newNum === 2, JSON.stringify(r));
  r = lineRows(SplitDiff.buildRows(D('@@ -1,2 +1,2 @@', '-a', '+A', '-b', '+B'), { fileId: 1 }));
  check('interleaved -a +A -b +B gives two change rows', r.length === 2 && r.every(x => x.kind === 'change'), JSON.stringify(r));
}

console.log('\n4. Numbering across hunks');
{
  const r = lineRows(SplitDiff.buildRows(D('@@ -1,2 +1,3 @@', ' a', '+x', ' b', '@@ -10,2 +11,2 @@', ' c', '-d', '+e'), { fileId: 1 }));
  const first2 = r.find(x => x.oldText === 'c');
  check('second hunk starts at old 10 / new 11', first2 && first2.oldNum === 10 && first2.newNum === 11, JSON.stringify(first2));
}

console.log('\n5. Expand rows');
{
  const expands = rows => rows.filter(r => r.type === 'expand');
  let rows = SplitDiff.buildRows(D('@@ -3,2 +3,3 @@ ctx', ' c1', '-d', '+D', '+E', '@@ -10,1 +11,1 @@', '-x', '+y'),
    { fileId: 1, fullLineCount: 20 });
  let e = expands(rows);
  check('three expand rows', e.length === 3, JSON.stringify(e));
  check('before: 1-2, oldStart 1', e[0].direction === 'before' && e[0].startLine === 1 && e[0].endLine === 2 && e[0].oldStart === 1, JSON.stringify(e[0]));
  check('gap: 6-10, oldStart 5', e[1].startLine === 6 && e[1].endLine === 10 && e[1].oldStart === 5, JSON.stringify(e[1]));
  check('after: 12-20, oldStart 11', e[2].direction === 'after' && e[2].startLine === 12 && e[2].endLine === 20 && e[2].oldStart === 11, JSON.stringify(e[2]));

  rows = SplitDiff.buildRows(D('@@ -3,2 +2,0 @@', '-a', '-b'), { fileId: 1, fullLineCount: 5 });
  e = expands(rows);
  check('pure deletion: before [1,2] oldStart 1', e[0].startLine === 1 && e[0].endLine === 2 && e[0].oldStart === 1, JSON.stringify(e[0]));
  check('pure deletion: trailing startLine 3, oldStart 5', e[1] && e[1].startLine === 3 && e[1].oldStart === 5, JSON.stringify(e[1]));

  rows = SplitDiff.buildRows(D('@@ -2,0 +3,2 @@', '+a', '+b'), { fileId: 1, fullLineCount: 8 });
  e = expands(rows);
  check('pure addition: before [1,2] oldStart 1', e[0].startLine === 1 && e[0].endLine === 2 && e[0].oldStart === 1, JSON.stringify(e[0]));
  check('pure addition: trailing startLine 5, oldStart 3', e[1] && e[1].startLine === 5 && e[1].oldStart === 3, JSON.stringify(e[1]));

  rows = SplitDiff.buildRows(D('@@ -3,1 +3,1 @@', '-a', '+b'), { fileId: 1, fullLineCount: 0 });
  check('fullLineCount 0 gives no expand rows', expands(rows).length === 0);
}

console.log('\n6. Word diff, both sides');
{
  const w = SplitDiff.renderWordDiffSides('const x = 1;', 'const x = 2;');
  check('old side has word-del', w && w.oldHtml.includes('<span class="word-del">1;</span>'), w && w.oldHtml);
  check('new side has word-add', w && w.newHtml.includes('<span class="word-add">2;</span>'), w && w.newHtml);
  check('no inline popover data', w && !w.oldHtml.includes('data-old-text') && !w.newHtml.includes('data-old-text'));
  check('dissimilar lines return null', SplitDiff.renderWordDiffSides('aaaa bbbb cccc', 'xxxx yyyy zzzz') === null);
  const e = SplitDiff.renderWordDiffSides('x = <a> + y', 'x = <b> + y');
  check('escapes markup', e && e.oldHtml.includes('&lt;') && !e.oldHtml.includes('<a>') && !e.newHtml.includes('<b>'), e && e.oldHtml);
}

console.log('\n7. Rendered HTML');
{
  const opts = { fileId: 1, highlight: SplitDiff.escapeHtml };
  const rows = SplitDiff.buildRows(D('@@ -1,4 +1,4 @@', ' a', ' b', ' c', ' d', '@@ -9,4 +9,5 @@ ctx', ' keep', '-old1', '-old2', '+new1', '+new2', '+new3', ' z'),
    { fileId: 1, fullLineCount: 30 });
  const html = SplitDiff.render(rows, opts);
  const lines = trs(html, 'diff-line').filter(l => !l.includes('split-context'));
  check('has changed line rows', lines.length === 3, String(lines.length));
  check('every line row has exactly 6 cells', trs(html, 'diff-line').every(l => cells(l).length === 6));

  const delHtml = trs(SplitDiff.render(SplitDiff.buildRows(D('@@ -1,2 +1,1 @@', '-a', '-b', '+A'), { fileId: 1 }), opts), 'diff-line');
  const dc = cells(delHtml[1]);
  check('del row: right side is three split-empty cells', dc.slice(3).every(c => c.includes('split-empty')), dc.slice(3).join('|'));
  check('del row: right side has no <code>', !dc.slice(3).join('').includes('<code>'));
  check('del row: left side has code', dc[2].includes('<code>b</code>'));

  const addHtml = trs(SplitDiff.render(SplitDiff.buildRows(D('@@ -1,1 +1,2 @@', '-a', '+A', '+B'), { fileId: 1 }), opts), 'diff-line');
  const ac = cells(addHtml[1]);
  check('add row: left side is split-empty', ac.slice(0, 3).every(c => c.includes('split-empty')));
  check('add row: right side is split-add', ac.slice(3).every(c => c.includes('split-add')));

  const cc = cells(lines[0]);
  check('change row: left split-del, right split-add', cc.slice(0, 3).every(c => c.includes('split-del')) &&
    cc.slice(3).every(c => c.includes('split-add')));
  check('change row gutters are - and +', />-<\/td>/.test(cc[1]) && />\+<\/td>/.test(cc[4]), cc[1] + cc[4]);

  const hunkRows = trs(html, 'hunk-header');
  const ctxHunk = hunkRows.find(r => r.includes('ctx'));
  check('hunk header is a single colspan=6 cell', hunkRows.length === 2 && ctxHunk && cells(ctxHunk).length === 1 &&
    ctxHunk.includes('colspan="6"'), hunkRows.join('\n'));
  const exp = html.match(/<button class="expand-context-btn"[^>]*>/g) || [];
  check('expand buttons carry data-old-start', exp.length === 2 && exp[0].includes('data-old-start="5"'), exp.join('\n'));
  check('expand rows use colspan=6', trs(html, 'expand-context-row').every(r => r.includes('colspan="6"')));
}

console.log('\n8. Annotations');
{
  const diff = D('@@ -1,2 +1,2 @@', ' a', '-b', '+B');
  const ann = { id: 9, line_start: 2, annotation_type: 'note' };
  const opts = {
    fileId: 1, annotations: [ann], highlight: SplitDiff.escapeHtml,
    isExpanded: id => id === 9,
    renderAnnotationCard: (a, c) => '<tr class="annotation-card-row" data-c="' + c + '"></tr>'
  };
  const html = SplitDiff.render(SplitDiff.buildRows(diff, { fileId: 1 }), opts);
  const changeRow = trs(html, 'diff-line').find(r => r.includes('split-del'));
  const c = cells(changeRow);
  check('marker is in the new-side content cell', c[5].includes('data-annotation-id="9"') && c[5].includes('line-content new'), c[5]);
  check('no marker on the old side', !c.slice(0, 3).join('').includes('annotation-marker'));
  check('card follows the row with colspan 6', html.includes(changeRow + '<tr class="annotation-card-row" data-c="6">'), html.slice(-200));

  const oldOnly = SplitDiff.render(SplitDiff.buildRows(D('@@ -1,3 +1,2 @@', ' a', ' b', '-c'), { fileId: 1 }),
    { fileId: 1, annotations: [{ id: 5, line_start: 3, annotation_type: 'note' }], highlight: SplitDiff.escapeHtml });
  check('old-only line number gets no marker', !oldOnly.includes('annotation-marker'));
}

console.log('\n9. Whitespace hunk');
{
  const rows = SplitDiff.buildRows(D('@@ -1 +1 @@', '-  foo', '+    foo'), { fileId: 7 });
  const html = SplitDiff.render(rows, { fileId: 7, highlight: SplitDiff.escapeHtml });
  check('model has a ws-hunk', rows.some(r => r.type === 'ws-hunk' && r.hunkId === 'ws-split-7-1'));
  check('one collapsed row with the hunk id', (html.match(/<tr class="whitespace-hunk-collapsed" data-hunk-id="ws-split-7-1"/g) || []).length === 1, html);
  check('collapsed row is colspan 6', /whitespace-hunk-collapsed[\s\S]*?colspan="6"/.test(html));
  check('wrapper holds a nested split table with colgroup',
    html.includes('<table class="diff-table diff-table-split whitespace-hunk-content"><colgroup>'));
  const inner = trs(html, 'diff-line');
  check('inner changed row has word-del / word-add', inner.length === 1 && inner[0].includes('word-del') && inner[0].includes('word-add'), inner[0]);
}

console.log('\n10. Move banners');
{
  const sections = [{ source: 'a/Old.scala', similarity: 91, target_start: 3, target_end: 4 }];
  const rows = SplitDiff.buildRows(D('@@ -1,2 +1,4 @@', ' a', ' b', '+c', '+d'), { fileId: 1, moveSections: sections });
  const idx = rows.findIndex(r => r.type === 'move');
  check('move row precedes the newNum 3 row', idx >= 0 && rows[idx + 1].type === 'line' && rows[idx + 1].newNum === 3, JSON.stringify(rows));
  const html = SplitDiff.render(rows, { fileId: 1, highlight: SplitDiff.escapeHtml });
  const banners = trs(html, 'move-section-banner');
  check('exactly one banner, colspan 6, names the source', banners.length === 1 && banners[0].includes('colspan="6"') &&
    banners[0].includes('a/Old.scala'), banners[0]);
}

console.log('\n11. renderContextRow');
{
  const html = SplitDiff.renderContextRow(5, 7, 'x', true);
  check('expanded class', html.includes('split-context-expanded'));
  check('six cells', cells(html).length === 6);
  check('old and new numbers', /line-num old[^>]*>5</.test(html) && /line-num new[^>]*>7</.test(html), html);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
