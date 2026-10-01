/*
 * Split (side-by-side) diff — DOM-free. Builds a row model from a unified diff
 * (old line | new line per row), then turns it into HTML strings. Also home of the
 * pure hunk and word-diff helpers shared with review.js. Tested by test_split_diff.js.
 */
(function () {
  'use strict';

  const COLSPAN = 6;
  const COLGROUP = '<colgroup><col class="split-col-num"><col class="split-col-gutter"><col class="split-col-code"><col class="split-col-num"><col class="split-col-gutter"><col class="split-col-code"></colgroup>';
  const WORD_DIFF_MAX_PRODUCT = 250000; // token-count product; protects against minified lines
  const WORD_DIFF_MIN_SIMILARITY = 0.25;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function wrapTable(rowsHtml, extraClass = '') {
    return `<table class="diff-table diff-table-split${extraClass ? ' ' + extraClass : ''}">${COLGROUP}<tbody>${rowsHtml}</tbody></table>`;
  }

  // ============================================================
  // Hunk parsing
  // ============================================================

  function parseHunks(lines) {
    const hunks = [];
    let currentHunk = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Hunk header: @@ -old_start,old_count +new_start,new_count @@ context
      if (line.startsWith('@@')) {
        // Save previous hunk if exists
        if (currentHunk) {
          currentHunk.isWhitespaceOnly = isHunkWhitespaceOnly(currentHunk.lines);
          hunks.push(currentHunk);
        }
        currentHunk = null;

        const match = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/);
        if (match) {
          currentHunk = {
            oldStart: parseInt(match[1]),
            oldCount: match[2] === undefined ? 1 : parseInt(match[2]),
            newStart: parseInt(match[3]),
            newCount: match[4] === undefined ? 1 : parseInt(match[4]),
            context: match[5].trim(),
            lines: [],
            isWhitespaceOnly: false
          };
        }
        continue;
      }

      // File headers (diff --git, index, ---, +++) only appear before a hunk
      if (!currentHunk) continue;

      if (line.startsWith('+')) {
        currentHunk.lines.push({ type: 'addition', content: line.substring(1) });
      } else if (line.startsWith('-')) {
        currentHunk.lines.push({ type: 'deletion', content: line.substring(1) });
      } else if (line.startsWith(' ')) {
        currentHunk.lines.push({ type: 'context', content: line.substring(1) });
      }
    }

    // Save last hunk
    if (currentHunk) {
      currentHunk.isWhitespaceOnly = isHunkWhitespaceOnly(currentHunk.lines);
      hunks.push(currentHunk);
    }

    return hunks;
  }

  function isHunkWhitespaceOnly(lines) {
    // A hunk is whitespace-only if all additions and deletions have identical
    // content when whitespace is normalized (trim + collapse internal whitespace)
    const additions = lines.filter(l => l.type === 'addition');
    const deletions = lines.filter(l => l.type === 'deletion');

    // If there are no changes (only context), not whitespace-only
    if (additions.length === 0 && deletions.length === 0) {
      return false;
    }

    // Must have same number of additions and deletions for whitespace-only
    if (additions.length !== deletions.length) {
      return false;
    }

    // Normalize whitespace: trim and collapse multiple spaces
    function normalizeWhitespace(text) {
      return text.trim().replace(/\s+/g, ' ');
    }

    // Get normalized versions
    const normalizedAdditions = additions.map(a => normalizeWhitespace(a.content));
    const normalizedDeletions = deletions.map(d => normalizeWhitespace(d.content));

    // Sort both arrays to allow for line reordering
    normalizedAdditions.sort();
    normalizedDeletions.sort();

    // Check if all normalized lines match
    for (let i = 0; i < normalizedAdditions.length; i++) {
      if (normalizedAdditions[i] !== normalizedDeletions[i]) {
        return false; // Real content change
      }
    }

    return true;
  }

  // ============================================================
  // Word-Diff
  // ============================================================

  // Tokenize preserving whitespace runs as separate tokens
  function tokenize(text) {
    return text.match(/\S+|\s+/g) || [];
  }

  function computeWordDiff(oldText, newText) {
    const oldTokens = tokenize(oldText);
    const newTokens = tokenize(newText);
    const m = oldTokens.length;
    const n = newTokens.length;

    // LCS dynamic programming table
    const dp = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (oldTokens[i - 1] === newTokens[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
      }
    }

    // Backtrack to get operations (in reverse)
    const ops = [];
    let i = m, j = n;
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && oldTokens[i - 1] === newTokens[j - 1]) {
        ops.push({ type: 'equal', text: newTokens[j - 1] });
        i--; j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        ops.push({ type: 'insert', text: newTokens[j - 1] });
        j--;
      } else {
        ops.push({ type: 'delete', text: oldTokens[i - 1] });
        i--;
      }
    }
    ops.reverse();

    // Group consecutive same-type ops
    const groups = [];
    for (const op of ops) {
      if (groups.length > 0 && groups[groups.length - 1].type === op.type) {
        groups[groups.length - 1].text += op.text;
      } else {
        groups.push({ type: op.type, text: op.text });
      }
    }

    // Merge adjacent delete+insert into replace
    const merged = [];
    for (let k = 0; k < groups.length; k++) {
      if (groups[k].type === 'delete' && k + 1 < groups.length && groups[k + 1].type === 'insert') {
        merged.push({ type: 'replace', oldText: groups[k].text, newText: groups[k + 1].text });
        k++;
      } else {
        merged.push(groups[k]);
      }
    }

    return merged;
  }

  function pairHunkLines(hunkLines) {
    // Group hunk lines into: context lines, paired (del+add), pure additions, pure deletions
    const result = [];
    let i = 0;

    while (i < hunkLines.length) {
      if (hunkLines[i].type === 'context') {
        result.push({ kind: 'context', line: hunkLines[i] });
        i++;
      } else {
        // Collect consecutive deletions then additions
        const dels = [];
        while (i < hunkLines.length && hunkLines[i].type === 'deletion') {
          dels.push(hunkLines[i]);
          i++;
        }
        const adds = [];
        while (i < hunkLines.length && hunkLines[i].type === 'addition') {
          adds.push(hunkLines[i]);
          i++;
        }

        // Pair them up
        const pairCount = Math.min(dels.length, adds.length);
        for (let p = 0; p < pairCount; p++) {
          result.push({ kind: 'paired', old: dels[p], new: adds[p] });
        }
        // Remaining unpaired deletions
        for (let p = pairCount; p < dels.length; p++) {
          result.push({ kind: 'pure-deletion', line: dels[p] });
        }
        // Remaining unpaired additions
        for (let p = pairCount; p < adds.length; p++) {
          result.push({ kind: 'pure-addition', line: adds[p] });
        }
      }
    }

    return result;
  }

  // Word-highlighted HTML for both halves of a changed pair, or null when the lines
  // are too long or too different for highlights to help.
  function renderWordDiffSides(oldText, newText) {
    if (tokenize(oldText).length * tokenize(newText).length > WORD_DIFF_MAX_PRODUCT) return null;

    const segments = computeWordDiff(oldText, newText);
    const maxLen = Math.max(oldText.trim().length, newText.trim().length);
    const equalNonWs = segments
      .filter(s => s.type === 'equal' && /\S/.test(s.text))
      .reduce((sum, s) => sum + s.text.length, 0);
    if (maxLen > 0 && equalNonWs / maxLen < WORD_DIFF_MIN_SIMILARITY) return null;

    const del = t => `<span class="word-del">${escapeHtml(t)}</span>`;
    const add = t => `<span class="word-add">${escapeHtml(t)}</span>`;
    let oldHtml = '';
    let newHtml = '';
    for (const seg of segments) {
      if (seg.type === 'equal') {
        oldHtml += escapeHtml(seg.text);
        newHtml += escapeHtml(seg.text);
      } else if (seg.type === 'delete') {
        oldHtml += del(seg.text);
      } else if (seg.type === 'insert') {
        newHtml += add(seg.text);
      } else if (seg.type === 'replace') {
        oldHtml += del(seg.oldText);
        newHtml += add(seg.newText);
      }
    }
    return { oldHtml, newHtml };
  }

  // ============================================================
  // Row model
  // ============================================================

  function hunkLineRows(hunk, effOld, effNew) {
    const rows = [];
    let oldNum = effOld;
    let newNum = effNew;

    for (const item of pairHunkLines(hunk.lines)) {
      if (item.kind === 'context') {
        rows.push({ type: 'line', kind: 'context', oldNum, oldText: item.line.content, newNum, newText: item.line.content });
        oldNum++; newNum++;
      } else if (item.kind === 'paired') {
        rows.push({ type: 'line', kind: 'change', oldNum, oldText: item.old.content, newNum, newText: item.new.content });
        oldNum++; newNum++;
      } else if (item.kind === 'pure-deletion') {
        rows.push({ type: 'line', kind: 'del', oldNum, oldText: item.line.content, newNum: null, newText: null });
        oldNum++;
      } else {
        rows.push({ type: 'line', kind: 'add', oldNum: null, oldText: null, newNum, newText: item.line.content });
        newNum++;
      }
    }
    return rows;
  }

  function buildRows(diffText, { fileId, fullLineCount = 0, moveSections = [] } = {}) {
    const rows = [];
    const hunks = parseHunks((diffText || '').split('\n'));
    const sections = moveSections.slice().sort((a, b) => a.target_start - b.target_start);
    let nextSectionIdx = 0;
    let oldNext = 1;
    let newNext = 1;

    for (const hunk of hunks) {
      // A zero-length side starts after the line the header names
      const effOld = hunk.oldCount === 0 ? hunk.oldStart + 1 : hunk.oldStart;
      const effNew = hunk.newCount === 0 ? hunk.newStart + 1 : hunk.newStart;

      if (fullLineCount && effNew > newNext) {
        rows.push({ type: 'expand', direction: 'before', startLine: newNext, endLine: effNew - 1, oldStart: oldNext, count: effNew - newNext });
      }

      rows.push({ type: 'hunk', label: hunk.context || `Lines ${hunk.newStart}...` });

      const lineRows = hunkLineRows(hunk, effOld, effNew);
      if (hunk.isWhitespaceOnly) {
        rows.push({ type: 'ws-hunk', hunkId: `ws-split-${fileId}-${hunk.newStart}`, lineCount: hunk.lines.length, rows: lineRows });
      } else {
        for (const row of lineRows) {
          if (row.newNum != null) {
            while (nextSectionIdx < sections.length && row.newNum >= sections[nextSectionIdx].target_start) {
              rows.push({ type: 'move', section: sections[nextSectionIdx] });
              nextSectionIdx++;
            }
          }
          rows.push(row);
        }
      }

      const dels = hunk.lines.filter(l => l.type === 'deletion').length;
      const adds = hunk.lines.filter(l => l.type === 'addition').length;
      const ctx = hunk.lines.filter(l => l.type === 'context').length;
      oldNext = effOld + dels + ctx;
      newNext = effNew + adds + ctx;
    }

    if (fullLineCount && newNext <= fullLineCount) {
      rows.push({ type: 'expand', direction: 'after', startLine: newNext, endLine: fullLineCount, oldStart: oldNext, count: fullLineCount - newNext + 1 });
    }

    return rows;
  }

  // ============================================================
  // HTML
  // ============================================================

  function side(which, num, cls, gutter, contentHtml, markers) {
    return `<td class="line-num ${which} ${cls}">${num ?? ''}</td><td class="gutter ${which} ${cls}">${gutter}</td><td class="line-content ${which} ${cls}">${markers}${contentHtml == null ? '' : '<code>' + contentHtml + '</code>'}</td>`;
  }

  function emptySide(which) {
    return side(which, null, 'split-empty', '', null, '');
  }

  function renderContextRow(oldNum, newNum, contentHtml, expanded) {
    return `<tr class="diff-line split-row split-context${expanded ? '-expanded' : ''}">${side('old', oldNum, '', '', contentHtml, '')}${side('new', newNum, '', '', contentHtml, '')}</tr>`;
  }

  function renderExpandRow(fileId, row) {
    const label = row.direction === 'before' ? `⬆ Show ${row.count} more lines` : `⬇ Show ${row.count} more lines`;
    return `
      <tr class="expand-context-row">
        <td colspan="${COLSPAN}">
          <button class="expand-context-btn"
                  data-file-id="${fileId}"
                  data-direction="${row.direction}"
                  data-start-line="${row.startLine}"
                  data-end-line="${row.endLine}"
                  data-old-start="${row.oldStart}">
            ${label}
          </button>
        </td>
      </tr>
    `;
  }

  function renderWsHunk(row, opts) {
    return `
      <tr class="whitespace-hunk-collapsed" data-hunk-id="${row.hunkId}">
        <td colspan="${COLSPAN}">
          <div class="whitespace-hunk-indicator">
            <span class="ws-hunk-icon">⋮</span>
            <span class="ws-hunk-text">formatting changes (${row.lineCount} lines)</span>
            <button class="ws-hunk-expand-btn" data-hunk-id="${row.hunkId}">expand</button>
          </div>
        </td>
      </tr>
      <tr class="whitespace-hunk-content-wrapper hidden" data-hunk-id="${row.hunkId}">
        <td colspan="${COLSPAN}" style="padding: 0;">
          ${wrapTable(render(row.rows, opts), 'whitespace-hunk-content')}
        </td>
      </tr>
    `;
  }

  // The 💬 marker: a note Claude left in the session about this line
  function markerHtml(a) {
    const label = `Claude's note from the session (${escapeHtml(a.annotation_type)}) — click to read`;
    return `<span class="annotation-marker" data-annotation-id="${a.id}" title="${label}" aria-label="${label}">💬</span>`;
  }

  function renderLine(row, opts) {
    const highlight = opts.highlight || escapeHtml;
    const annotations = row.newNum != null ? (opts.annotations || []).filter(a => a.line_start === row.newNum) : [];
    const markers = annotations.map(a =>
      markerHtml(a)
    ).join('');

    let left, right;
    if (row.kind === 'context') {
      const html = highlight(row.oldText, opts.language);
      left = side('old', row.oldNum, '', '', html, '');
      right = side('new', row.newNum, '', '', html, markers);
    } else if (row.kind === 'change') {
      const words = renderWordDiffSides(row.oldText, row.newText);
      const oldHtml = words ? words.oldHtml : highlight(row.oldText, opts.language);
      const newHtml = words ? words.newHtml : highlight(row.newText, opts.language);
      left = side('old', row.oldNum, 'split-del', '-', oldHtml, '');
      right = side('new', row.newNum, 'split-add', '+', newHtml, markers);
    } else if (row.kind === 'del') {
      left = side('old', row.oldNum, 'split-del', '-', highlight(row.oldText, opts.language), '');
      right = emptySide('new');
    } else {
      left = emptySide('old');
      right = side('new', row.newNum, 'split-add', '+', highlight(row.newText, opts.language), markers);
    }

    let html = `<tr class="diff-line split-row split-${row.kind}">${left}${right}</tr>`;
    const isExpanded = opts.isExpanded || (() => false);
    const renderCard = opts.renderAnnotationCard || (() => '');
    annotations.forEach(a => {
      if (isExpanded(a.id)) html += renderCard(a, COLSPAN);
    });
    return html;
  }

  function render(rows, opts = {}) {
    let html = '';
    for (const row of rows) {
      if (row.type === 'hunk') {
        html += `<tr class="hunk-header"><td colspan="${COLSPAN}" class="line-content"><span class="hunk-label">${escapeHtml(row.label)}</span></td></tr>`;
      } else if (row.type === 'expand') {
        html += renderExpandRow(opts.fileId, row);
      } else if (row.type === 'move') {
        const sec = row.section;
        html += `<tr class="move-section-banner"><td colspan="${COLSPAN}"><div class="move-section-label">↙ From <code>${escapeHtml(sec.source)}</code> (${sec.similarity}% similar) — lines ${sec.target_start}–${sec.target_end}</div></td></tr>`;
      } else if (row.type === 'ws-hunk') {
        html += renderWsHunk(row, opts);
      } else {
        html += renderLine(row, opts);
      }
    }
    return html;
  }

  window.SplitDiff = {
    COLSPAN,
    markerHtml,
    parseHunks,
    isHunkWhitespaceOnly,
    computeWordDiff,
    pairHunkLines,
    buildRows,
    render,
    renderContextRow,
    renderWordDiffSides,
    wrapTable,
    escapeHtml
  };
})();
