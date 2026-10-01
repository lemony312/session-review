/*
 * Validation for pipeline-diff.js — asserts the rendered HTML actually shows the
 * embedded bash, rather than merely containing it somewhere behind a click.
 *
 * Runs the real renderer (no DOM needed: render() returns a string) against
 * payloads built from the synthetic fixtures in tests/fixtures/pipelines by
 * preview_pipeline_diff.py.
 *
 *   node test_pipeline_render.js
 */
'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const FIXTURES = path.join(ROOT, 'tests', 'fixtures', 'pipelines');

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + '  ' + (detail === undefined ? '' : detail)); }
}

// Load pipeline-diff.js the way a browser does: it assigns window.PipelineDiff.
function loadRenderer() {
  const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'pipeline-diff.js'), 'utf8');
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  new vm.Script(src).runInContext(sandbox);
  return sandbox.window.PipelineDiff;
}

function payloadFor(name) {
  const out = path.join(os.tmpdir(), `pp-${name}.json`);
  execFileSync('python3', [
    path.join(ROOT, 'preview_pipeline_diff.py'),
    path.join(FIXTURES, `${name}.old.pp`), path.join(FIXTURES, `${name}.new.pp`),
    '--json', out, '--html', path.join(os.tmpdir(), `pp-${name}.html`),
  ], { stdio: 'pipe' });
  return JSON.parse(fs.readFileSync(out, 'utf8'));
}

const PD = loadRenderer();
const render = (payload) => PD.render({ pipeline_diff: JSON.stringify(payload) });

// The body of a stage card, i.e. what is on screen before any click.
function stageBodies(html) {
  const out = [];
  const re = /<div class="pd-stage-body"([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) out.push({ attrs: m[1], from: m.index });
  return out;
}

// --- Case 1: a bash-only edit (shell-edit fixture) ---
console.log('Case 1: shell-edit — the bash is on screen unprompted');
const notify = payloadFor('shell-edit');
let html = render(notify);
check('shell block rendered', html.includes('class="pd-shell"'));
check('stage bodies are not hidden',
      stageBodies(html).length > 0 && stageBodies(html).every(b => !/hidden/.test(b.attrs)),
      stageBodies(html).map(b => b.attrs));
check('shell block sits inside an open stage body',
      html.indexOf('class="pd-shell"') > html.indexOf('<div class="pd-stage-body">'));
check('script location shown', html.includes('pd-shell-loc') && html.includes('command[2]'));
check('folded runs are clickable', /class="pd-shell-gap" data-gap="/.test(html));
check('full-script escape hatch offered', html.includes('data-expand-all'));
check('changed span highlighted within the line', html.includes('class="pd-ch"'));
check('shell line totals in the summary', /shell edits.*\+\d+ −\d+ lines/.test(html));

// No wall of JSON in the rows *about* the change. Script lines are exempt: a
// one-line JSON payload inside the bash is the code under review, not noise about
// it. Hidden nodes (copy source, show-all values) are exempt for the same reason.
function longestTextRun(s) {
  return s
    .replace(/<code class="pd-shell-code">[\s\S]*?<\/code>/g, "")
    .replace(/<textarea[^>]*>[\s\S]*?<\/textarea>/g, "")
    .replace(/<span class="pd-more-full" hidden>[\s\S]*?<\/span>/g, "")
    .replace(/<title>[\s\S]*?<\/title>/g, "")
    .replace(/<[^>]*>/g, "\n").split(/\s+/)
    .reduce(function (max, t) { return Math.max(max, t.trim().length); }, 0);
}
check('no unreadable JSON run', longestTextRun(html) <= 400, longestTextRun(html));

// --- Case 2: a CronJob deploy under manifests[] (cronjob fixture) ---
console.log('Case 2: cronjob — manifests[]/CronJob edits are legible');
const cron = payloadFor('cronjob');
html = render(cron);
check('env var named in a field row', html.includes('MAX_TIME'));
check('kubernetes boilerplate compacted out of the path',
      html.includes('› containers[') && !html.includes('jobTemplate ›'),
      'path segments not compacted');
check('no unreadable JSON run', longestTextRun(html) <= 400, longestTextRun(html));
check('stage bodies are not hidden',
      stageBodies(html).every(b => !/hidden/.test(b.attrs)));

// --- Case 3: a stage whose config rows are numerous folds them, not the script ---
console.log('Case 3: many config rows fold behind a labelled count');
const manyRows = {
  schemaVersion: '1.0', pipeline: { name: 'p' }, fileChangeType: 'modified',
  summary: { stagesChanged: 1, shellEdits: 1, shellLinesAdded: 1, shellLinesRemoved: 1,
             topSeverity: 'critical', bulkChanges: [] },
  meta: [], parameters: [], triggers: [], notifications: [], expectedArtifacts: [],
  dag: { edgesAdded: [], edgesRemoved: [], rootsAdded: [], rootsRemoved: [] },
  stages: [{
    identity: 'S', displayName: 'S', changeType: 'changed', changeCategories: ['shell-change'],
    severity: 'critical', old: { type: 'runJobManifest' }, new: { type: 'runJobManifest' },
    fieldChanges: Array.from({ length: 12 }, (_, i) => ({
      path: `manifest.spec.template.spec.containers[c].env[V${i}].value`,
      category: 'env-change', severity: 'major', changeType: 'changed', old: String(i), new: String(i + 1),
    })),
    shellDiffs: [{ container: 'c', manifest: 'Job', location: 'command[2]', changed: true,
                   old: 'echo a\n', new: 'echo b\n', hunks: [] }],
    lintFlags: [],
  }],
};
html = render(manyRows);
check('config rows folded', html.includes('data-fold') && html.includes('12 config changes'));
check('script still open', html.includes('class="pd-shell"')
      && html.indexOf('class="pd-shell"') < html.indexOf('data-fold'));

// --- Case 4: added/removed stages (legs fixture) show what is in them ---
console.log('Case 4: legs — an added stage is not just a title bar');
const legs = payloadFor('legs');
html = render(legs);
fs.writeFileSync(path.join(os.tmpdir(), 'render-legs.html'), html);
// Every stage card that reports a change must say something under its heading.
const cards = html.split('<div class="pd-stage pd-stage-').slice(1);
const bodyless = cards.filter(c => !c.includes('pd-stage-body'))
                      .map(c => (c.match(/pd-stage-name">([^<]*)/) || [])[1]);
check('no change is reported as a bare title bar', bodyless.length === 0, bodyless);
check('added stage shows its schedule', /schedule/.test(html));
check('added stage shows its image', /image/.test(html) && /registry\.example\.com/.test(html));
check('added stage shows env by name', html.includes('env['));
check('added stage shows the SpEL that gates it', html.includes('stageEnabled'));
check('one-sided rows fold under a "settings" label',
      html.includes('settings</div>') || html.includes('settings'), 'no settings label');
check('no unreadable JSON run', longestTextRun(html) <= 400, longestTextRun(html));

// --- Case 5: a wholly-new script is capped, not dumped ---
console.log('Case 5: a brand-new 200-line script is capped, with the rest one click away');
const bigScript = Array.from({ length: 200 }, (_, i) => `echo line ${i}`).join('\n') + '\n';
const newStage = JSON.parse(JSON.stringify(manyRows));
newStage.stages[0].changeType = 'added';
newStage.stages[0].changeCategories = ['stage-added'];
newStage.stages[0].fieldChanges = [];
newStage.stages[0].shellDiffs = [{ container: 'c', manifest: 'CronJob/x', location: 'command[2]',
                                   changed: true, old: '', new: bigScript, hunks: [] }];
html = render(newStage);
// On screen = outside the hidden fold blocks. Folds are flat siblings, so cutting
// each fold's contents out leaves exactly what a reader sees before clicking.
const onScreen = html.replace(/<div class="pd-shell-fold"[^>]*>[\s\S]*?(?=<div class="pd-shell-(?:line|gap)"|<\/div><\/div>)/g, '');
const shownLines = (onScreen.match(/class="pd-shell-line pd-shell-add"/g) || []).length;
const totalLines = (html.match(/class="pd-shell-line/g) || []).length;
check('does not dump all 200 lines up front', shownLines < 200 && shownLines > 5, shownLines);
check('the remainder is folded, not dropped',
      html.includes('<div class="pd-shell-fold"') && totalLines >= 200, totalLines);
check('the fold does not claim the lines are unchanged',
      !/unchanged line/.test(html.slice(html.indexOf('pd-shell-gap'), html.indexOf('pd-shell-gap') + 200)),
      html.slice(html.indexOf('pd-shell-gap'), html.indexOf('pd-shell-gap') + 120));

console.log();
console.log(`${fail === 0 ? 'PASS' : 'FAIL'}: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
