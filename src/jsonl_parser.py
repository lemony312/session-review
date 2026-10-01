"""Parse Claude Code session JSONL files to extract structured conversation data.

This module parses session files to extract user prompts, assistant responses,
thinking blocks, and tool calls for building annotated code reviews.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolCall:
    """A tool invocation from the session."""

    tool_name: str  # Edit, Write, Read, Bash, Grep, Glob, Agent, etc.
    tool_id: str
    input: dict[str, Any]  # the full input params
    file_path: str | None  # extracted from input if applicable
    timestamp: str | None


@dataclass
class AssistantTurn:
    """One assistant response with its context."""

    uuid: str | None
    parent_uuid: str | None
    timestamp: str | None
    text_blocks: list[str] = field(default_factory=list)
    thinking_blocks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ConversationTurn:
    """A user prompt + assistant response pair."""

    user_text: str
    assistant: AssistantTurn


@dataclass
class ParsedSession:
    """Complete parsed session."""

    session_id: str
    project: str | None
    branch: str | None
    cwd: str | None
    first_timestamp: str | None
    last_timestamp: str | None
    turns: list[ConversationTurn] = field(default_factory=list)
    all_tool_calls: list[ToolCall] = field(default_factory=list)


def _extract_file_path(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Extract file path from tool input based on tool type."""
    if tool_name in ("Edit", "Write", "Read"):
        return tool_input.get("file_path")
    elif tool_name == "Bash":
        # Extract file path from git/file commands if possible
        command = tool_input.get("command", "")
        # Simple heuristics for common patterns
        if isinstance(command, str):
            # Look for file paths in common commands
            for prefix in ["git add ", "git diff ", "cat ", "ls ", "rm ", "mv ", "cp "]:
                if prefix in command:
                    # This is a heuristic; full parsing would be complex
                    parts = command.split()
                    for i, part in enumerate(parts):
                        if part == prefix.strip() and i + 1 < len(parts):
                            return parts[i + 1]
    return None


def _extract_text_from_content(content: str | list[dict[str, Any]]) -> str:
    """Extract text from message content (handles both str and block list)."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        # Concatenate text from text blocks
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return ""


def parse_session(file_path: Path) -> ParsedSession:
    """Parse a session JSONL file into structured data.

    Args:
        file_path: Path to the session JSONL file

    Returns:
        ParsedSession with extracted conversation data

    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If JSONL is malformed
    """
    session_id = ""
    project = None
    branch = None
    cwd = None
    first_timestamp = None
    last_timestamp = None
    turns: list[ConversationTurn] = []
    all_tool_calls: list[ToolCall] = []

    # Track message sequence to pair user + assistant
    last_user_message = ""
    pending_assistant: AssistantTurn | None = None

    # Skip message types that aren't relevant
    skip_types = {"progress", "system", "file-history-snapshot", "queue-operation"}

    with open(file_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                # Log warning but continue parsing
                print(f"Warning: Skipping malformed JSON at line {line_num}: {e}")
                continue

            msg_type = msg.get("type")
            if msg_type in skip_types:
                continue

            timestamp = msg.get("timestamp")
            if timestamp:
                if not first_timestamp:
                    first_timestamp = timestamp
                last_timestamp = timestamp

            if msg_type == "user":
                # Any user record ends the current run of assistant records.
                if pending_assistant and last_user_message:
                    turns.append(ConversationTurn(last_user_message, pending_assistant))
                pending_assistant = None

                # Extract session metadata from first user message
                if not session_id:
                    session_id = msg.get("sessionId", "")
                    branch = msg.get("gitBranch")
                    cwd = msg.get("cwd")
                    project = msg.get("project")

                # Only a record with real text is a prompt. Tool-result records
                # (text == "") must not replace or clear the prompt being answered.
                content = msg.get("message", {}).get("content", "")
                text = _extract_text_from_content(content)
                if text.strip():
                    last_user_message = text

            elif msg_type == "assistant":
                # Parse assistant message
                uuid = msg.get("uuid")
                parent_uuid = msg.get("parentUuid")
                content_blocks = msg.get("message", {}).get("content", [])

                assistant_turn = AssistantTurn(
                    uuid=uuid,
                    parent_uuid=parent_uuid,
                    timestamp=timestamp,
                )

                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue

                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            if text:
                                assistant_turn.text_blocks.append(text)

                        elif block_type == "thinking":
                            thinking = block.get("thinking", "")
                            if thinking:
                                assistant_turn.thinking_blocks.append(thinking)

                        elif block_type == "tool_use":
                            tool_name = block.get("name", "")
                            tool_id = block.get("id", "")
                            tool_input = block.get("input", {})

                            file_path = _extract_file_path(tool_name, tool_input)

                            tool_call = ToolCall(
                                tool_name=tool_name,
                                tool_id=tool_id,
                                input=tool_input,
                                file_path=file_path,
                                timestamp=timestamp,
                            )

                            assistant_turn.tool_calls.append(tool_call)
                            all_tool_calls.append(tool_call)

                # Consecutive assistant records (Claude Code writes each content block
                # as its own record) form one turn, so text and the tool calls it
                # introduces stay together. A user record (prompt or tool result) ends it.
                if pending_assistant:
                    pending_assistant.text_blocks.extend(assistant_turn.text_blocks)
                    pending_assistant.thinking_blocks.extend(assistant_turn.thinking_blocks)
                    pending_assistant.tool_calls.extend(assistant_turn.tool_calls)
                else:
                    pending_assistant = assistant_turn

    # Commit any final pending assistant turn
    if pending_assistant and last_user_message:
        turns.append(ConversationTurn(last_user_message, pending_assistant))

    return ParsedSession(
        session_id=session_id,
        project=project,
        branch=branch,
        cwd=cwd,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        turns=turns,
        all_tool_calls=all_tool_calls,
    )


def parse_session_with_subagents(file_path: Path) -> ParsedSession:
    """Parse a session JSONL and all its subagent sessions into a unified structure.

    Args:
        file_path: Path to the main session JSONL file

    Returns:
        ParsedSession with turns from main session + all subagents merged

    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If JSONL is malformed
    """
    # Parse the main session first
    main_session = parse_session(file_path)

    # Look for subagent directory
    # Pattern: <project_dir>/<session_uuid>/subagents/agent-*.jsonl
    session_uuid = file_path.stem
    subagent_dir = file_path.parent / session_uuid / "subagents"

    if not subagent_dir.exists():
        # No subagents, return main session as-is
        return main_session

    # Parse all subagent sessions
    subagent_files = sorted(subagent_dir.glob("agent-*.jsonl"))
    for subagent_file in subagent_files:
        try:
            subagent_session = parse_session(subagent_file)
            # Merge turns from subagent into main session
            main_session.turns.extend(subagent_session.turns)
            main_session.all_tool_calls.extend(subagent_session.all_tool_calls)
            # Update timestamps if subagent extends the time range
            if subagent_session.last_timestamp:
                if not main_session.last_timestamp or subagent_session.last_timestamp > main_session.last_timestamp:
                    main_session.last_timestamp = subagent_session.last_timestamp
        except Exception as e:
            # Log warning but continue with other subagents
            print(f"Warning: Failed to parse subagent {subagent_file}: {e}")

    return main_session


def parse_session_metadata(file_path: Path) -> dict | None:
    """Parse only the first user message from a JSONL for lightweight metadata.

    Much faster than parse_session() — reads only until the first user message
    (typically 5-20 lines) instead of the entire file.

    Returns:
        Dict with session_id, branch, cwd, project, first_prompt, or None if
        no user message found.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") != "user":
                    continue

                # Extract first prompt text
                content = msg.get("message", {}).get("content", "")
                first_prompt = _extract_text_from_content(content)

                return {
                    "session_id": msg.get("sessionId", file_path.stem),
                    "branch": msg.get("gitBranch"),
                    "cwd": msg.get("cwd"),
                    "project": msg.get("project"),
                    "first_prompt": first_prompt[:200] if first_prompt else "",
                }
    except Exception:
        pass

    return None


def get_file_edits(session: ParsedSession) -> dict[str, list[dict[str, Any]]]:
    """Extract file edits grouped by file path.

    Returns dict mapping file_path -> list of edit records with:
    {
        "tool_call": ToolCall,
        "thinking": list[str],  # thinking blocks from same turn
        "text": list[str],  # text blocks from same turn
        "user_context": str,  # preceding user message
    }
    """
    edits_by_file: dict[str, list[dict[str, Any]]] = {}

    for turn in session.turns:
        user_context = turn.user_text
        assistant = turn.assistant

        for tool_call in assistant.tool_calls:
            # Only include Edit and Write tool calls
            if tool_call.tool_name not in ("Edit", "Write"):
                continue

            if not tool_call.file_path:
                continue

            edit_record = {
                "tool_call": tool_call,
                "thinking": assistant.thinking_blocks.copy(),
                "text": assistant.text_blocks.copy(),
                "user_context": user_context,
            }

            if tool_call.file_path not in edits_by_file:
                edits_by_file[tool_call.file_path] = []

            edits_by_file[tool_call.file_path].append(edit_record)

    return edits_by_file
