/*
 * Validation for the "Show N more lines" expand buttons in review.js
 * (parseDiffInline and parseDiff/traditional).
 *
 *   node test_inline_expand.js
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

// Load split-diff.js then review.js the way review.html does, exposing the two parsers.
function load() {
  const sandbox = {
    window: {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => new Promise(() => {}),
    document: {
      readyState: 'loading',
      addEventListener() {},
      createElement: () => ({
        set textContent(v) { this._t = String(v); },
        get innerHTML() { return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
      })
    }
  };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  const dir = path.join(__dirname, 'static', 'js');
  new vm.Script(fs.readFileSync(path.join(dir, 'split-diff.js'), 'utf8')).runInContext(sandbox);
  const src = fs.readFileSync(path.join(dir, 'review.js'), 'utf8');
  const patched = src.replace(/\}\)\(\);\s*$/, 'window.__t={parseDiffInline,parseDiff};})();');
  if (patched === src) throw new Error('review.js does not end with the expected IIFE close');
  new vm.Script(patched).runInContext(sandbox);
  return sandbox.window.__t;
}

const { parseDiffInline, parseDiff } = load();

const FULL = Array.from({ length: 10 }, (_, i) => 'line' + (i + 1)).join('\n') + '\n';
const D = (...lines) => ['diff --git a/f b/f', '--- a/f', '+++ b/f', ...lines].join('\n');

// Returns [{dir, start, end, label}] for every expand button in the output.
function buttons(html) {
  const out = [];
  const re = /data-direction="(\w+)"\s+data-start-line="(\d+)"\s+data-end-line="(\d+)">\s*[^\d<]*?(\d+) more lines/g;
  let m;
  while ((m = re.exec(html))) out.push({ dir: m[1], start: +m[2], end: +m[3], label: +m[4] });
  return out;
}
const fmt = bs => bs.map(b => `${b.dir}:${b.start}-${b.end}`).join(' ') || '(none)';

const cases = [
  { name: 'hunk at lines 4-5',
    diff: D('@@ -4,2 +4,2 @@', '-old4', '-old5', '+new4', '+new5'),
    want: ['before:1-3', 'after:6-10'] },
  { name: 'hunk at end of file',
    diff: D('@@ -9,2 +9,2 @@', ' line9', '-old10', '+line10'),
    want: ['before:1-8'] },
  { name: 'pure deletion (@@ -4,1 +3,0 @@)',
    diff: D('@@ -4,1 +3,0 @@', '-old4'),
    want: ['before:1-3', 'after:4-10'] },
];

for (const [fnName, fn] of [['parseDiffInline', parseDiffInline], ['parseDiff', parseDiff]]) {
  console.log(`\n${fnName}`);
  for (const c of cases) {
    const bs = buttons(fn(c.diff, FULL, 1, [], 'text', []));
    check(c.name + ': ' + c.want.join(' '), fmt(bs) === c.want.join(' '), 'got ' + fmt(bs));
    check(c.name + ': labels match ranges', bs.every(b => b.label === b.end - b.start + 1),
      bs.map(b => `${b.label}!=${b.end - b.start + 1}`).join(' '));
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
