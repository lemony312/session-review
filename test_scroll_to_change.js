/*
 * Validation for MarkdownDiff.firstChangeOffset (markdown-diff.js): where to scroll a
 * rendered-markdown container so the first changed block is near its top.
 * Pure logic over a fake container; the real-browser check is test_scroll_to_change.py.
 *
 *   node test_scroll_to_change.js
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

const sandbox = { window: {} };
sandbox.self = sandbox;
vm.createContext(sandbox);
new vm.Script(fs.readFileSync(path.join(__dirname, 'static/js/markdown-diff.js'), 'utf8')).runInContext(sandbox);
const { MarkdownDiff } = sandbox.window;

// A container whose box starts at viewport y=100, scrolled by `scrollTop`, with one
// changed child at content offset `childTop` (so its viewport y = 100 + childTop - scrollTop).
function fake(childTop, scrollTop, selectorHit = true) {
  return {
    scrollTop,
    getBoundingClientRect: () => ({ top: 100 }),
    querySelector: sel => (selectorHit && /md-added/.test(sel) && /md-removed/.test(sel) && /md-modified/.test(sel))
      ? { getBoundingClientRect: () => ({ top: 100 + childTop - scrollTop }) } : null
  };
}

console.log('firstChangeOffset');
check('exported', typeof MarkdownDiff.firstChangeOffset === 'function');
const f = MarkdownDiff.firstChangeOffset;
if (f) {
  check('change far below the fold: scroll to it minus 48px of context', f(fake(2000, 0)) === 1952, f(fake(2000, 0)));
  check('same answer when already scrolled (position-independent)', f(fake(2000, 500)) === 1952, f(fake(2000, 500)));
  check('change near the top: clamp to 0', f(fake(20, 0)) === 0, f(fake(20, 0)));
  check('custom context padding', f(fake(2000, 0), 100) === 1900, f(fake(2000, 0), 100));
  check('no changed block (added file): null, leave at top', f(fake(0, 0, false)) === null, f(fake(0, 0, false)));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
