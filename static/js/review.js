// Review Detail Page - Main JavaScript
// Renders GitHub PR-style diff view with annotations

(function() {
  'use strict';

  // State
  let reviewData = null;
  let fileDataMap = new Map(); // file_id -> file object
  let annotationsByFile = new Map(); // file_id -> annotation[]
  let expandedAnnotations = new Set(); // annotation IDs that are visible
  let currentFileIndex = 0;

  const { parseHunks, computeWordDiff, pairHunkLines } = window.SplitDiff;

  // Diff display modes: 'inline' (word-diff, default), 'split' (side-by-side), 'traditional' (+/- lines)
  const DIFF_MODES = ['inline', 'split', 'traditional'];
  const DIFF_MODE_KEY = 'sessionReview.diffMode';
  let globalDiffMode = loadDiffMode();
  const fileDiffModes = new Map(); // fileId (string) -> mode override; cleared when the global mode changes

  function loadDiffMode() {
    try {
      const m = localStorage.getItem(DIFF_MODE_KEY);
      return DIFF_MODES.includes(m) ? m : 'inline';
    } catch (e) {
      return 'inline';
    }
  }

  function saveDiffMode(mode) {
    try {
      localStorage.setItem(DIFF_MODE_KEY, mode);
    } catch (e) { /* storage unavailable */ }
  }

  // Initialize
  async function init() {
    const reviewId = getReviewIdFromUrl();
    if (!reviewId) {
      showError('No review ID in URL');
      return;
    }

    try {
      await loadReviewData(reviewId);
      renderPage();
      setupKeyboardNavigation();
      highlightSyntax();
    } catch (error) {
      showError(`Failed to load review: ${error.message}`);
    }
  }

  function getReviewIdFromUrl() {
    const match = window.location.pathname.match(/\/review\/(\d+)/);
    return match ? match[1] : null;
  }

  async function loadReviewData(reviewId) {
    const response = await fetch(`/api/reviews/${reviewId}`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    reviewData = await response.json();

    // Build lookup maps
    reviewData.files.forEach(file => {
      fileDataMap.set(file.id, file);
    });

    reviewData.annotations.forEach(annotation => {
      if (!annotationsByFile.has(annotation.file_id)) {
        annotationsByFile.set(annotation.file_id, []);
      }
      annotationsByFile.get(annotation.file_id).push(annotation);
    });
  }

  function renderPage() {
    renderHeader();
    renderFileTree();
    renderGeneralAnnotations();
    renderDiffs();
  }

  // ============================================================
  // Header
  // ============================================================

  function renderHeader() {
    const header = document.getElementById('review-header');
    const { review } = reviewData;
    document.title = review.title;

    const statusClass = review.status === 'published' ? 'status-published' : 'status-draft';
    const dateStr = new Date(review.session_date).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric'
    });

    // Truncate summary if too long
    const summaryText = review.summary || '';
    const summaryTruncated = summaryText.length > 300;
    const summaryShort = summaryTruncated ? summaryText.substring(0, 300) + '...' : summaryText;

    header.innerHTML = `
      <div class="review-title-row">
        <h1>${escapeHtml(review.title)}</h1>
        <span class="badge ${statusClass}">${review.status}</span>
      </div>
      <div class="review-meta">
        <span class="badge badge-project">${escapeHtml(review.project)}</span>
        <span class="branch-info">${escapeHtml(review.branch)} ← ${escapeHtml(review.base_ref)}</span>
        <span class="date">${dateStr}</span>
      </div>
      <div class="review-stats">
        <span>${review.total_files_changed} files changed</span>
        <span class="stat-additions">+${review.total_additions}</span>
        <span class="stat-deletions">-${review.total_deletions}</span>
        ${renderModeControl(null)}
        <button class="btn btn-small" id="refresh-review-btn" title="Re-fetch diff from branch and regenerate if changed">↻ Refresh</button>
        <button class="review-ask-btn" id="review-ask-btn">Ask Claude</button>
      </div>
      <div class="review-summary">
        <div class="summary-content" id="summary-short">${renderMarkdown(summaryShort)}</div>
        ${summaryTruncated ? `
          <div class="summary-content hidden" id="summary-full">${renderMarkdown(summaryText)}</div>
          <button class="btn-expand-text" id="summary-toggle">Show more</button>
        ` : ''}
      </div>
    `;

    if (summaryTruncated) {
      document.getElementById('summary-toggle').addEventListener('click', () => {
        const short = document.getElementById('summary-short');
        const full = document.getElementById('summary-full');
        const btn = document.getElementById('summary-toggle');
        const isExpanded = !full.classList.contains('hidden');
        short.classList.toggle('hidden', !isExpanded);
        full.classList.toggle('hidden', isExpanded);
        btn.textContent = isExpanded ? 'Show more' : 'Show less';
      });
    }

    // Global diff-mode control
    const globalSeg = document.getElementById('global-diff-mode');
    globalSeg.querySelectorAll('.diff-mode-seg-btn').forEach(btn => {
      btn.addEventListener('click', () => setGlobalDiffMode(btn.dataset.mode));
    });
    setSegActive(globalSeg, globalDiffMode);

    // Refresh button — re-fetch diff from branch, reload if changed
    document.getElementById('refresh-review-btn').addEventListener('click', async () => {
      const btn = document.getElementById('refresh-review-btn');
      const { review } = reviewData;
      if (!review.repo_path || !review.branch) {
        btn.textContent = '✗ No branch info';
        setTimeout(() => { btn.textContent = '↻ Refresh'; }, 2000);
        return;
      }
      btn.textContent = '↻ Checking…';
      btn.disabled = true;
      try {
        const params = new URLSearchParams({
          repo_path: review.repo_path,
          branch: review.branch,
          base_ref: review.base_ref || 'main'
        });
        const resp = await fetch(`/api/reviews/generate-branch?${params}`, { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'generated') {
          btn.textContent = '↻ Updated — reloading…';
          setTimeout(() => window.location.reload(), 500);
        } else {
          btn.textContent = '✓ Up to date';
          setTimeout(() => { btn.textContent = '↻ Refresh'; }, 2000);
        }
      } catch (e) {
        btn.textContent = '✗ Error';
        setTimeout(() => { btn.textContent = '↻ Refresh'; btn.disabled = false; }, 2000);
      }
    });
  }

  // ============================================================
  // File Tree
  // ============================================================

  function renderFileTree() {
    const container = document.getElementById('file-tree');
    const { files } = reviewData;

    if (files.length === 0) {
      container.innerHTML = '<p class="empty-state">No files changed</p>';
      return;
    }

    // Build directory tree
    const tree = buildFileTree(files);
    container.innerHTML = `
      <div class="file-tree-header">
        <h3>Files (${files.length})</h3>
      </div>
      <div class="file-tree-list">
        ${renderFileTreeNodes(tree)}
      </div>
    `;

    // Setup click handlers
    container.querySelectorAll('.file-tree-item').forEach(item => {
      item.addEventListener('click', () => {
        const fileId = parseInt(item.dataset.fileId);
        scrollToFile(fileId);
      });
    });

    container.querySelectorAll('.directory-toggle').forEach(toggle => {
      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        const dir = toggle.closest('.directory-node');
        dir.classList.toggle('collapsed');
      });
    });
  }

  function buildFileTree(files) {
    const tree = { children: {}, files: [] };

    files.forEach(file => {
      const parts = file.file_path.split('/');
      let current = tree;

      // Navigate/create directories
      for (let i = 0; i < parts.length - 1; i++) {
        const dirName = parts[i];
        if (!current.children[dirName]) {
          current.children[dirName] = { children: {}, files: [] };
        }
        current = current.children[dirName];
      }

      // Add file to current directory
      current.files.push(file);
    });

    return tree;
  }

  function renderFileTreeNodes(tree, depth = 0) {
    let html = '';

    // Render directories — collapse single-child chains into one line
    const dirNames = Object.keys(tree.children).sort();
    dirNames.forEach(dirName => {
      let subtree = tree.children[dirName];
      let collapsedName = dirName;

      // Collapse: if this dir has exactly one child dir and no files, merge them
      while (
        Object.keys(subtree.children).length === 1 &&
        subtree.files.length === 0
      ) {
        const onlyChild = Object.keys(subtree.children)[0];
        collapsedName += '/' + onlyChild;
        subtree = subtree.children[onlyChild];
      }

      const indent = depth * 14;
      html += `
        <div class="directory-node">
          <div class="directory-toggle" style="padding-left: ${indent}px">
            <span class="toggle-icon">▼</span>
            <span class="dir-name">${escapeHtml(collapsedName)}/</span>
          </div>
          <div class="directory-contents">
            ${renderFileTreeNodes(subtree, depth + 1)}
          </div>
        </div>
      `;
    });

    // Render files
    tree.files.forEach(file => {
      const icon = getChangeTypeIcon(file.change_type);
      const indent = depth * 14 + 8;
      const fileName = file.file_path.split('/').pop();
      html += `
        <div class="file-tree-item" data-file-id="${file.id}" style="padding-left: ${indent}px">
          <span class="change-icon ${file.change_type}">${icon}</span>
          <span class="file-name" title="${escapeHtml(file.file_path)}">${escapeHtml(fileName)}</span>
          <span class="file-stats">
            <span class="stat-additions">+${file.additions}</span>
            <span class="stat-deletions">-${file.deletions}</span>
          </span>
        </div>
      `;
    });

    return html;
  }

  function getChangeTypeIcon(changeType) {
    const icons = {
      added: '+',
      modified: '~',
      deleted: '-',
      renamed: '→',
      moved: '↗'
    };
    return icons[changeType] || '~';
  }

  // ============================================================
  // General Annotations (review-level, no file)
  // ============================================================

  function renderGeneralAnnotations() {
    const container = document.getElementById('general-annotations');
    const generalAnns = reviewData.annotations.filter(a => !a.file_id);

    if (generalAnns.length === 0) {
      container.classList.add('hidden');
      return;
    }

    container.classList.remove('hidden');

    // Deduplicate by content (first 200 chars)
    const seen = new Set();
    const unique = generalAnns.filter(a => {
      const key = a.content.substring(0, 200);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

    // Only show high-signal types; skip verbose context unless short
    const SHOW_TYPES = new Set(['plan', 'decision', 'warning', 'reasoning', 'change-summary']);
    const filtered = unique.filter(a => {
      if (SHOW_TYPES.has(a.annotation_type)) return true;
      // Show context only if short
      if (a.annotation_type === 'context' && a.content.length <= 200) return true;
      return false;
    });

    const TRUNCATE_LEN = 200;

    container.innerHTML = `
      <div class="general-annotations-toggle collapsed" id="general-ann-toggle">
        <span class="toggle-icon">▼</span>
        <h3>Session Notes (${filtered.length})</h3>
      </div>
      <div id="general-ann-list" class="hidden">
        ${filtered.map((ann, idx) => {
          const isTruncated = ann.content.length > TRUNCATE_LEN;
          const shortContent = isTruncated ? ann.content.substring(0, TRUNCATE_LEN) + '...' : ann.content;
          return `
            <div class="general-annotation-card annotation-${ann.annotation_type}">
              <div class="annotation-header">
                <span class="badge badge-annotation-type">${ann.annotation_type}</span>
              </div>
              <div class="annotation-content" id="gen-ann-short-${idx}">${escapeHtml(shortContent)}</div>
              ${isTruncated ? `
                <div class="annotation-content hidden" id="gen-ann-full-${idx}">${escapeHtml(ann.content)}</div>
                <button class="btn-expand-text gen-ann-expand" data-idx="${idx}">Show more</button>
              ` : ''}
            </div>
          `;
        }).join('')}
      </div>
    `;

    // Toggle section collapse
    document.getElementById('general-ann-toggle').addEventListener('click', () => {
      const toggle = document.getElementById('general-ann-toggle');
      const list = document.getElementById('general-ann-list');
      toggle.classList.toggle('collapsed');
      list.classList.toggle('hidden');
    });

    // Toggle individual annotation expand
    container.querySelectorAll('.gen-ann-expand').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = btn.dataset.idx;
        const short = document.getElementById(`gen-ann-short-${idx}`);
        const full = document.getElementById(`gen-ann-full-${idx}`);
        const isExpanded = !full.classList.contains('hidden');
        short.classList.toggle('hidden', !isExpanded);
        full.classList.toggle('hidden', isExpanded);
        btn.textContent = isExpanded ? 'Show more' : 'Show less';
      });
    });
  }

  // ============================================================
  // Diff View
  // ============================================================

  function renderDiffs() {
    const container = document.getElementById('diff-container');
    const { files } = reviewData;

    if (files.length === 0) {
      container.innerHTML = '<p class="empty-state">No diffs to display</p>';
      return;
    }

    container.innerHTML = files.map(file => renderFileDiff(file)).join('');

    wireDiffTables(container);

    // Wire "Go to file" buttons on move banners
    document.querySelectorAll('.move-goto-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const targetPath = btn.dataset.targetPath;
        const targetFile = reviewData.files.find(f => f.file_path === targetPath);
        if (targetFile) {
          scrollToFile(targetFile.id);
        }
      });
    });

    // Wire file-level Ask buttons
    document.querySelectorAll('.ask-file-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const fileId = parseInt(btn.dataset.fileId);
        openAskPanel('file', fileId, null, null, null);
      });
    });

    // Wire review-level Ask button
    const reviewAskBtn = document.getElementById('review-ask-btn');
    if (reviewAskBtn) {
      reviewAskBtn.addEventListener('click', () => {
        openAskPanel('review', null, null, null, null);
      });
    }

    // Wire markdown toggle buttons
    document.querySelectorAll('.markdown-toggle-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const fileId = btn.dataset.fileId;
        toggleMarkdownView(fileId, btn);
      });
    });

    // Wire diff-mode controls (per-file)
    document.querySelectorAll('.diff-mode-seg[data-file-id] .diff-mode-seg-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!btn.disabled) setFileDiffMode(btn.closest('.diff-mode-seg').dataset.fileId, btn.dataset.mode);
      });
    });

    // Wire pipeline view toggle buttons (per-file: pipeline ⇄ raw JSON diff)
    document.querySelectorAll('.pipeline-toggle-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        togglePipelineView(btn.dataset.fileId, btn);
      });
    });

    // Wire interactions inside rendered pipeline views (stage expand, etc.)
    if (typeof window.PipelineDiff !== 'undefined' && window.PipelineDiff.wire) {
      document.querySelectorAll('.pipeline-view').forEach(el => window.PipelineDiff.wire(el));
    }

    // Wire word-change click popovers (inline diff view)
    setupWordChangePopovers();

    // Show the right container per file (restores split from localStorage, sets per-file active states)
    document.querySelectorAll('.file-diff[data-file-id]').forEach(fd => showDiffContainer(fd.dataset.fileId));
  }

  // Wire the interactive bits of every diff table under root (annotations, expand, ask, whitespace hunks)
  function wireDiffTables(root) {
    root.querySelectorAll('.annotation-marker').forEach(marker => {
      marker.addEventListener('click', () => {
        toggleAnnotation(parseInt(marker.dataset.annotationId));
      });
    });

    root.querySelectorAll('.expand-context-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const fileId = parseInt(btn.dataset.fileId);
        const direction = btn.dataset.direction;
        const startLine = parseInt(btn.dataset.startLine);
        const endLine = parseInt(btn.dataset.endLine);
        expandContext(fileId, direction, startLine, endLine, btn);
      });
    });

    root.querySelectorAll('.ws-hunk-expand-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        toggleWhitespaceHunk(btn.dataset.hunkId, btn);
      });
    });

    setupAskHandlers(root);

    root.querySelectorAll('.annotation-card-row[data-annotation-id]').forEach(wireAnnotationCard);
  }

  let activeWordPopover = null;

  function setupWordChangePopovers() {
    document.addEventListener('click', (e) => {
      const wordEl = e.target.closest('.word-change[data-old-text]');
      if (wordEl) {
        e.stopPropagation();
        showWordChangePopover(wordEl);
        return;
      }
      // Click outside dismisses
      if (activeWordPopover && !e.target.closest('.word-popover')) {
        dismissWordPopover();
      }
    });
  }

  function showWordChangePopover(wordEl) {
    dismissWordPopover();

    const oldText = wordEl.getAttribute('data-old-text');
    const newText = wordEl.textContent;

    const popover = document.createElement('div');
    popover.className = 'word-popover';
    popover.innerHTML = `
      <div class="word-popover-row word-popover-old"><span class="word-popover-gutter">−</span><code>${escapeHtml(oldText)}</code></div>
      <div class="word-popover-row word-popover-new"><span class="word-popover-gutter">+</span><code>${escapeHtml(newText)}</code></div>
    `;

    document.body.appendChild(popover);
    activeWordPopover = popover;

    // Position near the word
    const rect = wordEl.getBoundingClientRect();
    const popRect = popover.getBoundingClientRect();

    let top = rect.top - popRect.height - 6;
    let left = rect.left + (rect.width / 2) - (popRect.width / 2);

    // Flip below if no room above
    if (top < 4) {
      top = rect.bottom + 6;
      popover.classList.add('word-popover-below');
    }
    // Keep within viewport
    left = Math.max(4, Math.min(left, window.innerWidth - popRect.width - 4));

    popover.style.top = `${top}px`;
    popover.style.left = `${left}px`;
  }

  function dismissWordPopover() {
    if (activeWordPopover) {
      activeWordPopover.remove();
      activeWordPopover = null;
    }
  }

  function toggleMarkdownView(fileId, buttonElement) {
    const markdownContainer = document.getElementById(`markdown-${fileId}`);
    if (!markdownContainer) return;

    const isShowingMarkdown = markdownContainer.style.display !== 'none';
    markdownContainer.style.display = isShowingMarkdown ? 'none' : 'block';
    buttonElement.textContent = isShowingMarkdown ? 'View Rendered' : 'View Diff';
    showDiffContainer(fileId);
  }

  // ---- Diff mode switching ----

  const MODE_TITLES = {
    inline: 'Word-level inline diff',
    split: 'Side-by-side diff',
    traditional: 'Unified +/- diff'
  };
  const MODE_LABELS = { inline: 'Inline', split: 'Split', traditional: 'Traditional' };

  // fileId null renders the global control; disabledModes greys out modes that don't apply
  function renderModeControl(fileId, disabledModes = []) {
    const attrs = fileId == null ? 'id="global-diff-mode"' : `data-file-id="${fileId}"`;
    const buttons = DIFF_MODES.map(m => {
      const disabled = disabledModes.includes(m);
      const title = disabled ? "Split view isn't useful for added/deleted files" : MODE_TITLES[m];
      return `<button type="button" class="diff-mode-seg-btn" data-mode="${m}" title="${title}"${disabled ? ' disabled' : ''}>${MODE_LABELS[m]}</button>`;
    }).join('');
    return `<div class="diff-mode-seg${fileId == null ? ' diff-mode-seg-global' : ''}" role="group" ${attrs}>${buttons}</div>`;
  }

  function setSegActive(seg, mode) {
    if (!seg) return;
    seg.querySelectorAll('.diff-mode-seg-btn').forEach(btn => {
      const active = btn.dataset.mode === mode;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  // Added/deleted files would show one empty half, so they stay inline in split mode
  function splitSupported(file) {
    return !!file && file.change_type !== 'added' && file.change_type !== 'deleted';
  }

  function resolvedMode(fileId) {
    const m = fileDiffModes.get(String(fileId)) || globalDiffMode;
    return m === 'split' && !splitSupported(fileDataMap.get(Number(fileId))) ? 'inline' : m;
  }

  // True while the markdown-rendered or pipeline view covers the diff tables
  function isAltViewShowing(fileId) {
    const el = document.getElementById(`markdown-${fileId}`) || document.getElementById(`pipeline-${fileId}`);
    return !!el && el.style.display !== 'none';
  }

  function showDiffContainer(fileId) {
    const mode = resolvedMode(fileId);
    const alt = isAltViewShowing(fileId);
    if (mode === 'split' && !alt) ensureSplitRendered(fileId);

    DIFF_MODES.forEach(m => {
      const el = document.getElementById(`diff-${m}-${fileId}`);
      if (el) el.style.display = (!alt && m === mode) ? 'block' : 'none';
    });
    setSegActive(document.querySelector(`.diff-mode-seg[data-file-id="${fileId}"]`), mode);
  }

  function setGlobalDiffMode(mode) {
    if (!DIFF_MODES.includes(mode)) return;
    const container = document.getElementById('diff-container');

    // Anchor on the first file still in view so the reader keeps their place
    const containerTop = container.getBoundingClientRect().top;
    const anchor = Array.from(container.querySelectorAll('.file-diff'))
      .find(fd => fd.getBoundingClientRect().bottom > containerTop);
    const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;

    const prevMode = globalDiffMode;
    globalDiffMode = mode;
    saveDiffMode(mode);
    fileDiffModes.clear();
    container.querySelectorAll('.file-diff[data-file-id]').forEach(fd => showDiffContainer(fd.dataset.fileId));
    setSegActive(document.getElementById('global-diff-mode'), mode);

    if (anchor) container.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
  }

  function setFileDiffMode(fileId, mode) {
    fileDiffModes.set(String(fileId), mode);
    showDiffContainer(fileId);
  }

  // ---- Lazy split rendering ----

  function countFullLines(content) {
    if (!content) return 0;
    const lines = content.split('\n');
    if (lines[lines.length - 1] === '') lines.pop();
    return lines.length;
  }

  function getMoveSections(file) {
    if (!file.move_metadata) return [];
    try {
      const meta = JSON.parse(file.move_metadata);
      return meta.type === 'consolidated_from' && meta.sections ? meta.sections : [];
    } catch (e) {
      return [];
    }
  }

  function ensureSplitRendered(fileId) {
    const el = document.getElementById(`diff-split-${fileId}`);
    if (!el || el.dataset.rendered === 'true') return;
    const file = fileDataMap.get(Number(fileId));
    if (!file) return;

    const lineAnnotations = (annotationsByFile.get(file.id) || []).filter(a => a.line_start);
    const rows = window.SplitDiff.buildRows(file.diff_text || '', {
      fileId: file.id,
      fullLineCount: countFullLines(file.full_content),
      moveSections: getMoveSections(file)
    });
    el.innerHTML = window.SplitDiff.wrapTable(window.SplitDiff.render(rows, {
      fileId: file.id,
      annotations: lineAnnotations,
      language: file.language,
      highlight: highlightCode,
      isExpanded: id => expandedAnnotations.has(id),
      renderAnnotationCard
    }));
    el.dataset.rendered = 'true';
    wireDiffTables(el);
  }

  function renderFileDiff(file) {
    // Specialized pipeline (.pp) renderer — falls through to the raw diff if the
    // module isn't loaded or the payload is missing/unparseable. A raw-diff view is
    // ALWAYS available: renderPipelineFileDiff embeds the diff tables + a toggle.
    if (file.render_mode === 'pipeline' && file.pipeline_diff
        && typeof window.PipelineDiff !== 'undefined') {
      try {
        return renderPipelineFileDiff(file);
      } catch (e) {
        console.error('Pipeline render failed, falling back to raw diff:', e);
      }
    }

    const changeTypeClass = `change-${file.change_type}`;
    const annotations = annotationsByFile.get(file.id) || [];

    // Separate file-level annotations (no line_start) from line-level
    const fileAnnotations = annotations.filter(a => !a.line_start);
    const lineAnnotations = annotations.filter(a => a.line_start);

    // Parse move metadata before rendering diff (parseDiff needs moveSections)
    let moveBanner = '';
    let moveSections = [];
    if (file.move_metadata) {
      try {
        const meta = JSON.parse(file.move_metadata);
        if (meta.type === 'moved_to') {
          // For moved (deleted) files: show ONLY a compact banner, no diff table
          const fileBanners = fileAnnotations.length > 0 ? fileAnnotations.map(a => `<div class="file-annotation-banner annotation-${a.annotation_type}"><span class="badge badge-annotation-type">${a.annotation_type}</span> <span class="banner-md">${renderMarkdown(a.content)}</span></div>`).join('') : '';
          return `
            <div class="file-diff" id="file-${file.id}" data-file-id="${file.id}">
              <div class="file-header ${changeTypeClass}">
                <div class="file-header-left">
                  <span class="file-path">${escapeHtml(file.file_path)}</span>
                </div>
                <div class="file-header-right">
                  <span class="badge badge-change-type">${file.change_type}</span>
                </div>
              </div>
              <div class="move-banner move-banner-compact">↗ Deleted and moved to <code>${escapeHtml(meta.target)}</code> (${meta.similarity}% similar) <button class="move-goto-btn" data-target-path="${escapeHtml(meta.target)}">Go to file →</button></div>
              ${fileBanners}
            </div>
          `;
        } else if (meta.type === 'consolidated_from') {
          const sources = meta.sources.map(s => `<code>${escapeHtml(s.source)}</code> (${s.similarity}%)`).join(', ');
          moveBanner = `<div class="move-banner">↙ Consolidated from ${sources} — gray lines are unchanged, only moved</div>`;
          moveSections = getMoveSections(file);
        }
      } catch (e) { /* ignore bad JSON */ }
    }

    const traditionalHtml = parseDiff(file.diff_text, file.full_content, file.id, lineAnnotations, file.language, moveSections);
    const inlineHtml = parseDiffInline(file.diff_text, file.full_content, file.id, lineAnnotations, file.language, moveSections);

    // Render file-level annotation banners
    const fileBanners = fileAnnotations.length > 0 ? fileAnnotations.map(a => `<div class="file-annotation-banner annotation-${a.annotation_type}"><span class="badge badge-annotation-type">${a.annotation_type}</span> <span class="banner-md">${renderMarkdown(a.content)}</span></div>`).join('') : '';

    // Check if this is a markdown file
    const isMarkdown = file.file_path.endsWith('.md');
    const markdownToggleBtn = isMarkdown && file.full_content ? `<button class="markdown-toggle-btn" data-file-id="${file.id}" title="Toggle between rendered and diff view">View Diff</button>` : '';

    // Diff mode control
    const diffModeBtn = renderModeControl(file.id, splitSupported(file) ? [] : ['split']);

    // Render markdown content if available
    let markdownHtml = '';
    if (isMarkdown && file.full_content && typeof marked !== 'undefined') {
      try {
        const oldContent = file.change_type === 'added' ? '' : window.MarkdownDiff.reconstructOld(file.diff_text || '', file.full_content);
        const rendered = window.MarkdownDiff.render(oldContent, file.full_content, marked);
        markdownHtml = `
          <div class="markdown-rendered-container" id="markdown-${file.id}">
            ${rendered}
          </div>
        `;
      } catch (e) {
        console.error('Markdown rendering failed:', e);
      }
    }

    const hideForMarkdown = isMarkdown && file.full_content;

    return `
      <div class="file-diff" id="file-${file.id}" data-file-id="${file.id}">
        <div class="file-header ${changeTypeClass}">
          <div class="file-header-left">
            <span class="file-path">${escapeHtml(file.file_path)}</span>
            <button class="copy-path-btn" onclick="navigator.clipboard.writeText('${escapeHtml(file.file_path)}'); this.textContent='Copied!'; setTimeout(()=>this.textContent='Copy', 1000)" title="Copy file path">Copy</button>
            <button class="ask-file-btn" data-file-id="${file.id}" title="Ask Claude about this file">Ask</button>
            ${diffModeBtn}
            ${markdownToggleBtn}
          </div>
          <div class="file-header-right">
            <span class="badge badge-change-type">${file.change_type}</span>
            <span class="file-stats">
              <span class="stat-additions">+${file.additions}</span>
              <span class="stat-deletions">-${file.deletions}</span>
            </span>
          </div>
        </div>
        ${moveBanner}
        ${fileBanners}
        ${markdownHtml}
        <div class="diff-table-container diff-inline" id="diff-inline-${file.id}" ${hideForMarkdown || globalDiffMode !== 'inline' ? 'style="display: none;"' : ''}>
          <table class="diff-table">
            <tbody>
              ${inlineHtml}
            </tbody>
          </table>
        </div>
        <div class="diff-table-container diff-traditional" id="diff-traditional-${file.id}" ${hideForMarkdown || globalDiffMode !== 'traditional' ? 'style="display: none;"' : ''}>
          <table class="diff-table">
            <tbody>
              ${traditionalHtml}
            </tbody>
          </table>
        </div>
        <div class="diff-table-container diff-split" id="diff-split-${file.id}" data-rendered="false" style="display: none;"></div>
      </div>
    `;
  }

  function renderPipelineFileDiff(file) {
    const changeTypeClass = `change-${file.change_type}`;
    const annotations = annotationsByFile.get(file.id) || [];
    const lineAnnotations = annotations.filter(a => a.line_start);
    const fileAnnotations = annotations.filter(a => !a.line_start);

    // Specialized pipeline view (from pipeline-diff.js).
    const pipelineHtml = window.PipelineDiff.render(file);

    // Raw diff tables — hidden by default, shown via the toggle. Always available.
    const traditionalHtml = parseDiff(file.diff_text, file.full_content, file.id, lineAnnotations, file.language, []);
    const inlineHtml = parseDiffInline(file.diff_text, file.full_content, file.id, lineAnnotations, file.language, []);

    const fileBanners = fileAnnotations.length > 0 ? fileAnnotations.map(a => `<div class="file-annotation-banner annotation-${a.annotation_type}"><span class="badge badge-annotation-type">${a.annotation_type}</span> <span class="banner-md">${renderMarkdown(a.content)}</span></div>`).join('') : '';

    return `
      <div class="file-diff" id="file-${file.id}" data-file-id="${file.id}">
        <div class="file-header ${changeTypeClass}">
          <div class="file-header-left">
            <span class="file-path">${escapeHtml(file.file_path)}</span>
            <span class="badge badge-pipeline" title="Rendered as a Spinnaker pipeline diff">↯ pipeline</span>
            <button class="copy-path-btn" onclick="navigator.clipboard.writeText('${escapeHtml(file.file_path)}'); this.textContent='Copied!'; setTimeout(()=>this.textContent='Copy', 1000)" title="Copy file path">Copy</button>
            <button class="ask-file-btn" data-file-id="${file.id}" title="Ask Claude about this file">Ask</button>
            <button class="pipeline-toggle-btn" data-file-id="${file.id}" title="Toggle between the pipeline view and the raw JSON diff">View Raw Diff</button>
          </div>
          <div class="file-header-right">
            <span class="badge badge-change-type">${file.change_type}</span>
            <span class="file-stats">
              <span class="stat-additions">+${file.additions}</span>
              <span class="stat-deletions">-${file.deletions}</span>
            </span>
          </div>
        </div>
        ${fileBanners}
        <div class="pipeline-view" id="pipeline-${file.id}">
          ${pipelineHtml}
        </div>
        <div class="diff-table-container diff-inline" id="diff-inline-${file.id}" style="display: none;">
          <table class="diff-table"><tbody>${inlineHtml}</tbody></table>
        </div>
        <div class="diff-table-container diff-traditional" id="diff-traditional-${file.id}" style="display: none;">
          <table class="diff-table"><tbody>${traditionalHtml}</tbody></table>
        </div>
        <div class="diff-table-container diff-split" id="diff-split-${file.id}" data-rendered="false" style="display: none;"></div>
      </div>
    `;
  }

  function togglePipelineView(fileId, buttonElement) {
    const pipelineContainer = document.getElementById(`pipeline-${fileId}`);
    if (!pipelineContainer) return;

    const showingPipeline = pipelineContainer.style.display !== 'none';
    pipelineContainer.style.display = showingPipeline ? 'none' : 'block';
    buttonElement.textContent = showingPipeline ? 'View Pipeline' : 'View Raw Diff';
    showDiffContainer(fileId);
  }

  function parseDiff(diffText, fullContent, fileId, annotations, language, moveSections) {
    const lines = diffText.split('\n');
    const fullLineCount = countFullLines(fullContent);
    let html = '';
    let oldLineNum = 0;
    let newLineNum = 0;
    let lastNewLineShown = 0; // Track what we've shown for expand buttons

    // Track which move sections have been rendered
    const sections = (moveSections || []).slice().sort((a, b) => a.target_start - b.target_start);
    let nextSectionIdx = 0;

    // First pass: parse hunks and detect whitespace-only hunks
    const hunks = parseHunks(lines);

    // Render each hunk
    for (const hunk of hunks) {
      // Check if we need an expand button before this hunk
      // A pure-deletion hunk (+X,0) reports the line before the deletion as newStart
      const effNew = hunk.newCount === 0 ? hunk.newStart + 1 : hunk.newStart;
      if (fullContent && effNew > lastNewLineShown + 1) {
        const missingStart = lastNewLineShown + 1;
        const missingEnd = effNew - 1;
        const missingCount = missingEnd - missingStart + 1;
        html += renderExpandButton(fileId, 'before', missingStart, missingEnd, missingCount);
      }

      oldLineNum = hunk.oldStart;
      newLineNum = effNew;

      // Render hunk header
      const hunkLabel = hunk.context ? escapeHtml(hunk.context) : `Lines ${hunk.newStart}...`;
      html += `<tr class="hunk-header"><td class="line-num old"></td><td class="line-num new"></td><td class="gutter"></td><td class="line-content"><span class="hunk-label">${hunkLabel}</span></td></tr>`;

      // If hunk is whitespace-only, render collapsed indicator
      if (hunk.isWhitespaceOnly) {
        html += renderWhitespaceHunk(fileId, hunk, language, annotations);
        // Update line numbers for whitespace-only hunk
        oldLineNum += hunk.lines.filter(l => l.type === 'deletion' || l.type === 'context').length;
        newLineNum += hunk.lines.filter(l => l.type === 'addition' || l.type === 'context').length;
        lastNewLineShown = newLineNum - 1;
      } else {
        // Render normal hunk
        for (const diffLine of hunk.lines) {
          // Insert move section banners when entering a new source region
          if (sections.length > 0 && nextSectionIdx < sections.length && (diffLine.type === 'addition' || diffLine.type === 'context')) {
            while (nextSectionIdx < sections.length && newLineNum >= sections[nextSectionIdx].target_start) {
              const sec = sections[nextSectionIdx];
              const srcName = sec.source.split('/').pop();
              html += `<tr class="move-section-banner"><td colspan="4"><div class="move-section-label">↙ From <code>${escapeHtml(sec.source)}</code> (${sec.similarity}% similar) — lines ${sec.target_start}–${sec.target_end}</div></td></tr>`;
              nextSectionIdx++;
            }
          }

          if (diffLine.type === 'addition') {
            const annotationsHere = annotations.filter(a => a.line_start === newLineNum);
            html += renderDiffLine(null, newLineNum, diffLine.content, 'addition', annotationsHere, language);
            newLineNum++;
            lastNewLineShown = newLineNum - 1;
          } else if (diffLine.type === 'deletion') {
            html += renderDiffLine(oldLineNum, null, diffLine.content, 'deletion', [], language);
            oldLineNum++;
          } else if (diffLine.type === 'context') {
            const annotationsHere = annotations.filter(a => a.line_start === newLineNum);
            html += renderDiffLine(oldLineNum, newLineNum, diffLine.content, 'context', annotationsHere, language);
            oldLineNum++;
            newLineNum++;
            lastNewLineShown = newLineNum - 1;
          }
        }
      }
      lastNewLineShown = newLineNum - 1;
    }

    // Expand button at end if needed
    if (fullContent && lastNewLineShown < fullLineCount) {
      const missingStart = lastNewLineShown + 1;
      const missingEnd = fullLineCount;
      const missingCount = missingEnd - missingStart + 1;
      html += renderExpandButton(fileId, 'after', missingStart, missingEnd, missingCount);
    }

    return html;
  }

  // ============================================================
  // Word-Diff (Inline View)
  // ============================================================

  function renderWordDiffContent(oldContent, newContent, language) {
    const segments = computeWordDiff(oldContent, newContent);
    let html = '';

    for (const seg of segments) {
      if (seg.type === 'equal') {
        html += escapeHtml(seg.text);
      } else if (seg.type === 'replace') {
        const escapedOld = escapeHtml(seg.oldText);
        html += `<span class="word-change" data-old-text="${escapedOld}">${escapeHtml(seg.newText)}</span>`;
      } else if (seg.type === 'insert') {
        html += `<span class="word-insert">${escapeHtml(seg.text)}</span>`;
      }
      // 'delete' segments are hidden in inline mode (available via hover on replace)
    }

    return html;
  }

  function parseDiffInline(diffText, fullContent, fileId, annotations, language, moveSections) {
    const lines = diffText.split('\n');
    const fullLineCount = countFullLines(fullContent);
    let html = '';
    let oldLineNum = 0;
    let newLineNum = 0;
    let lastNewLineShown = 0;

    const sections = (moveSections || []).slice().sort((a, b) => a.target_start - b.target_start);
    let nextSectionIdx = 0;

    const hunks = parseHunks(lines);

    for (const hunk of hunks) {
      // Expand button before hunk
      // A pure-deletion hunk (+X,0) reports the line before the deletion as newStart
      const effNew = hunk.newCount === 0 ? hunk.newStart + 1 : hunk.newStart;
      if (fullContent && effNew > lastNewLineShown + 1) {
        const missingStart = lastNewLineShown + 1;
        const missingEnd = effNew - 1;
        const missingCount = missingEnd - missingStart + 1;
        html += renderExpandButton(fileId, 'before', missingStart, missingEnd, missingCount);
      }

      oldLineNum = hunk.oldStart;
      newLineNum = effNew;

      // Hunk header
      const hunkLabel = hunk.context ? escapeHtml(hunk.context) : `Lines ${hunk.newStart}...`;
      html += `<tr class="hunk-header"><td class="line-num old"></td><td class="line-num new"></td><td class="gutter"></td><td class="line-content"><span class="hunk-label">${hunkLabel}</span></td></tr>`;

      if (hunk.isWhitespaceOnly) {
        html += renderWhitespaceHunk(fileId, hunk, language, annotations);
        oldLineNum += hunk.lines.filter(l => l.type === 'deletion' || l.type === 'context').length;
        newLineNum += hunk.lines.filter(l => l.type === 'addition' || l.type === 'context').length;
        lastNewLineShown = newLineNum - 1;
      } else {
        // Pair deletions with additions for word-level diffing
        const paired = pairHunkLines(hunk.lines);

        for (const item of paired) {
          // Move section banners
          if (sections.length > 0 && nextSectionIdx < sections.length && item.kind !== 'pure-deletion') {
            const checkLineNum = item.kind === 'paired' ? newLineNum : newLineNum;
            while (nextSectionIdx < sections.length && checkLineNum >= sections[nextSectionIdx].target_start) {
              const sec = sections[nextSectionIdx];
              html += `<tr class="move-section-banner"><td colspan="4"><div class="move-section-label">↙ From <code>${escapeHtml(sec.source)}</code> (${sec.similarity}% similar) — lines ${sec.target_start}–${sec.target_end}</div></td></tr>`;
              nextSectionIdx++;
            }
          }

          if (item.kind === 'context') {
            const annotationsHere = annotations.filter(a => a.line_start === newLineNum);
            html += renderDiffLine(oldLineNum, newLineNum, item.line.content, 'context', annotationsHere, language);
            oldLineNum++;
            newLineNum++;
            lastNewLineShown = newLineNum - 1;
          } else if (item.kind === 'paired') {
            // Render single line with word-level highlights
            const annotationsHere = annotations.filter(a => a.line_start === newLineNum);
            const wordDiffHtml = renderWordDiffContent(item.old.content, item.new.content, language);

            const annotationMarkers = annotationsHere.length > 0 ? annotationsHere.map(a =>
              window.SplitDiff.markerHtml(a)
            ).join('') : '';

            html += `<tr class="diff-line line-inline-change"><td class="line-num old">${oldLineNum}</td><td class="line-num new">${newLineNum}</td><td class="gutter" title="Modified line">~</td><td class="line-content">${annotationMarkers}<code>${wordDiffHtml}</code></td></tr>`;

            // Add annotation cards if expanded
            annotationsHere.forEach(annotation => {
              if (expandedAnnotations.has(annotation.id)) {
                html += renderAnnotationCard(annotation);
              }
            });

            oldLineNum++;
            newLineNum++;
            lastNewLineShown = newLineNum - 1;
          } else if (item.kind === 'pure-addition') {
            const annotationsHere = annotations.filter(a => a.line_start === newLineNum);
            html += renderDiffLine(null, newLineNum, item.line.content, 'addition', annotationsHere, language);
            newLineNum++;
            lastNewLineShown = newLineNum - 1;
          } else if (item.kind === 'pure-deletion') {
            // Render as a subtle deleted-line indicator
            const deletedId = `del-${fileId}-${oldLineNum}`;
            const highlightedContent = highlightCode(item.line.content, language);
            html += `<tr class="diff-line line-deletion-collapsed" data-deleted-id="${deletedId}">
              <td class="line-num old">${oldLineNum}</td><td class="line-num new"></td>
              <td class="gutter"><span class="deleted-indicator" data-deleted-id="${deletedId}" title="Click to show deleted line">−</span></td>
              <td class="line-content"><code class="deleted-line-content">${highlightedContent}</code></td>
            </tr>`;
            oldLineNum++;
          }
        }
      }
      lastNewLineShown = newLineNum - 1;
    }

    // Expand button at end
    if (fullContent && lastNewLineShown < fullLineCount) {
      const missingStart = lastNewLineShown + 1;
      const missingEnd = fullLineCount;
      const missingCount = missingEnd - missingStart + 1;
      html += renderExpandButton(fileId, 'after', missingStart, missingEnd, missingCount);
    }

    return html;
  }

  function renderWhitespaceHunk(fileId, hunk, language, annotations) {
    const totalLines = hunk.lines.length;
    const hunkId = `ws-hunk-${fileId}-${hunk.newStart}`;

    // Build the full hunk HTML for when expanded (wrapped in a hidden container row)
    let hunkHtml = '';
    let oldNum = hunk.oldStart;
    let newNum = hunk.newStart;

    for (const diffLine of hunk.lines) {
      if (diffLine.type === 'addition') {
        const annotationsHere = annotations.filter(a => a.line_start === newNum);
        hunkHtml += renderDiffLine(null, newNum, diffLine.content, 'addition', annotationsHere, language);
        newNum++;
      } else if (diffLine.type === 'deletion') {
        hunkHtml += renderDiffLine(oldNum, null, diffLine.content, 'deletion', [], language);
        oldNum++;
      } else if (diffLine.type === 'context') {
        const annotationsHere = annotations.filter(a => a.line_start === newNum);
        hunkHtml += renderDiffLine(oldNum, newNum, diffLine.content, 'context', annotationsHere, language);
        oldNum++;
        newNum++;
      }
    }

    return `
      <tr class="whitespace-hunk-collapsed" data-hunk-id="${hunkId}">
        <td colspan="4">
          <div class="whitespace-hunk-indicator">
            <span class="ws-hunk-icon">⋮</span>
            <span class="ws-hunk-text">formatting changes (${totalLines} lines)</span>
            <button class="ws-hunk-expand-btn" data-hunk-id="${hunkId}">expand</button>
          </div>
        </td>
      </tr>
      <tr class="whitespace-hunk-content-wrapper hidden" data-hunk-id="${hunkId}">
        <td colspan="4" style="padding: 0;">
          <table class="diff-table whitespace-hunk-content">
            <tbody>
              ${hunkHtml}
            </tbody>
          </table>
        </td>
      </tr>
    `;
  }

  function renderDiffLine(oldNum, newNum, content, type, annotations, language) {
    const oldNumStr = oldNum ? oldNum : '';
    const newNumStr = newNum ? newNum : '';
    const typeClass = `line-${type}`;

    // Determine gutter symbol
    let gutterSymbol = '';
    if (type === 'addition') gutterSymbol = '+';
    else if (type === 'deletion') gutterSymbol = '-';

    // Check for annotation markers
    const hasAnnotations = annotations.length > 0;
    const annotationMarkers = hasAnnotations ? annotations.map(a =>
      window.SplitDiff.markerHtml(a)
    ).join('') : '';

    // Apply syntax highlighting to content
    const highlightedContent = highlightCode(content, language);

    let html = `<tr class="diff-line ${typeClass}"><td class="line-num old">${oldNumStr}</td><td class="line-num new">${newNumStr}</td><td class="gutter">${gutterSymbol}</td><td class="line-content">${annotationMarkers}<code>${highlightedContent}</code></td></tr>`;

    // Add annotation cards if expanded
    annotations.forEach(annotation => {
      if (expandedAnnotations.has(annotation.id)) {
        html += renderAnnotationCard(annotation);
      }
    });

    return html;
  }

  function renderExpandButton(fileId, direction, startLine, endLine, count) {
    const label = direction === 'before' ? `⬆ Show ${count} more lines` :
                  direction === 'after' ? `⬇ Show ${count} more lines` :
                  `Show all`;

    return `
      <tr class="expand-context-row">
        <td colspan="4">
          <button class="expand-context-btn"
                  data-file-id="${fileId}"
                  data-direction="${direction}"
                  data-start-line="${startLine}"
                  data-end-line="${endLine}">
            ${label}
          </button>
        </td>
      </tr>
    `;
  }

  function expandContext(fileId, direction, startLine, endLine, buttonElement) {
    const file = fileDataMap.get(fileId);
    if (!file || !file.full_content) return;

    const fullLines = file.full_content.split('\n');
    const linesToShow = fullLines.slice(startLine - 1, endLine); // Convert to 0-indexed

    // Build HTML for new lines
    const isSplit = !!buttonElement.closest('.diff-table-split');
    const oldStart = parseInt(buttonElement.dataset.oldStart) || startLine;
    let html = '';
    for (let i = 0; i < linesToShow.length; i++) {
      const lineNum = startLine + i;
      const content = linesToShow[i];
      html += isSplit
        ? window.SplitDiff.renderContextRow(oldStart + i, lineNum, highlightCode(content, file.language), true)
        : renderDiffLine(lineNum, lineNum, content, 'context-expanded', [], file.language);
    }

    // Replace button with new lines
    const row = buttonElement.closest('tr');
    const tbody = row.parentElement;
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = `<table><tbody>${html}</tbody></table>`;
    const newRows = Array.from(tempDiv.querySelector('tbody').children);

    newRows.forEach(newRow => {
      newRow.querySelectorAll('.line-num').forEach(c => c.addEventListener('click', handleLineNumClick));
      tbody.insertBefore(newRow, row);
    });

    row.remove();
  }

  function toggleWhitespaceHunk(hunkId, btn) {
    // The same hunk id exists in several mode containers, so look within the clicked one
    const scope = (btn && btn.closest('.diff-table-container')) || document;
    const collapsedRow = scope.querySelector(`.whitespace-hunk-collapsed[data-hunk-id="${hunkId}"]`);
    const contentWrapper = scope.querySelector(`.whitespace-hunk-content-wrapper[data-hunk-id="${hunkId}"]`);
    const button = scope.querySelector(`.ws-hunk-expand-btn[data-hunk-id="${hunkId}"]`);

    if (!collapsedRow || !contentWrapper || !button) return;

    const isExpanded = !contentWrapper.classList.contains('hidden');

    if (isExpanded) {
      // Collapse
      contentWrapper.classList.add('hidden');
      collapsedRow.classList.remove('hidden');
      button.textContent = 'expand';
    } else {
      // Expand
      contentWrapper.classList.remove('hidden');
      collapsedRow.classList.add('hidden');
      button.textContent = 'collapse';
    }
  }

  // ============================================================
  // Annotations
  // ============================================================

  function renderAnnotationCard(annotation, colspan = 4) {
    const typeClass = `annotation-${annotation.annotation_type}`;
    return `
      <tr class="annotation-card-row" data-annotation-id="${annotation.id}">
        <td colspan="${colspan}">
          <div class="annotation-card note-card ${typeClass}">
            <div class="annotation-header">
              <span class="note-title">💬 Claude's note · ${escapeHtml(annotation.annotation_type)}</span>
              <button type="button" class="note-close" title="Close note" aria-label="Close note">×</button>
            </div>
            <div class="note-body">${renderMarkdown(annotation.content)}</div>
          </div>
        </td>
      </tr>
    `;
  }

  // The × closes the card exactly like clicking its marker again
  function wireAnnotationCard(cardRow) {
    const closeBtn = cardRow && cardRow.querySelector('.note-close');
    if (!closeBtn) return;
    closeBtn.addEventListener('click', () => toggleAnnotation(parseInt(cardRow.dataset.annotationId)));
  }

  // colspan for full-width rows inserted into the table containing el
  function colspanFor(el) {
    return el && el.closest('.diff-table-split') ? window.SplitDiff.COLSPAN : 4;
  }

  // Parse one <tr> from HTML (firstElementChild: the templates start with whitespace)
  function rowFromHtml(html) {
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = `<table><tbody>${html}</tbody></table>`;
    return tempDiv.querySelector('tbody').firstElementChild;
  }

  function toggleAnnotation(annotationId) {
    if (expandedAnnotations.has(annotationId)) {
      expandedAnnotations.delete(annotationId);
      // Remove every copy of the card (one per rendered mode)
      document.querySelectorAll(`.annotation-card-row[data-annotation-id="${annotationId}"]`)
        .forEach(card => card.remove());
    } else {
      expandedAnnotations.add(annotationId);
      const annotation = reviewData.annotations.find(a => a.id === annotationId);
      if (!annotation) return;
      // Insert a card after the row of each marker copy, including hidden modes
      document.querySelectorAll(`.annotation-marker[data-annotation-id="${annotationId}"]`).forEach(marker => {
        const row = marker.closest('tr');
        const cardRow = rowFromHtml(renderAnnotationCard(annotation, colspanFor(row)));
        row.parentElement.insertBefore(cardRow, row.nextSibling);
        wireAnnotationCard(cardRow);
      });
    }
  }

  // ============================================================
  // Markdown Rendering
  // ============================================================

  // Small markdown subset. All input is HTML-escaped first; only tags produced here reach the DOM.
  function renderMarkdown(text) {
    if (!text) return '';
    const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const stash = [];
    const hold = (h) => `\u0000${stash.push(h) - 1}\u0000`;

    let html = esc(text);

    // Fenced code blocks, then inline code: stashed so later rules leave them alone
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) =>
      hold(`<pre><code class="language-${lang}">${code}</code></pre>`));
    html = html.replace(/`([^`\n]+)`/g, (m, code) => hold(`<code>${code}</code>`));

    // Tables
    html = html.replace(/(\|.+\|\n)+/g, (match) => {
      const rows = match.trim().split('\n');
      if (rows.length < 2) return match;
      const cells = (row) => row.split('|').map(s => s.trim()).filter(s => s);
      let t = '<table class="markdown-table"><thead><tr>';
      cells(rows[0]).forEach(c => { t += `<th>${c}</th>`; });
      t += '</tr></thead><tbody>';
      rows.slice(2).forEach(r => { t += '<tr>' + cells(r).map(c => `<td>${c}</td>`).join('') + '</tr>'; });
      return hold(t + '</tbody></table>');
    });

    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Links: http(s) or relative only (no javascript: URLs); text/href are already escaped
    html = html.replace(/\[([^\]]+?)\]\(((?:https?:\/\/|\/)[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/\n/g, '<br>');
    return html.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[+i]);
  }

  // ============================================================
  // Navigation
  // ============================================================

  function scrollToFile(fileId) {
    const fileElement = document.getElementById(`file-${fileId}`);
    if (fileElement) {
      fileElement.scrollIntoView({ behavior: 'smooth', block: 'start' });
      // Highlight briefly
      fileElement.classList.add('highlight');
      setTimeout(() => fileElement.classList.remove('highlight'), 2000);
    }
  }

  function setupKeyboardNavigation() {
    const fileIds = reviewData.files.map(f => f.id);

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
        return; // Don't interfere with input fields
      }

      if (e.key === 'j') {
        // Next file
        currentFileIndex = Math.min(currentFileIndex + 1, fileIds.length - 1);
        scrollToFile(fileIds[currentFileIndex]);
      } else if (e.key === 'k') {
        // Previous file
        currentFileIndex = Math.max(currentFileIndex - 1, 0);
        scrollToFile(fileIds[currentFileIndex]);
      }
    });
  }

  // ============================================================
  // Syntax Highlighting
  // ============================================================

  function highlightCode(text, language) {
    if (!text) return '';

    // We need to work with the raw text and track which parts should be escaped vs highlighted
    // Strategy: identify tokens, escape everything, then wrap tokens in spans

    const tokens = [];
    let lastIndex = 0;

    // Helper to add a token
    function addToken(start, end, type) {
      tokens.push({ start, end, type, text: text.substring(start, end) });
    }

    // Multi-line comments (/* ... */)
    const mlCommentRegex = /\/\*[\s\S]*?\*\//g;
    let match;
    while ((match = mlCommentRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'comment');
    }

    // Single-line comments (// ... or # ...)
    const slCommentRegex = /(\/\/[^\n]*|#[^\n]*)/g;
    while ((match = slCommentRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'comment');
    }

    // Strings (triple quotes, double quotes, single quotes, backticks)
    const stringRegex = /"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`/g;
    while ((match = stringRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'string');
    }

    // Annotations (@Override, @Test, etc.)
    const annotationRegex = /@\w+/g;
    while ((match = annotationRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'annotation');
    }

    // Keywords
    const keywords = [
      'def', 'val', 'var', 'trait', 'object', 'class', 'extends', 'import', 'package',
      'override', 'lazy', 'sealed', 'case', 'match', 'if', 'else', 'for', 'while',
      'return', 'new', 'yield', 'with', 'type', 'abstract', 'final', 'private',
      'protected', 'implicit', 'given', 'using', 'enum', 'then', 'do', 'end',
      'function', 'const', 'let', 'async', 'await', 'export', 'from', 'try', 'catch',
      'finally', 'throw', 'throws', 'instanceof', 'typeof', 'void', 'null', 'undefined',
      'true', 'false', 'this', 'super', 'static', 'public', 'interface', 'implements',
      'as', 'in', 'is', 'not', 'and', 'or', 'lambda', 'pass', 'break',
      'continue', 'elif', 'except', 'raise', 'assert', 'global', 'nonlocal', 'del'
    ];
    const keywordRegex = new RegExp(`\\b(${keywords.join('|')})\\b`, 'g');
    while ((match = keywordRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'keyword');
    }

    // Type names (capitalized words)
    const typeRegex = /\b([A-Z][a-zA-Z0-9_]*)\b/g;
    while ((match = typeRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'type');
    }

    // Numbers
    const numberRegex = /\b(\d+\.?\d*)\b/g;
    while ((match = numberRegex.exec(text)) !== null) {
      addToken(match.index, match.index + match[0].length, 'number');
    }

    // Sort tokens by start position
    tokens.sort((a, b) => a.start - b.start);

    // Remove overlapping tokens (keep the first one found)
    const filteredTokens = [];
    let lastEnd = 0;
    for (const token of tokens) {
      if (token.start >= lastEnd) {
        filteredTokens.push(token);
        lastEnd = token.end;
      }
    }

    // Build HTML by escaping everything and wrapping tokens
    let html = '';
    lastIndex = 0;
    for (const token of filteredTokens) {
      // Add text before token (escaped)
      if (token.start > lastIndex) {
        html += escapeHtml(text.substring(lastIndex, token.start));
      }
      // Add token (escaped and wrapped)
      html += `<span class="token ${token.type}">${escapeHtml(token.text)}</span>`;
      lastIndex = token.end;
    }
    // Add remaining text (escaped)
    if (lastIndex < text.length) {
      html += escapeHtml(text.substring(lastIndex));
    }

    return html;
  }

  function highlightSyntax() {
    // Not using Prism anymore - we do custom highlighting per-line
  }

  // ============================================================
  // Ask Claude
  // ============================================================

  let activeAskRow = null; // Only one ask-input open at a time

  // Agent shown in the UI; /api/ask/status reports the configured one (default claude)
  let askName = 'Claude';
  let ASK_UNAVAILABLE_MSG = 'Ask needs the Claude CLI (`claude`) on this machine.';
  let ASK_HINT = 'Questions only — Claude reads the code but won\'t change anything.';
  let askAvailable = true;
  const askStatusPromise = fetch('/api/ask/status').then(r => r.json()).then(d => {
    askAvailable = !!d.available;
    if (d.name) {
      askName = d.name;
      ASK_HINT = `Questions only — ${askName} reads the code but won't change anything.`;
    }
    if (d.message) ASK_UNAVAILABLE_MSG = d.message;
    applyAskStatus();
  }).catch(() => {});

  // Label the Ask entry points with the agent; grey them out (reason as tooltip) when it isn't available
  function applyAskStatus() {
    document.body.classList.toggle('ask-unavailable', !askAvailable);
    document.querySelectorAll('.review-ask-btn').forEach(b => { b.textContent = `Ask ${askName}`; });
    document.querySelectorAll('.review-ask-btn, .ask-file-btn').forEach(b => {
      b.title = askAvailable ? (b.classList.contains('ask-file-btn') ? `Ask ${askName} about this file` : '') : ASK_UNAVAILABLE_MSG;
    });
  }

  function setupAskHandlers(root = document) {
    applyAskStatus();
    root.querySelectorAll('.diff-table .line-num').forEach(cell => {
      cell.addEventListener('click', handleLineNumClick);
    });
  }

  function handleLineNumClick(e) {
    const cell = e.currentTarget;
    const row = cell.closest('tr');
    if (!row || !row.classList.contains('diff-line')) return;

    const newNum = row.querySelector('.line-num.new')?.textContent?.trim();
    const oldNum = row.querySelector('.line-num.old')?.textContent?.trim();
    const lineNumber = parseInt(newNum || oldNum);
    if (!lineNumber) return;

    const fileDiff = row.closest('.file-diff');
    if (!fileDiff) return;
    const fileId = parseInt(fileDiff.dataset.fileId);

    const contextSnippet = gatherDiffContext(row, 5);
    openAskPanel('line', fileId, lineNumber, contextSnippet, row);
  }

  function openAskPanel(scope, fileId, lineNumber, contextSnippet, anchorElement) {
    // Close any existing ask panel
    removeActiveAskRow();

    // Build the label
    let label;
    if (scope === 'line') label = `Ask about line ${lineNumber}`;
    else if (scope === 'file') {
      const file = fileDataMap.get(fileId);
      label = `Ask about ${file ? file.file_path : 'this file'}`;
    } else {
      label = 'Ask about this review';
    }

    // Review scope, and file scope when a rendered view hides every diff table, use a div
    // (a <tr> would land in a hidden table). Otherwise the panel is a <tr> in the diff table.
    const fileDiffEl = scope === 'file' ? document.getElementById(`file-${fileId}`) : null;
    const altView = scope === 'file' && isAltViewShowing(fileId)
      ? (document.getElementById(`markdown-${fileId}`) || document.getElementById(`pipeline-${fileId}`))
      : null;
    if (scope === 'review' || altView) {
      const panel = document.createElement('div');
      panel.className = 'review-ask-panel';
      panel.innerHTML = `<div class="ask-input-card"><div class="ask-input-label">${label}</div><div class="ask-input-hint">${ASK_HINT}</div><div class="ask-input-form"><input type="text" class="ask-input" placeholder="Ask a question..." autofocus /><button class="ask-btn">Ask</button><button class="ask-cancel-btn">Cancel</button></div></div>`;

      if (altView) {
        // Top of the visible rendered view, just under the file header
        altView.parentElement.insertBefore(panel, altView);
      } else {
        const diffContainer = document.getElementById('diff-container');
        const firstChild = diffContainer.querySelector('.file-diff') || diffContainer.querySelector('.general-annotations');
        if (firstChild) diffContainer.insertBefore(panel, firstChild);
        else diffContainer.appendChild(panel);
      }

      activeAskRow = panel;
      // Store metadata
      panel.dataset.scope = scope;
      panel.dataset.fileId = altView ? fileId : '';
      panel.dataset.lineNumber = '';
      panel.dataset.context = '';
    } else {
      // For line and file scope, use <tr> inside the diff table
      const askRowFor = colspan => rowFromHtml(`<tr class="ask-input-row"><td colspan="${colspan}"><div class="ask-input-card"><div class="ask-input-label">${label}</div><div class="ask-input-hint">${ASK_HINT}</div><div class="ask-input-form"><input type="text" class="ask-input" placeholder="Ask a question..." autofocus /><button class="ask-btn">Ask</button><button class="ask-cancel-btn">Cancel</button></div></div></td></tr>`);
      let askRow;

      if (scope === 'file') {
        // Insert at the top of the visible diff table for the file
        const fileDiff = fileDiffEl;
        const visible = [...fileDiff.querySelectorAll('.diff-table-container')]
          .find(c => c.style.display !== 'none' && c.querySelector('.diff-table tbody'));
        const tbody = (visible || fileDiff).querySelector('.diff-table tbody');
        askRow = askRowFor(colspanFor(tbody));
        tbody.insertBefore(askRow, tbody.firstChild);
      } else {
        // Line scope: insert after the anchor row
        askRow = askRowFor(colspanFor(anchorElement));
        anchorElement.parentElement.insertBefore(askRow, anchorElement.nextSibling);
      }

      activeAskRow = askRow;
      askRow.dataset.scope = scope;
      askRow.dataset.fileId = fileId || '';
      askRow.dataset.lineNumber = lineNumber || '';
      askRow.dataset.context = contextSnippet || '';
    }

    // Focus and wire handlers
    const input = activeAskRow.querySelector('.ask-input');
    input.focus();
    activeAskRow.querySelector('.ask-btn').addEventListener('click', () => submitAskQuestion(activeAskRow));
    activeAskRow.querySelector('.ask-cancel-btn').addEventListener('click', () => removeActiveAskRow());
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') submitAskQuestion(activeAskRow);
      if (ev.key === 'Escape') removeActiveAskRow();
    });
  }

  // Split rows hold two code cells; prefer the new side
  function rowCodeText(row) {
    const code = row.querySelector('.line-content.new code') || row.querySelector('.line-content code');
    return code ? code.textContent : null;
  }

  function gatherDiffContext(centerRow, radius) {
    const lines = [];
    let current = centerRow;
    // Collect lines before
    const before = [];
    let prev = current.previousElementSibling;
    for (let i = 0; i < radius && prev; i++) {
      if (prev.classList.contains('diff-line')) {
        const text = rowCodeText(prev);
        if (text != null) before.unshift(text);
      }
      prev = prev.previousElementSibling;
    }
    lines.push(...before);
    // Current line
    const currentText = rowCodeText(current);
    if (currentText != null) lines.push(currentText);
    // Collect lines after
    let next = current.nextElementSibling;
    for (let i = 0; i < radius && next; i++) {
      if (next.classList.contains('diff-line')) {
        const text = rowCodeText(next);
        if (text != null) lines.push(text);
      }
      next = next.nextElementSibling;
    }
    return lines.join('\n');
  }

  function removeActiveAskRow() {
    if (activeAskRow) {
      activeAskRow.remove();
      activeAskRow = null;
    }
  }

  async function submitAskQuestion(askElement) {
    const input = askElement.querySelector('.ask-input');
    const question = input.value.trim();
    if (!question) return;

    const scope = askElement.dataset.scope || 'line';
    const fileId = askElement.dataset.fileId ? parseInt(askElement.dataset.fileId) : null;
    const lineNumber = askElement.dataset.lineNumber ? parseInt(askElement.dataset.lineNumber) : null;
    const context = askElement.dataset.context || '';
    const reviewId = getReviewIdFromUrl();

    // Show loading
    const card = askElement.querySelector('.ask-input-card');
    card.innerHTML = `<div class="ask-loading"><span class="ask-spinner"></span> Asking ${askName}...</div>`;

    try {
      const resp = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review_id: parseInt(reviewId), file_id: fileId, line_number: lineNumber, question, context, scope }),
      });
      const data = await resp.json();

      // Render response
            const scopeLabel = scope === 'line' ? `Line ${lineNumber}` : scope === 'file' ? fileDataMap.get(fileId)?.file_path || 'File' : 'Review';
      const unavailable = data.code === 'claude_not_available' || data.code === 'agent_not_available';
      const modelBadge = data.model_used && !unavailable ? `<span class="badge badge-model">${data.model_used}</span>` : '';
      if (unavailable) { askAvailable = false; if (data.response) ASK_UNAVAILABLE_MSG = data.response; applyAskStatus(); }
      const typeClass = data.error ? 'annotation-warning' : 'annotation-assistant';
      const badge = unavailable ? `${askName} not available` : data.error ? 'error' : 'assistant';
      const renderedContent = data.error ? escapeHtml(data.response) : renderMarkdown(data.response);

      if (askElement.tagName !== 'TR') {
        // Replace the panel div content
        askElement.innerHTML = `<div class="annotation-card ${typeClass}"><div class="annotation-header"><span class="badge badge-annotation-type">${badge}</span>${modelBadge}<span style="font-size:11px;color:var(--text-muted);">${scopeLabel}</span><button class="ask-dismiss-btn" title="Dismiss">&times;</button></div><div class="ask-question-text">Q: ${escapeHtml(question)}</div><div class="annotation-content">${renderedContent}</div></div>`;
        askElement.querySelector('.ask-dismiss-btn').addEventListener('click', () => { askElement.remove(); activeAskRow = null; });
      } else {
        // Replace the <tr> with response row
        const responseHtml = `<tr class="annotation-card-row"><td colspan="${colspanFor(askElement)}"><div class="annotation-card ${typeClass}"><div class="annotation-header"><span class="badge badge-annotation-type">${badge}</span>${modelBadge}<span style="font-size:11px;color:var(--text-muted);">${scopeLabel}</span><button class="ask-dismiss-btn" title="Dismiss">&times;</button></div><div class="ask-question-text">Q: ${escapeHtml(question)}</div><div class="annotation-content">${renderedContent}</div></div></td></tr>`;
        const responseRow = rowFromHtml(responseHtml);
        askElement.parentElement.insertBefore(responseRow, askElement);
        askElement.remove();
        activeAskRow = null;
        responseRow.querySelector('.ask-dismiss-btn').addEventListener('click', () => responseRow.remove());
      }
    } catch (err) {
      card.innerHTML = `<div style="color: var(--red); font-size: 12px;">Error: ${escapeHtml(err.message)}</div>`;
    }
  }

  // ============================================================
  // Utilities
  // ============================================================

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function showError(message) {
    const container = document.getElementById('diff-container');
    container.innerHTML = `
      <div class="error-state">
        <h2>Error</h2>
        <p>${escapeHtml(message)}</p>
      </div>
    `;
  }

  // ============================================================
  // Entry Point
  // ============================================================

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
