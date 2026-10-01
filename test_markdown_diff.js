/*
 * Validation for markdown-diff.js — a changed .md file should render as markdown
 * with the changes marked inside the rendered output, not as raw source lines.
 *
 *   node test_markdown_diff.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + '  ' + (detail === undefined ? '' : detail)); }
}

// Load both scripts the way review.html does: marked sets a global, markdown-diff.js
// assigns window.MarkdownDiff.
function load() {
  const sandbox = { window: {} };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  for (const f of ['vendor/marked.min.js', 'js/markdown-diff.js']) {
    new vm.Script(fs.readFileSync(path.join(ROOT, 'static', f), 'utf8')).runInContext(sandbox);
  }
  return { MarkdownDiff: sandbox.window.MarkdownDiff, marked: sandbox.marked };
}

const { MarkdownDiff, marked } = load();
check('markdown-diff.js exposes window.MarkdownDiff', !!MarkdownDiff);
check('vendored marked loads', !!(marked && marked.lexer));
if (!MarkdownDiff || !marked) { console.log(`\n${pass} passed, ${fail} failed`); process.exit(1); }

// Blocks inside a given wrapper class, as HTML strings.
function blocks(html, cls) {
  const re = new RegExp(`<div class="md-block ${cls}"[^>]*>([\\s\\S]*?)</div><!--/md-block-->`, 'g');
  return [...html.matchAll(re)].map(m => m[1]);
}

console.log('reconstructOld');
{
  const newMd = ['# Title', '', 'Intro stays.', '', 'Added para.', '', 'Tail changed.', ''].join('\n');
  const diff = [
    'diff --git a/x.md b/x.md', '--- a/x.md', '+++ b/x.md',
    '@@ -3,5 +3,7 @@',
    ' Intro stays.', ' ', '+Added para.', '+', '-Tail old.', '+Tail changed.', ' ',
  ].join('\n');
  const oldMd = MarkdownDiff.reconstructOld(diff, newMd);
  check('reverse-applies the diff to recover the old file',
    oldMd === ['# Title', '', 'Intro stays.', '', 'Tail old.', ''].join('\n'), JSON.stringify(oldMd));
}

console.log('render');
{
  const oldMd = [
    '# Guide', '',
    'Keep this paragraph.', '',
    'Run the **fast** build before you push.', '',
    'This paragraph is going away.', '',
    '```bash', 'make test', '```', '',
  ].join('\n');
  const newMd = [
    '# Guide', '',
    'Keep this paragraph.', '',
    'Run the **full** build before you push.', '',
    '## New section', '',
    '```bash', 'make test-all', '```', '',
  ].join('\n');
  const html = MarkdownDiff.render(oldMd, newMd, marked);

  check('unchanged blocks render as plain markdown', html.includes('<h1') && html.includes('<p>Keep this paragraph.</p>'));
  check('unchanged blocks are not wrapped', !/md-block[^>]*>\s*<p>Keep this/.test(html));

  const added = blocks(html, 'md-added');
  check('added heading is rendered and marked added', added.some(b => /<h2[^>]*>New section<\/h2>/.test(b)), added);

  const removed = blocks(html, 'md-removed');
  check('removed paragraph is rendered and marked removed', removed.some(b => b.includes('<p>This paragraph is going away.</p>')), removed);

  const modified = blocks(html, 'md-modified');
  const para = modified.find(b => b.includes('build before you push'));
  check('edited paragraph is marked modified', !!para, modified);
  check('changed word is highlighted inside the rendered bold', !!para && para.includes('<strong><span class="md-word-change">full</span></strong>'), para);
  check('unchanged words are not highlighted', !!para && !/md-word-change">(Run|the|build|before)/.test(para), para);

  const code = modified.find(b => b.includes('<pre>'));
  check('edited code block is marked modified, still rendered as <pre>', !!code, modified);
  check('changed token highlighted inside the code block', !!code && code.includes('<span class="md-word-change">test-all</span>'), code);
  check('modified block carries the old source for hover', !!para && /title="[^"]*fast/.test(html.slice(html.indexOf('md-modified'))), '');
}

console.log('added file');
{
  const html = MarkdownDiff.render('', '# New doc\n\nHello.\n', marked);
  check('a brand-new file renders plainly (no per-block wrappers)', html.includes('<h1') && !html.includes('md-block'), html);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
