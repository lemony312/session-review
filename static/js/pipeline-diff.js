/*
 * pipeline-diff.js — renders a semantic Spinnaker pipeline (.pp) diff.
 *
 * Consumes the object produced by src/pipeline_diff.py (attached to a review file
 * as file.pipeline_diff, a JSON string). Vanilla JS, no build step, matches the
 * existing single-IIFE style of review.js. Exposes window.PipelineDiff.{render,wire}.
 *
 * A raw-JSON diff is always one click away — review.js keeps the raw diff tables
 * in the DOM and the "View Raw Diff" toggle flips between them.
 */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Change-type → sigil + css class (redundant coding: sigil + color, never color alone).
  var SIGIL = { added: '+', removed: '−', changed: '~', renamed: '⇄', reordered: '⇅', unchanged: '=' };
  // Stage-type → identity glyph (never carries change meaning).
  var TYPE_GLYPH = {
    pipeline: '⎈', runJobManifest: '⚙', deployManifest: '▤', evaluateVariables: 'ƒ',
    wait: '◷', manualJudgment: '⏸', checkPreconditions: '✓?', jenkins: '⚙', script: '⚙',
    findImageFromTags: '◎', bake: '◍', deploy: '▤', destroyServerGroup: '✕'
  };
  var SEV_ICON = { critical: '⛔', serious: '◆', major: '◆', warning: '⚠', minor: '·' };

  function sigilSpan(changeType) {
    return '<span class="pd-sigil pd-sigil-' + esc(changeType) + '">' + (SIGIL[changeType] || '') + '</span>';
  }

  function shortVal(v) {
    if (v === null || v === undefined) return '∅';
    if (typeof v === 'object') { try { return JSON.stringify(v); } catch (e) { return String(v); } }
    return String(v);
  }

  // A value long enough to bury its own meaning gets clipped, with the full text
  // one click away — the JSON wall is what this renderer exists to avoid.
  var VAL_CLIP = 160;

  function valHtml(v, cls) {
    var s = shortVal(v);
    if (s.length <= VAL_CLIP) return '<span class="' + cls + '">' + esc(s) + '</span>';
    var full = (v !== null && typeof v === 'object') ? JSON.stringify(v, null, 2) : s;
    return '<span class="' + cls + '">'
      + '<span class="pd-more-clip">' + esc(s.slice(0, VAL_CLIP)) + '…</span>'
      + '<span class="pd-more-full" hidden>' + esc(full) + '</span>'
      + '<button class="pd-more" data-more>show all (' + s.length + ' chars)</button>'
      + '</span>';
  }

  // Structural noise in a Kubernetes path. Dropping it makes the meaningful
  // segments — manifest, container, env var — the ones you actually read.
  var PATH_NOISE = { spec: 1, template: 1, jobTemplate: 1, metadata: 1, manifest: 1, manifests: 1 };

  function prettyPath(path) {
    var segs = String(path || '').split('.');
    var kept = [];
    segs.forEach(function (s) {
      var bare = s.replace(/\[.*$/, '');
      if (PATH_NOISE[bare] && s === bare) return;         // plain noise segment
      if (PATH_NOISE[bare] && s !== bare) {                // e.g. manifests[CronJob/x]
        kept.push(s.slice(bare.length).replace(/^\[|\]$/g, ''));
        return;
      }
      kept.push(s);
    });
    return kept.length ? kept.join(' › ') : path;
  }

  // ---- summary header ----
  function renderSummary(d) {
    var s = d.summary || {};
    var parts = [];
    function tile(sig, n, label, cls) {
      if (!n) return '';
      return '<span class="pd-tile ' + cls + '"><span class="pd-tile-sig">' + sig + '</span>'
        + n + ' <span class="pd-tile-label">' + label + '</span></span>';
    }
    parts.push(tile('+', s.stagesAdded, 'added', 'pd-added'));
    parts.push(tile('−', s.stagesRemoved, 'removed', 'pd-removed'));
    parts.push(tile('~', s.stagesChanged, 'changed', 'pd-changed'));
    parts.push(tile('⇄', s.stagesRenamed, 'renamed', 'pd-changed'));
    var shellLines = (s.shellLinesAdded || s.shellLinesRemoved)
      ? ' <span class="pd-tile-label">(+' + (s.shellLinesAdded || 0) + ' −' + (s.shellLinesRemoved || 0) + ' lines)</span>'
      : '';
    parts.push(tile('⌗', s.shellEdits, 'shell edits' + shellLines, 'pd-changed'));
    var edges = (s.edgesAdded || 0) + (s.edgesRemoved || 0);
    parts.push(tile('⇅', edges, 'edge changes', 'pd-moved'));
    var risk = s.lintFlags
      ? '<span class="pd-tile pd-risk"><span class="pd-tile-sig">⚠</span>' + s.lintFlags + ' risk flag' + (s.lintFlags > 1 ? 's' : '') + '</span>'
      : '';
    var tiles = parts.filter(Boolean).join('') || '<span class="pd-tile pd-muted">No semantic changes (cosmetic re-serialization only)</span>';
    var reorder = s.pureReorder ? '<span class="pd-note">DAG reshaped — no stage bodies changed</span>' : '';
    var name = (d.pipeline && d.pipeline.name) ? d.pipeline.name : '';

    return '<div class="pd-summary">'
      + '<div class="pd-summary-title">' + esc(name) + '  <span class="pd-filetype">' + esc(d.fileChangeType) + '</span></div>'
      + '<div class="pd-tiles">' + tiles + ' ' + risk + '</div>'
      + reorder
      + '</div>';
  }

  // ---- pipeline-level collections (params, triggers, meta, dag) ----
  function renderCollection(title, items, kind) {
    if (!items || !items.length) return '';
    var rows = items.map(function (it) {
      var ct = it.changeType || 'changed';
      var body = '';
      if (ct === 'added') body = valHtml(it.new, 'pd-new');
      else if (ct === 'removed') body = valHtml(it.old, 'pd-old');
      else body = valHtml(it.old, 'pd-old') + ' → ' + valHtml(it.new, 'pd-new');
      return '<div class="pd-coll-row pd-sev-' + esc(it.severity || 'minor') + '">'
        + sigilSpan(ct) + ' <code>' + esc(it.identity || it.path) + '</code> ' + body + '</div>';
    }).join('');
    return '<div class="pd-block"><div class="pd-block-title">' + esc(title) + ' (' + items.length + ')</div>' + rows + '</div>';
  }

  function renderMeta(items) {
    if (!items || !items.length) return '';
    var rows = items.map(function (m) {
      var ct = m.changeType || 'changed';
      var body = ct === 'added' ? valHtml(m.new, 'pd-new')
        : ct === 'removed' ? valHtml(m.old, 'pd-old')
        : valHtml(m.old, 'pd-old') + ' → ' + valHtml(m.new, 'pd-new');
      return '<div class="pd-coll-row pd-sev-' + esc(m.severity || 'minor') + '">'
        + sigilSpan(ct) + ' <code title="' + esc(m.path) + '">' + esc(prettyPath(m.path)) + '</code> '
        + body + '</div>';
    }).join('');
    return '<div class="pd-block"><div class="pd-block-title">Pipeline config (' + items.length + ')</div>' + rows + '</div>';
  }

  // ---- visual DAG (SVG, layered left→right by dependency depth) ----
  var NODE_W = 190, NODE_H = 46, COL_GAP = 70, ROW_GAP = 18, PAD = 16;

  function statusStroke(status) {
    return status === 'added' ? 'var(--green)'
      : status === 'removed' ? 'var(--red)'
      : status === 'changed' ? 'var(--yellow)'
      : status === 'renamed' || status === 'reordered' ? 'var(--blue)'
      : 'var(--border)';
  }

  function renderGraph(dag) {
    var g = dag && dag.graph;
    if (!g || !g.nodes || !g.nodes.length) return '';

    // Group nodes by level (column). Stable order within a column.
    var byLevel = {};
    var maxLevel = 0;
    g.nodes.forEach(function (n) {
      var lv = n.level || 0;
      maxLevel = Math.max(maxLevel, lv);
      (byLevel[lv] = byLevel[lv] || []).push(n);
    });

    // Assign x,y per node.
    var pos = {};
    var maxRows = 0;
    for (var lv = 0; lv <= maxLevel; lv++) {
      var col = byLevel[lv] || [];
      col.sort(function (a, b) { return a.id < b.id ? -1 : 1; });
      maxRows = Math.max(maxRows, col.length);
      col.forEach(function (n, row) {
        pos[n.id] = {
          x: PAD + lv * (NODE_W + COL_GAP),
          y: PAD + row * (NODE_H + ROW_GAP),
          node: n
        };
      });
    }

    var width = PAD * 2 + (maxLevel + 1) * NODE_W + maxLevel * COL_GAP;
    var height = PAD * 2 + maxRows * NODE_H + (maxRows - 1) * ROW_GAP;

    // Edges (draw first, under nodes).
    var edgeSvg = (g.edges || []).map(function (e) {
      var a = pos[e.from], b = pos[e.to];
      if (!a || !b) return '';
      var x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
      var x2 = b.x, y2 = b.y + NODE_H / 2;
      var mx = (x1 + x2) / 2;
      var d = 'M' + x1 + ',' + y1 + ' C' + mx + ',' + y1 + ' ' + mx + ',' + y2 + ' ' + x2 + ',' + y2;
      var cls = 'pd-edge pd-edge-' + esc(e.status);
      var marker = e.status === 'removed' ? 'url(#pd-arrow-removed)'
        : e.status === 'added' ? 'url(#pd-arrow-added)' : 'url(#pd-arrow)';
      return '<path class="' + cls + '" d="' + d + '" marker-end="' + marker + '"/>';
    }).join('');

    // Nodes.
    var nodeSvg = g.nodes.map(function (n) {
      var p = pos[n.id];
      var glyph = TYPE_GLYPH[n.type] || '◆';
      var sig = SIGIL[n.status] || '';
      var label = n.id.length > 22 ? n.id.slice(0, 21) + '…' : n.id;
      var cls = 'pd-gnode pd-gnode-' + esc(n.status);
      return '<g class="' + cls + '" transform="translate(' + p.x + ',' + p.y + ')">'
        + '<rect width="' + NODE_W + '" height="' + NODE_H + '" rx="7" ry="7"/>'
        + '<text class="pd-gnode-sig" x="10" y="19">' + esc(sig) + '</text>'
        + '<text class="pd-gnode-glyph" x="26" y="19">' + esc(glyph) + '</text>'
        + '<text class="pd-gnode-name" x="42" y="19">' + esc(label) + '</text>'
        + '<text class="pd-gnode-type" x="10" y="36">' + esc(n.type) + '</text>'
        + '<title>' + esc(n.id) + ' — ' + esc(n.status) + ' (' + esc(n.type) + ')</title>'
        + '</g>';
    }).join('');

    var defs = '<defs>'
      + '<marker id="pd-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      + '<path d="M0,0 L8,4 L0,8 z" fill="var(--text-muted)"/></marker>'
      + '<marker id="pd-arrow-added" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      + '<path d="M0,0 L8,4 L0,8 z" fill="var(--green)"/></marker>'
      + '<marker id="pd-arrow-removed" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
      + '<path d="M0,0 L8,4 L0,8 z" fill="var(--red)"/></marker>'
      + '</defs>';

    var legend = '<div class="pd-graph-legend">'
      + '<span class="pd-lg pd-lg-added">+ added</span>'
      + '<span class="pd-lg pd-lg-removed">− removed</span>'
      + '<span class="pd-lg pd-lg-changed">~ changed</span>'
      + '<span class="pd-lg pd-lg-unchanged">= unchanged</span>'
      + '<span class="pd-lg-hint">runs left → right by dependency depth</span>'
      + '</div>';

    return '<div class="pd-block"><div class="pd-block-title">Pipeline graph</div>'
      + legend
      + '<div class="pd-graph-scroll">'
      + '<svg class="pd-graph" width="' + width + '" height="' + height + '" '
      + 'viewBox="0 0 ' + width + ' ' + height + '">'
      + defs + edgeSvg + nodeSvg
      + '</svg></div></div>';
  }

  function renderDag(dag) {
    if (!dag) return '';
    var rows = [];
    (dag.edgesAdded || []).forEach(function (e) {
      rows.push('<div class="pd-coll-row pd-sev-critical">' + sigilSpan('added')
        + ' edge <code>' + esc(e.from) + '</code> → <code>' + esc(e.to) + '</code></div>');
    });
    (dag.edgesRemoved || []).forEach(function (e) {
      rows.push('<div class="pd-coll-row pd-sev-critical">' + sigilSpan('removed')
        + ' edge <code>' + esc(e.from) + '</code> ⇸ <code>' + esc(e.to) + '</code></div>');
    });
    (dag.rootsAdded || []).forEach(function (r) {
      rows.push('<div class="pd-coll-row pd-sev-major">' + sigilSpan('added') + ' <code>' + esc(r) + '</code> now runs at start (root)</div>');
    });
    (dag.rootsRemoved || []).forEach(function (r) {
      rows.push('<div class="pd-coll-row pd-sev-major">' + sigilSpan('removed') + ' <code>' + esc(r) + '</code> no longer a root</div>');
    });
    if (!rows.length) return '';
    return '<div class="pd-block"><div class="pd-block-title">Dependency graph (' + rows.length + ')</div>' + rows.join('') + '</div>';
  }

  // ---- bash syntax highlighting (self-contained, token-based) ----
  var SHELL_KEYWORDS = ('if then else elif fi for while until do done case esac function '
    + 'in select return exit break continue local export readonly declare set unset '
    + 'shift eval trap source').split(' ');
  var SHELL_BUILTINS = ('echo printf read cd pwd test cat grep sed awk cut curl jq kubectl '
    + 'mkdir mkdir rm cp mv ln chmod chown sleep date env true false exec kill wait '
    + 'mill flyway psql aws').split(' ');

  function highlightShell(text) {
    // Tokenize into: comments, strings, var-expansions, then words/numbers.
    var out = '';
    var i = 0, n = text.length;
    function isWordChar(c) { return /[A-Za-z0-9_]/.test(c); }
    while (i < n) {
      var c = text[i];
      // comment to EOL (only if at start or preceded by whitespace)
      if (c === '#' && (i === 0 || /\s/.test(text[i - 1]))) {
        var j = text.indexOf('\n', i); if (j < 0) j = n;
        out += '<span class="tok-comment">' + esc(text.slice(i, j)) + '</span>'; i = j; continue;
      }
      // double / single quoted string
      if (c === '"' || c === "'") {
        var q = c, k = i + 1;
        while (k < n && text[k] !== q) { if (text[k] === '\\' && q === '"') k++; k++; }
        k = Math.min(k + 1, n);
        out += '<span class="tok-string">' + esc(text.slice(i, k)) + '</span>'; i = k; continue;
      }
      // variable expansion: ${...} or $NAME or $(...)
      if (c === '$') {
        var m = /^\$\{[^}]*\}|^\$\([^)]*\)|^\$[A-Za-z0-9_]+/.exec(text.slice(i));
        if (m) {
          var isBrace = m[0].charAt(1) === '{';
          // Flag raw shell brace-expansion (SpEL-collision risk surface).
          var cls = isBrace ? 'tok-var tok-var-risk' : 'tok-var';
          out += '<span class="' + cls + '">' + esc(m[0]) + '</span>'; i += m[0].length; continue;
        }
      }
      // word (keyword / builtin / plain)
      if (isWordChar(c)) {
        var s = i; while (i < n && isWordChar(text[i])) i++;
        var w = text.slice(s, i);
        if (/^\d+$/.test(w)) out += '<span class="tok-num">' + esc(w) + '</span>';
        else if (SHELL_KEYWORDS.indexOf(w) >= 0) out += '<span class="tok-kw">' + esc(w) + '</span>';
        else if (SHELL_BUILTINS.indexOf(w) >= 0) out += '<span class="tok-fn">' + esc(w) + '</span>';
        else out += esc(w);
        continue;
      }
      out += esc(c); i++;
    }
    return out;
  }

  // Build a unified line diff (with context + line numbers) from full old/new text.
  // We recompute here rather than trust hunks so we can show context + collapse runs.
  function diffLines(oldText, newText) {
    var a = (oldText || '').split('\n');
    var b = (newText || '').split('\n');
    // LCS table (fine for the few-hundred-line scripts we see).
    var m = a.length, nn = b.length;
    var lcs = [];
    for (var x = 0; x <= m; x++) lcs.push(new Array(nn + 1).fill(0));
    for (var x = m - 1; x >= 0; x--)
      for (var y = nn - 1; y >= 0; y--)
        lcs[x][y] = a[x] === b[y] ? lcs[x + 1][y + 1] + 1 : Math.max(lcs[x + 1][y], lcs[x][y + 1]);
    var rows = [], x2 = 0, y2 = 0, oi = 1, ni = 1;
    while (x2 < m && y2 < nn) {
      if (a[x2] === b[y2]) { rows.push({ t: 'ctx', o: oi++, n: ni++, s: a[x2] }); x2++; y2++; }
      else if (lcs[x2 + 1][y2] >= lcs[x2][y2 + 1]) { rows.push({ t: 'del', o: oi++, n: null, s: a[x2] }); x2++; }
      else { rows.push({ t: 'add', o: null, n: ni++, s: b[y2] }); y2++; }
    }
    while (x2 < m) { rows.push({ t: 'del', o: oi++, n: null, s: a[x2++] }); }
    while (y2 < nn) { rows.push({ t: 'add', o: null, n: ni++, s: b[y2++] }); }
    return rows;
  }

  var SHELL_CTX = 3; // context lines around a change
  // A stage that was added or removed wholesale has a one-sided script: every line
  // is a change, so the context rule keeps all of them. Show the head of it — enough
  // to see what the script does — and put the tail one click away.
  var SHELL_ONESIDED_MAX = 24;

  // Emphasize the changed span within a replaced line pair. Only when the edit is
  // a clear substring swap: highlighting each fragment separately can mis-tokenize
  // a split quote, so bail out unless the middle is small and quote-balanced.
  function emphasize(oldLine, newLine) {
    var a = oldLine || '', b = newLine || '';
    var p = 0;
    while (p < a.length && p < b.length && a[p] === b[p]) p++;
    var s = 0;
    while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
    var midA = a.slice(p, a.length - s), midB = b.slice(p, b.length - s);
    var shared = p + s;
    var balanced = function (t) {
      return (t.split('"').length - 1) % 2 === 0 && (t.split("'").length - 1) % 2 === 0;
    };
    if (shared < Math.max(a.length, b.length) * 0.3
        || Math.max(midA.length, midB.length) > 60
        || !balanced(midA) || !balanced(midB)) {
      return null;
    }
    var pre = highlightShell(a.slice(0, p));
    var suf = highlightShell(s ? a.slice(a.length - s) : '');
    var mark = function (mid) {
      return pre + (mid ? '<span class="pd-ch">' + highlightShell(mid) + '</span>' : '') + suf;
    };
    return { old: mark(midA), new: mark(midB) };
  }

  // Pair each run of deletions with the run of additions that follows it, so a
  // rewritten line can be shown as a substring swap rather than two unrelated lines.
  function pairRows(rows) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].t !== 'del') continue;
      var dEnd = i; while (dEnd + 1 < rows.length && rows[dEnd + 1].t === 'del') dEnd++;
      var aStart = dEnd + 1;
      if (aStart >= rows.length || rows[aStart].t !== 'add') { i = dEnd; continue; }
      var aEnd = aStart; while (aEnd + 1 < rows.length && rows[aEnd + 1].t === 'add') aEnd++;
      var n = Math.min(dEnd - i + 1, aEnd - aStart + 1);
      for (var k = 0; k < n; k++) {
        var em = emphasize(rows[i + k].s, rows[aStart + k].s);
        if (em) { rows[i + k].html = em.old; rows[aStart + k].html = em.new; }
      }
      i = aEnd;
    }
    return rows;
  }

  var gapSeq = 0;

  function renderShellDiff(sd) {
    var rows = (sd.old !== undefined || sd.new !== undefined)
      ? diffLines(sd.old, sd.new)
      : [];
    // Fallback to raw hunks if full text absent.
    if (!rows.length && sd.hunks) {
      (sd.hunks || []).forEach(function (h) {
        (h.oldLines || []).forEach(function (l) { rows.push({ t: 'del', o: null, n: null, s: l }); });
        (h.newLines || []).forEach(function (l) { rows.push({ t: 'add', o: null, n: null, s: l }); });
      });
    }
    pairRows(rows);

    // Mark which rows to keep (changes + context); the rest stay in the DOM behind
    // a click, so nothing is silently dropped from the review.
    var oneSided = sd.old === '' || sd.new === '' || sd.old == null || sd.new == null;
    var keep = new Array(rows.length).fill(false);
    for (var r = 0; r < rows.length; r++) {
      if (rows[r].t !== 'ctx') {
        for (var k = Math.max(0, r - SHELL_CTX); k <= Math.min(rows.length - 1, r + SHELL_CTX); k++) keep[k] = true;
      }
    }
    if (oneSided) {
      for (var r2 = SHELL_ONESIDED_MAX; r2 < rows.length; r2++) keep[r2] = false;
    }

    function lineHtml(row) {
      var sig = row.t === 'add' ? '+' : row.t === 'del' ? '−' : ' ';
      var cls = row.t === 'add' ? 'pd-shell-add' : row.t === 'del' ? 'pd-shell-del' : 'pd-shell-ctx';
      return '<div class="pd-shell-line ' + cls + '">'
        + '<span class="pd-shell-ln">' + (row.o == null ? '' : row.o) + '</span>'
        + '<span class="pd-shell-ln">' + (row.n == null ? '' : row.n) + '</span>'
        + '<span class="pd-shell-sig">' + sig + '</span>'
        + '<code class="pd-shell-code">' + (row.html || highlightShell(row.s)) + '</code>'
        + '</div>';
    }

    var body = '', hidden = [];
    function flushGap() {
      if (!hidden.length) return;
      var id = 'g' + (++gapSeq);
      var allCtx = hidden.every(function (h) { return h.t === 'ctx'; });
      var what = (allCtx ? ' unchanged line' : ' more line') + (hidden.length > 1 ? 's' : '');
      body += '<div class="pd-shell-gap" data-gap="' + id + '" role="button" tabindex="0">'
        + '⋯ ' + hidden.length + what + ' — click to expand</div>'
        + '<div class="pd-shell-fold" data-gap-group="' + id + '" hidden>'
        + hidden.map(lineHtml).join('') + '</div>';
      hidden = [];
    }
    for (var r = 0; r < rows.length; r++) {
      if (!keep[r]) { hidden.push(rows[r]); continue; }
      flushGap();
      body += lineHtml(rows[r]);
    }
    flushGap();

    var counts = rows.reduce(function (acc, r) { if (r.t === 'add') acc.a++; else if (r.t === 'del') acc.d++; return acc; }, { a: 0, d: 0 });
    var where = [sd.manifest, sd.container].filter(Boolean).join(' / ') || sd.container;
    var loc = sd.location ? ' · <span class="pd-shell-loc">' + esc(sd.location) + '</span>' : '';
    var folded = body.indexOf('data-gap-group') >= 0;
    var head = '⌗ embedded shell · <code>' + esc(where) + '</code>' + loc
      + ' <span class="pd-shell-stat">+' + counts.a + ' −' + counts.d + '</span>'
      + (folded ? '<button class="pd-shell-btn" data-expand-all title="Show every line of the script">full script</button>' : '')
      + '<button class="pd-shell-btn pd-shell-copy" data-copy title="Copy new script">copy</button>';

    return '<div class="pd-shell" data-shell>'
      + '<div class="pd-shell-head">' + head + '</div>'
      + '<div class="pd-shell-body">' + (body || '<div class="pd-muted">(no textual changes)</div>') + '</div>'
      + '<textarea class="pd-shell-src" hidden>' + esc(sd.new || '') + '</textarea>'
      + '</div>';
  }

  function renderLint(flags) {
    if (!flags || !flags.length) return '';
    return flags.map(function (f) {
      return '<div class="pd-lint pd-lint-' + esc(f.severity) + '">'
        + (SEV_ICON[f.severity] || '⚠') + ' <strong>' + esc(f.id) + '</strong> — ' + esc(f.message) + '</div>';
    }).join('');
  }

  // Config rows stay visible while they are still scannable; past this they fold
  // behind a labelled count so they cannot bury the shell diff below them.
  var FIELDS_OPEN_MAX = 8;

  // `noun` labels the fold bar: a changed stage lists config *changes*, while an
  // added or removed stage lists the settings it brought with it or took away.
  function renderFieldChanges(fcs, noun) {
    if (!fcs || !fcs.length) return '';
    var rows = fcs.map(function (fc) {
      var ct = fc.changeType || 'changed';
      var body;
      if (ct === 'added') body = '<div class="pd-field-vals">' + valHtml(fc.new, 'pd-new') + '</div>';
      else if (ct === 'removed') body = '<div class="pd-field-vals">' + valHtml(fc.old, 'pd-old') + '</div>';
      else if (fc.category === 'dag-edge-change') body = valHtml(fc.old, 'pd-old') + ' → ' + valHtml(fc.new, 'pd-new');
      else body = '<div class="pd-field-vals">' + valHtml(fc.old, 'pd-old') + valHtml(fc.new, 'pd-new') + '</div>';
      return '<div class="pd-field pd-sev-' + esc(fc.severity || 'minor') + '">'
        + sigilSpan(ct)
        + ' <code class="pd-field-path" title="' + esc(fc.path) + '">' + esc(prettyPath(fc.path)) + '</code> '
        + body + '</div>';
    }).join('');

    if (fcs.length <= FIELDS_OPEN_MAX) return '<div class="pd-fields">' + rows + '</div>';
    return '<div class="pd-fold">'
      + '<div class="pd-fold-bar" data-fold role="button" tabindex="0">▸ ' + fcs.length + ' '
      + esc(noun || 'config changes') + '</div>'
      + '<div class="pd-fields" data-fold-body hidden>' + rows + '</div>'
      + '</div>';
  }

  // ---- stage cards ----
  function renderStage(st) {
    var glyph = TYPE_GLYPH[(st.new && st.new.type) || (st.old && st.old.type)] || '◆';
    var cats = (st.changeCategories || []).map(function (c) { return '<span class="pd-chip">' + esc(c) + '</span>'; }).join('');
    var riskBadge = (st.lintFlags && st.lintFlags.length)
      ? '<span class="pd-risk-badge" title="' + st.lintFlags.length + ' risk flag(s)">⚠ ' + st.lintFlags.length + '</span>' : '';
    var shellDiffs = (st.shellDiffs || []).filter(function (s) { return s.changed; });
    var shell = shellDiffs.map(renderShellDiff).join('');
    // The script and its risk flags are the change; they are never behind a click.
    // Only the config rows collapse, and only when there are many of them.
    var noun = st.changeType === 'added' ? 'settings added with this stage'
      : st.changeType === 'removed' ? 'settings removed with this stage'
      : 'config changes';
    var body = renderLint(st.lintFlags) + shell + renderFieldChanges(st.fieldChanges, noun);
    var toggleHint = body
      ? '<span class="pd-stage-caret" title="Collapse this stage">▾</span>' : '';

    return '<div class="pd-stage pd-stage-' + esc(st.changeType) + '" data-stage>'
      + '<div class="pd-stage-head" data-stage-toggle>'
      + sigilSpan(st.changeType) + ' <span class="pd-glyph">' + glyph + '</span> '
      + '<span class="pd-stage-name">' + esc(st.displayName) + '</span> '
      + '<span class="pd-stage-type">' + esc((st.new && st.new.type) || (st.old && st.old.type) || '') + '</span> '
      + cats + ' ' + riskBadge + toggleHint
      + '</div>'
      + (body ? '<div class="pd-stage-body">' + body + '</div>' : '')
      + '</div>';
  }

  function render(file) {
    var d;
    try { d = JSON.parse(file.pipeline_diff); }
    catch (e) { return '<div class="pd-error">Could not parse pipeline diff — use “View Raw Diff”.</div>'; }
    if (!d) return '<div class="pd-error">No pipeline diff — use “View Raw Diff”.</div>';

    var changedStages = (d.stages || []).filter(function (s) { return s.changeType !== 'unchanged'; });
    // Sort most-severe first.
    var rank = { critical: 0, serious: 1, major: 2, warning: 3, minor: 4 };
    changedStages.sort(function (a, b) { return (rank[a.severity] || 3) - (rank[b.severity] || 3); });

    var stagesHtml = changedStages.length
      ? changedStages.map(renderStage).join('')
      : '<div class="pd-muted">No stage-level changes.</div>';

    return '<div class="pd-root">'
      + renderSummary(d)
      + renderGraph(d.dag)
      + renderDag(d.dag)
      + renderMeta(d.meta)
      + renderCollection('Parameters', d.parameters)
      + renderCollection('Triggers', d.triggers)
      + renderCollection('Notifications', d.notifications)
      + renderCollection('Expected artifacts', d.expectedArtifacts)
      + '<div class="pd-block"><div class="pd-block-title">Stages (' + changedStages.length + ' changed)</div>' + stagesHtml + '</div>'
      + '</div>';
  }

  // Wire stage collapse, shell fold expansion, clipped values, and copy buttons.
  function wire(rootEl) {
    rootEl.querySelectorAll('[data-stage-toggle]').forEach(function (head) {
      head.addEventListener('click', function () {
        var body = head.parentElement.querySelector('.pd-stage-body');
        if (!body) return;
        body.hidden = !body.hidden;
        var caret = head.querySelector('.pd-stage-caret');
        if (caret) caret.textContent = body.hidden ? '▸' : '▾';
      });
    });

    // "⋯ N unchanged lines" — reveal the run that was folded away.
    function revealGap(bar) {
      var group = bar.parentElement.querySelector('[data-gap-group="' + bar.dataset.gap + '"]');
      if (group) group.hidden = false;
      bar.hidden = true;
    }
    rootEl.querySelectorAll('.pd-shell-gap[data-gap]').forEach(function (bar) {
      bar.addEventListener('click', function (e) { e.stopPropagation(); revealGap(bar); });
      bar.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); revealGap(bar); }
      });
    });

    rootEl.querySelectorAll('[data-expand-all]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var block = btn.closest('[data-shell]');
        var bars = block.querySelectorAll('.pd-shell-gap[data-gap]');
        var expand = btn.textContent === 'full script';
        block.querySelectorAll('[data-gap-group]').forEach(function (g) { g.hidden = !expand; });
        bars.forEach(function (b) { b.hidden = expand; });
        btn.textContent = expand ? 'changes only' : 'full script';
      });
    });

    rootEl.querySelectorAll('.pd-fold-bar[data-fold]').forEach(function (bar) {
      var open = function () {
        var body = bar.parentElement.querySelector('[data-fold-body]');
        if (!body) return;
        body.hidden = !body.hidden;
        bar.textContent = (body.hidden ? '▸ ' : '▾ ') + bar.textContent.slice(2);
      };
      bar.addEventListener('click', function (e) { e.stopPropagation(); open(); });
      bar.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
    });

    rootEl.querySelectorAll('.pd-more[data-more]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var wrap = btn.parentElement;
        var clip = wrap.querySelector('.pd-more-clip');
        var full = wrap.querySelector('.pd-more-full');
        if (!clip || !full) return;
        full.hidden = !full.hidden;
        clip.hidden = !full.hidden;
        btn.textContent = full.hidden ? 'show all' : 'show less';
      });
    });

    rootEl.querySelectorAll('.pd-shell-copy[data-copy]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var src = btn.closest('[data-shell]').querySelector('.pd-shell-src');
        if (src) {
          navigator.clipboard.writeText(src.value);
          var t = btn.textContent; btn.textContent = 'copied!';
          setTimeout(function () { btn.textContent = t; }, 1000);
        }
      });
    });
  }

  window.PipelineDiff = { render: render, wire: wire };
})();
