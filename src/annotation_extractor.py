"""Extract annotations from session data and map them to file/line locations.

Annotations are thinking blocks and text explanations that correspond to
specific Edit/Write operations, explaining WHY changes were made.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from jsonl_parser import ParsedSession, ToolCall


MAX_ANNOTATION_LEN = 4000
MAX_ANNOTATIONS = 30
_ANNOTATION_PRIORITY = {"change-summary": 0, "plan": 1, "decision": 2, "warning": 3, "reasoning": 4, "context": 5}


@dataclass
class Annotation:
    """An annotation linked to a file change."""

    file_path: str | None  # None for general annotations
    line_start: int | None  # None for file-level annotations
    line_end: int | None
    annotation_type: str  # reasoning, decision, context, warning, change-summary
    content: str
    source_type: str  # thinking, assistant_text, auto_summary
    source_message_uuid: str | None
    sort_order: int = 0


def _classify_annotation(text: str) -> str:
    """Classify annotation type based on content patterns."""
    lower = text.lower()

    # Plan: describes approach or steps (require strong signals, not just "will"/"first"/"then")
    if any(w in lower for w in ["steps:", "approach:", "plan:", "strategy:"]):
        return "plan"

    # Decision: explains choice between alternatives
    if any(w in lower for w in ["chose", "instead of", "rather than", "trade-off", "decision", "opted"]):
        return "decision"

    # Warning: mentions risks or breaking changes
    if any(w in lower for w in ["warning", "careful", "risk", "breaking", "deprecated", "caution"]):
        return "warning"

    # Reasoning: explains why something is needed
    if any(w in lower for w in ["because", "reason", "need to", "should", "the issue", "so that"]):
        return "reasoning"

    # Context: provides background information
    return "context"


_NOISE_PREFIXES = (
    "here's what i found",
    "here is what i found",
    "i'll ",
    "i will ",
    "let me ",
    "sure,",
    "sure!",
    "ok,",
    "ok!",
    "okay,",
    "okay!",
    "got it",
    "understood",
    "absolutely",
    "certainly",
    "of course",
    "no problem",
    "great,",
    "great!",
    "perfect,",
    "perfect!",
    "done.",
    "done!",
    "here's the",
    "here is the",
    "i've launched",
    "i have launched",
    "i'll start by",
    "i will start by",
)

_NOISE_PATTERNS = (
    "i'll start by reading",
    "i'll start by fetching",
    "i'll start by understanding",
    "i'll start by getting",
    "let me start by",
    "i'll help you",
    "i need write permission",
)


def _is_noise(text: str) -> bool:
    """Return True if text is assistant chatter / status update, not real insight."""
    lower = text.strip().lower()
    # Check prefix-based noise
    if any(lower.startswith(p) for p in _NOISE_PREFIXES):
        return True
    # Check pattern-based noise
    if any(p in lower for p in _NOISE_PATTERNS):
        return True
    return False


def _extract_file_mentions(text: str) -> set[str]:
    """Extract file paths mentioned in text.

    Looks for patterns like:
    - *.scala, *.mill
    - path/to/file.ext
    - CamelCaseFilename.scala
    - `build.mill`  (backtick-quoted)
    - simple filenames with extensions
    """
    files = set()

    # Pattern 1: explicit file extensions (glob patterns)
    for match in re.finditer(r'\*\.\w+', text):
        files.add(match.group(0))

    # Pattern 2: path-like strings (word/word/file.ext)
    for match in re.finditer(r'\b[\w/-]+/[\w/-]+\.\w+', text):
        files.add(match.group(0))

    # Pattern 3: CamelCase filenames with extensions
    for match in re.finditer(r'\b[A-Z][a-zA-Z0-9]+\.\w+', text):
        files.add(match.group(0))

    # Pattern 4: backtick-quoted filenames with extensions
    for match in re.finditer(r'`([\w./-]+\.\w+)`', text):
        files.add(match.group(1))

    # Pattern 5: simple filenames with common code extensions
    for match in re.finditer(
        r'\b([\w-]+\.(?:scala|java|py|js|ts|tsx|jsx|mill|sbt|sc|yml|yaml|json|toml|xml|conf|properties|md|txt|sh|bash|zsh|go|rs|rb|c|cpp|h|hpp|css|html|sql))\b',
        text
    ):
        files.add(match.group(1))

    return files


def _match_file_to_diff(filename: str, diff_keys: set[str]) -> str | None:
    """Match a mentioned filename to a diff file path.

    Matches on basename (e.g. "build.mill" matches "project/build.mill")
    or on suffix match (e.g. "src/Main.scala" matches full path).
    """
    basename = os.path.basename(filename)
    for diff_file in sorted(diff_keys):
        diff_basename = os.path.basename(diff_file)
        # Exact basename match
        if basename == diff_basename:
            return diff_file
        # Suffix match (filename is a tail of the diff path)
        if diff_file.endswith(filename):
            return diff_file
        # Or the diff path ends with same basename
        if filename in diff_file:
            return diff_file
    return None


def _find_line_in_diff(diff_text: str, search_text: str) -> tuple[int | None, int | None]:
    """Find the line range in a diff that corresponds to a change.

    Looks for the search_text (from Edit's old_string or new_string) in the
    diff and returns the line numbers in the new file.
    """
    if not search_text or not diff_text:
        return None, None

    # Take first line of search text for matching
    first_line = search_text.strip().split("\n")[0].strip()
    if len(first_line) < 5:
        return None, None

    current_line = 0
    for diff_line in diff_text.splitlines():
        # Parse hunk header for line numbers
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", diff_line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue

        if diff_line.startswith("+++") or diff_line.startswith("---"):
            continue

        if diff_line.startswith("-"):
            continue  # deleted lines don't count for new file line numbers

        if diff_line.startswith("+"):
            line_content = diff_line[1:].strip()
            if first_line in line_content:
                num_lines = len(search_text.strip().split("\n"))
                return current_line, current_line + num_lines - 1
            current_line += 1
        else:
            # context line
            if first_line in diff_line.strip():
                num_lines = len(search_text.strip().split("\n"))
                return current_line, current_line + num_lines - 1
            current_line += 1

    return None, None


def _extract_identifiers(text: str) -> set[str]:
    """Extract code identifiers from text (class names, function names, variables).

    Returns identifiers that are at least 4 chars to avoid noise.
    """
    identifiers = set()
    # CamelCase identifiers (class names)
    for m in re.finditer(r'\b([A-Z][a-zA-Z0-9]{3,})\b', text):
        identifiers.add(m.group(1))
    # snake_case / camelCase identifiers (function/variable names)
    for m in re.finditer(r'\b([a-z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+)\b', text):
        identifiers.add(m.group(1))
    # camelCase
    for m in re.finditer(r'\b([a-z]+[A-Z][a-zA-Z0-9]{2,})\b', text):
        identifiers.add(m.group(1))
    # Remove common English words that look like identifiers
    noise = {
        "This", "That", "Then", "There", "These", "Those", "When", "Where",
        "What", "Which", "With", "From", "Into", "About", "After", "Before",
        "Should", "Would", "Could", "However", "Because", "Also", "Some",
        "Here", "Each", "Instead", "Rather", "Need", "Make", "Have",
    }
    return identifiers - noise


def _find_identifiers_in_diff(diff_text: str, identifiers: set[str]) -> tuple[int | None, int | None]:
    """Find line range in diff where identifiers from thinking text appear in added lines.

    Returns the line range of the first match found.
    """
    if not identifiers or not diff_text:
        return None, None

    current_line = 0
    first_match_line = None
    last_match_line = None

    for diff_line in diff_text.splitlines():
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", diff_line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue

        if diff_line.startswith("+++") or diff_line.startswith("---"):
            continue

        if diff_line.startswith("-"):
            continue

        if diff_line.startswith("+"):
            line_content = diff_line[1:]
            for ident in identifiers:
                if ident in line_content:
                    if first_match_line is None:
                        first_match_line = current_line
                    last_match_line = current_line
                    break
            current_line += 1
        else:
            current_line += 1

    if first_match_line is not None:
        return first_match_line, last_match_line
    return None, None


def _truncate_to_relevant_paragraph(text: str, file_hint: str | None = None) -> str:
    """Truncate text to the most relevant paragraph, max MAX_ANNOTATION_LEN chars.

    If file_hint is provided, prefer the paragraph that mentions it.
    Otherwise take the first substantial paragraph.
    """
    if len(text) <= MAX_ANNOTATION_LEN:
        return text

    # Split into paragraphs
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    if not paragraphs:
        return text[:MAX_ANNOTATION_LEN].rstrip() + "..."

    # If we have a file hint, prefer the paragraph mentioning it
    if file_hint:
        basename = os.path.basename(file_hint)
        for para in paragraphs:
            if basename in para or file_hint in para:
                if len(para) <= MAX_ANNOTATION_LEN:
                    return para
                return para[:MAX_ANNOTATION_LEN].rstrip() + "..."

    # Take the first paragraph that's substantial (>30 chars)
    for para in paragraphs:
        if len(para) >= 30:
            if len(para) <= MAX_ANNOTATION_LEN:
                return para
            return para[:MAX_ANNOTATION_LEN].rstrip() + "..."

    # Fallback: just truncate
    return text[:MAX_ANNOTATION_LEN].rstrip() + "..."


def _parse_diff_stats(diff_text: str) -> tuple[int, int, list[str], list[str]]:
    """Parse a diff to get additions, deletions, and sample added/removed lines.

    Returns (num_added, num_removed, sample_added_lines, sample_removed_lines).
    """
    added = 0
    removed = 0
    added_lines: list[str] = []
    removed_lines: list[str] = []

    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
            content = line[1:].strip()
            if content and len(content) > 3:
                added_lines.append(content)
        elif line.startswith("-"):
            removed += 1
            content = line[1:].strip()
            if content and len(content) > 3:
                removed_lines.append(content)

    return added, removed, added_lines, removed_lines


def _summarize_diff_patterns(added_lines: list[str], removed_lines: list[str]) -> str:
    """Summarize patterns in added/removed lines into a short description."""
    patterns = []

    # Check for import changes
    new_imports = [l for l in added_lines if l.startswith(("import ", "from ")) or "import " in l[:30]]
    removed_imports = [l for l in removed_lines if l.startswith(("import ", "from ")) or "import " in l[:30]]
    if new_imports and not removed_imports:
        patterns.append(f"{len(new_imports)} new import(s)")
    elif new_imports and removed_imports:
        patterns.append("modified imports")

    # Check for class/def/function changes
    new_defs = [l for l in added_lines if re.match(r'\s*(def |class |function |val |var |object |trait |case class |fn )', l)]
    if new_defs:
        # Extract names
        names = []
        for d in new_defs[:3]:
            m = re.search(r'(def|class|function|val|var|object|trait|fn)\s+(\w+)', d)
            if m:
                names.append(m.group(2))
        if names:
            patterns.append(f"new/modified: {', '.join(names)}")

    # Check for config / dependency changes
    dep_indicators = [l for l in added_lines if any(kw in l.lower() for kw in ["version", "dependency", "libraryDependencies", "ivy\""])]
    if dep_indicators:
        patterns.append("dependency changes")

    # Check for test changes
    test_indicators = [l for l in added_lines if any(kw in l for kw in ["test(", "assert", "expect(", "should", "spec", "@Test"])]
    if test_indicators:
        patterns.append("test changes")

    return "; ".join(patterns) if patterns else ""


def _generate_change_summary(file_path: str, diff_text: str) -> Annotation:
    """Generate a brief change-summary annotation for a file's diff."""
    added, removed, added_lines, removed_lines = _parse_diff_stats(diff_text)

    summary_parts = [f"+{added} -{removed} lines."]

    pattern_summary = _summarize_diff_patterns(added_lines, removed_lines)
    if pattern_summary:
        summary_parts.append(pattern_summary.capitalize() + ".")

    content = " ".join(summary_parts)
    if len(content) > MAX_ANNOTATION_LEN:
        content = content[:MAX_ANNOTATION_LEN].rstrip() + "..."

    return Annotation(
        file_path=file_path,
        line_start=None,
        line_end=None,
        annotation_type="change-summary",
        content=content,
        source_type="auto_summary",
        source_message_uuid=None,
        sort_order=0,
    )


def _resolve_text_to_files(
    text: str,
    diff_keys: set[str],
    read_files: set[str],
) -> list[tuple[str, int | None, int | None]]:
    """Resolve a text block to file(s) it likely discusses.

    Returns list of diff file paths that the text references.
    Uses file mentions in text, Read tool context, and basename matching.
    """
    results = []
    mentioned = _extract_file_mentions(text)

    matched_files: set[str] = set()

    # Match mentioned filenames to diff files
    for mf in sorted(mentioned):
        target = _match_file_to_diff(mf, diff_keys)
        if target and target not in matched_files:
            matched_files.add(target)

    # Match Read file paths to diff files
    for rf in sorted(read_files):
        # Read paths are often absolute; match on basename
        target = _match_file_to_diff(rf, diff_keys)
        if target and target not in matched_files:
            # Only add if text actually discusses this file
            basename = os.path.basename(rf)
            if basename in text or rf in text:
                matched_files.add(target)

    # If text mentions no specific files but only one file was read, use it
    if not matched_files and len(read_files) == 1:
        rf = list(read_files)[0]
        target = _match_file_to_diff(rf, diff_keys)
        if target:
            matched_files.add(target)

    return sorted(matched_files)


def extract_annotations(
    session: ParsedSession,
    file_diffs: dict[str, str] | None = None,
) -> list[Annotation]:
    """Extract annotations from a parsed session.

    Creates annotations from:
    1. Edit/Write tool calls - linked to specific files/lines
    2. Read/Grep followed by thinking/text - linked via file context
    3. Thinking blocks that mention files - linked by filename matching
    4. General reasoning - review-level annotations (file_path=None)
    5. First user prompt and last assistant response - plan/summary
    6. Auto-generated change summaries for each file in the diff

    Args:
        session: Parsed session data
        file_diffs: Optional dict of file_path -> diff_text for line mapping

    Returns:
        List of annotations with file/line mappings where possible
    """
    annotations: list[Annotation] = []
    sort_order = 0
    file_diffs = file_diffs or {}

    # Build a set of diff file keys for matching (deduplicate: only use the
    # shortest key per basename so we prefer relative paths)
    diff_keys: set[str] = set(file_diffs.keys())

    # (D) Auto-generate change-summary annotations for each unique file in the diff.
    # Use only relative-path keys (shortest path per basename) to avoid duplicates.
    _seen_basenames: set[str] = set()
    for fp, diff_text in file_diffs.items():
        bn = os.path.basename(fp)
        if bn in _seen_basenames:
            continue
        _seen_basenames.add(bn)
        if diff_text:
            ann = _generate_change_summary(fp, diff_text)
            ann.sort_order = sort_order
            annotations.append(ann)
            sort_order += 1

    # Add plan annotation from first user prompt
    if session.turns and session.turns[0].user_text:
        first_prompt = session.turns[0].user_text
        if len(first_prompt) >= 30:
            annotations.append(Annotation(
                file_path=None,
                line_start=None,
                line_end=None,
                annotation_type="plan",
                content=_truncate_to_relevant_paragraph(first_prompt),
                source_type="user_prompt",
                source_message_uuid=None,
                sort_order=sort_order,
            ))
            sort_order += 1

    for turn in session.turns:
        assistant = turn.assistant

        # Collect file paths touched by Edit/Write in this turn
        edited_files = set()
        edit_calls = [tc for tc in assistant.tool_calls if tc.tool_name in ("Edit", "Write")]
        for tc in edit_calls:
            if tc.file_path:
                edited_files.add(tc.file_path)

        # Collect file paths touched by Read/Grep in this turn
        read_files = set()
        read_calls = [tc for tc in assistant.tool_calls if tc.tool_name in ("Read", "Grep", "Glob")]
        for tc in read_calls:
            if tc.file_path:
                read_files.add(tc.file_path)

        # --- Process thinking blocks ---
        for thinking in assistant.thinking_blocks:
            if len(thinking) < 50:
                continue

            # (A, B, C) Try to resolve thinking to specific files via mentions,
            # Read context, and identifier matching
            resolved_files = _resolve_text_to_files(thinking, diff_keys, read_files)

            # Also consider Edit files from the same turn
            for ef in sorted(edited_files):
                if ef not in resolved_files and ef in diff_keys:
                    resolved_files.append(ef)

            if resolved_files:
                for target_file in resolved_files:
                    diff_text = file_diffs.get(target_file, "")

                    # (C) Try to map to specific lines via identifiers
                    identifiers = _extract_identifiers(thinking)
                    line_start, line_end = _find_identifiers_in_diff(diff_text, identifiers)

                    content = _truncate_to_relevant_paragraph(thinking, target_file)
                    annotations.append(Annotation(
                        file_path=target_file,
                        line_start=line_start,
                        line_end=line_end,
                        annotation_type=_classify_annotation(content),
                        content=content,
                        source_type="thinking",
                        source_message_uuid=assistant.uuid,
                        sort_order=sort_order,
                    ))
                    sort_order += 1
            else:
                # General review-level annotation
                content = _truncate_to_relevant_paragraph(thinking)
                annotations.append(Annotation(
                    file_path=None,
                    line_start=None,
                    line_end=None,
                    annotation_type=_classify_annotation(content),
                    content=content,
                    source_type="thinking",
                    source_message_uuid=assistant.uuid,
                    sort_order=sort_order,
                ))
                sort_order += 1

        # --- Process text blocks ---
        for text in assistant.text_blocks:
            if len(text) < 30:
                continue
            if _is_noise(text):
                continue

            resolved_files = _resolve_text_to_files(text, diff_keys, read_files)

            # Also consider Edit files from the same turn
            for ef in sorted(edited_files):
                if ef not in resolved_files and ef in diff_keys:
                    resolved_files.append(ef)

            if resolved_files:
                for target_file in resolved_files:
                    diff_text = file_diffs.get(target_file, "")

                    # Try line mapping via identifiers
                    identifiers = _extract_identifiers(text)
                    line_start, line_end = _find_identifiers_in_diff(diff_text, identifiers)

                    content = _truncate_to_relevant_paragraph(text, target_file)
                    annotations.append(Annotation(
                        file_path=target_file,
                        line_start=line_start,
                        line_end=line_end,
                        annotation_type=_classify_annotation(content),
                        content=content,
                        source_type="assistant_text",
                        source_message_uuid=assistant.uuid,
                        sort_order=sort_order,
                    ))
                    sort_order += 1
            else:
                content = _truncate_to_relevant_paragraph(text)
                annotations.append(Annotation(
                    file_path=None,
                    line_start=None,
                    line_end=None,
                    annotation_type=_classify_annotation(content),
                    content=content,
                    source_type="assistant_text",
                    source_message_uuid=assistant.uuid,
                    sort_order=sort_order,
                ))
                sort_order += 1

    # Add summary annotation from last assistant response
    if session.turns and session.turns[-1].assistant.text_blocks:
        last_text = session.turns[-1].assistant.text_blocks[-1]
        if len(last_text) >= 30:
            annotations.append(Annotation(
                file_path=None,
                line_start=None,
                line_end=None,
                annotation_type="context",
                content=_truncate_to_relevant_paragraph(last_text),
                source_type="assistant_text",
                source_message_uuid=session.turns[-1].assistant.uuid,
                sort_order=sort_order,
            ))
            sort_order += 1

    # --- Post-processing ---

    # Deduplicate: remove annotations with identical content (first 200 chars)
    seen_content: set[str] = set()
    deduped: list[Annotation] = []
    for ann in annotations:
        key = ann.content[:200]
        if key not in seen_content:
            seen_content.add(key)
            deduped.append(ann)

    # Filter out noise: assistant chatter, acknowledgments, status updates
    deduped = [a for a in deduped if a.source_type == "auto_summary" or not _is_noise(a.content)]

    # "context" type annotations must be substantial (100+ chars) to be kept
    deduped = [a for a in deduped
               if a.annotation_type != "context" or len(a.content) >= 100
               or a.source_type == "auto_summary"]

    # Limit total annotations to prevent noise
    # Prioritize: change-summary > file-linked > plan/decision/warning > reasoning > context
    deduped.sort(key=lambda a: (
        0 if a.file_path else 1,  # file-linked first
        _ANNOTATION_PRIORITY.get(a.annotation_type, 6),  # by type priority
        a.sort_order,  # original order
    ))

    if len(deduped) > MAX_ANNOTATIONS:
        deduped = deduped[:MAX_ANNOTATIONS]

    # Re-assign sort_order
    for i, ann in enumerate(deduped):
        ann.sort_order = i

    return deduped


def merge_annotations(
    annotation_lists: list[list[Annotation]],
    max_total: int = MAX_ANNOTATIONS,
) -> list[Annotation]:
    """Merge annotations from multiple sessions with dedup and budget.

    Lists should be ordered most-recent-session-first so that the freshest
    annotations get priority when deduplicating.
    """
    seen_content: set[str] = set()
    merged: list[Annotation] = []

    for annotations in annotation_lists:
        for ann in annotations:
            key = ann.content[:200]
            if key in seen_content:
                continue
            seen_content.add(key)
            merged.append(ann)

    # Apply same priority sort as extract_annotations
    merged.sort(key=lambda a: (
        0 if a.file_path else 1,
        _ANNOTATION_PRIORITY.get(a.annotation_type, 6),
        a.sort_order,
    ))

    if len(merged) > max_total:
        merged = merged[:max_total]

    for i, ann in enumerate(merged):
        ann.sort_order = i

    return merged
