"""Semantic diff for Spinnaker pipeline (.pp) files.

A ``.pp`` is a JSON document describing a pipeline: a DAG of ``stages`` connected
by ``requisiteStageRefIds`` edges, plus ``parameterConfig``, ``triggers``,
``notifications`` and ``expectedArtifacts``. A raw git line-diff of one is
unreadable because:

  * editing a pipeline in the Spinnaker UI / gate API round-trips and rewrites the
    whole file, so key order and refId numbering churn without any semantic change;
  * the real logic lives inside embedded shell strings (a container ``command[2]``),
    so a multi-line script edit collapses to one changed JSON line;
  * stage relationships live in ``requisiteStageRefIds`` arrays, so moving a stage
    shows up as huge delete+re-add blocks with no "same stage moved" signal.

This module parses both blobs, normalizes away the noise, matches stages by a
stable *identity* (not refId), and emits a structured diff object that a frontend
renders as a graph + stage cards. It NEVER makes rendering decisions.

If either side fails to parse as a pipeline (not JSON, or no ``stages`` array) the
top-level entry point returns ``None`` and the caller falls back to the raw diff —
so a non-Spinnaker ``.pp`` (e.g. Puppet) is never mis-rendered.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from typing import Any

SCHEMA_VERSION = "1.0"

def _allowed_image_prefixes() -> tuple[str, ...]:
    """Approved image registry prefixes, from ``PIPELINE_IMAGE_REGISTRY_PREFIXES``.

    Comma-separated (e.g. ``registry.example.com/team/,mirror.example.com/``). When
    unset or empty, no allowlist is enforced and the nonmirror-image lint is off.
    Read per call so the setting can change without reimporting the module.
    """
    raw = os.environ.get("PIPELINE_IMAGE_REGISTRY_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_NUMERIC_REFID = re.compile(r"^\d+$")
_SEMANTIC_REFID = re.compile(r"^[a-z][a-z0-9-]+$")

# Severity ordering for computing summary.topSeverity. Includes the lint-only
# levels so a flag severity never silently ranks as "minor".
_SEVERITY_RANK = {"minor": 0, "warning": 1, "major": 2, "serious": 3, "critical": 4}

# Interpreters whose last argument is a script body rather than a program name.
_SHELL_EXES = ("sh", "bash", "ash", "dash", "zsh", "ksh")

# Placeholder that stands in for an embedded script during field-level diffing.
# The script itself is diffed as text by ``_shell_diffs``; leaving it in the JSON
# would re-collapse a readable script edit into one unreadable field row.
_SCRIPT_MASK = "⟪script — see shell diff⟫"


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------

def _parse_pipeline(content: str | None) -> dict | None:
    """Parse a .pp blob into a dict, or None if it is not a pipeline.

    A valid pipeline is a JSON object containing a ``stages`` list. Puppet .pp,
    partial/merge-conflicted files, and arbitrary JSON all return None.
    """
    if not content or not content.strip():
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stages"), list):
        return None
    return data


def _canonical(value: Any) -> Any:
    """Recursively sort dict keys so key-order churn disappears from comparisons."""
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Stage identity & matching
# ---------------------------------------------------------------------------

def _stage_manifests(stage: dict) -> list[tuple[str, dict]]:
    """Every inline manifest on a stage as (label, manifest).

    runJobManifest carries a single ``manifest``; deployManifest carries a
    ``manifests`` list (a ServiceAccount plus one CronJob per leg, typically), and
    the label is what tells those legs apart in the UI.
    """
    out: list[dict] = []
    single = stage.get("manifest")
    if isinstance(single, dict):
        out.append(single)
    for m in (stage.get("manifests") or []):
        if isinstance(m, dict):
            out.append(m)

    labelled: list[tuple[str, dict]] = []
    for man in out:
        kind = str(man.get("kind") or "")
        name = str((man.get("metadata") or {}).get("name") or "")
        labelled.append(("/".join(p for p in (kind, name) if p) or "manifest", man))
    return labelled


def _pod_specs(node: Any) -> list[dict]:
    """Every dict under ``node`` that looks like a PodSpec (has containers).

    Walks the manifest rather than hard-coding a path, so Deployment
    (``spec.template.spec``), Job (same), CronJob
    (``spec.jobTemplate.spec.template.spec``) and a bare Pod (``spec``) are all
    covered — including shapes Kubernetes adds later.
    """
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("containers"), list) or isinstance(node.get("initContainers"), list):
            found.append(node)
        for v in node.values():
            found.extend(_pod_specs(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_pod_specs(v))
    return found


def _container_entries(stage: dict) -> list[dict]:
    """Every container on a stage with the context needed to label it.

    Returns ``{manifest, name, kind, container}`` records. ``manifest`` is the
    manifest label, so the four identically-named containers of a four-leg
    CronJob deploy stay distinguishable.
    """
    entries: list[dict] = []
    seen: set[int] = set()
    for label, manifest in _stage_manifests(stage):
        for spec in _pod_specs(manifest):
            for key in ("initContainers", "containers"):
                for c in (spec.get(key) or []):
                    if not isinstance(c, dict) or id(c) in seen:
                        continue
                    seen.add(id(c))
                    entries.append({
                        "manifest": label,
                        "kind": str(manifest.get("kind") or ""),
                        "name": str(c.get("name") or "container"),
                        "container": c,
                    })
    return entries


def _stage_containers(stage: dict) -> list[dict]:
    """All containers + initContainers of a runJob/deployManifest stage."""
    return [e["container"] for e in _container_entries(stage)]


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _script_slot(container: dict) -> tuple[str, int] | None:
    """Locate the embedded script inside a container as (key, index).

    Spinnaker's own editor writes ``command`` = ["/bin/sh", "-c", "<script>"], but
    a hand-written or Helm-derived manifest just as often splits it as
    ``command`` = ["/bin/bash"] + ``args`` = ["-c", "<script>"]. Both are the same
    thing to a reviewer, so both must resolve to a script slot — otherwise the
    script silently degrades into an unreadable JSON field row.
    """
    command = container.get("command")
    args = container.get("args")

    if isinstance(command, list) and len(command) >= 3 and isinstance(command[2], str):
        return ("command", 2)

    exe = command[0] if isinstance(command, list) and command and isinstance(command[0], str) else ""
    exe_is_shell = _basename(exe) in _SHELL_EXES

    if isinstance(args, list) and args:
        flagged = isinstance(args[0], str) and args[0].startswith("-")
        if exe_is_shell or flagged:
            # The script is the last string argument; anything before it is a flag.
            for i in range(len(args) - 1, -1, -1):
                if isinstance(args[i], str) and not args[i].startswith("-"):
                    return ("args", i)

    if exe_is_shell and isinstance(command, list):
        for i in range(len(command) - 1, 0, -1):
            if isinstance(command[i], str) and not command[i].startswith("-"):
                return ("command", i)
    return None


def _extract_shell(container: dict) -> str | None:
    """Return the embedded shell body of a container, if any.

    JSON already decoded the \\n escapes, so this is real text.
    """
    slot = _script_slot(container)
    if not slot:
        return None
    return container[slot[0]][slot[1]]


def _normalize_shell(script: str) -> str:
    """Collapse whitespace for fingerprinting (not for display)."""
    return re.sub(r"\s+", " ", script).strip()


def _image_repo(image: str) -> str:
    """Strip the tag/digest so image identity is registry+repo only."""
    # drop digest
    image = image.split("@", 1)[0]
    # drop tag (last colon after the final slash)
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        image = image[:colon]
    return image


def _stage_fingerprint(stage: dict) -> str:
    """Content fingerprint for rescuing rename-and-edit matches."""
    parts = [stage.get("type", "")]
    moniker = stage.get("moniker") or {}
    parts.append(str(moniker.get("app", "")))
    shells = []
    images = []
    for c in _stage_containers(stage):
        if isinstance(c.get("image"), str):
            images.append(_image_repo(c["image"]))
        sh = _extract_shell(c)
        if sh:
            shells.append(_normalize_shell(sh))
    parts.append("|".join(sorted(images)))
    parts.append("|".join(sorted(shells)))
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()


def _stage_identity(stage: dict) -> tuple[str, str]:
    """Return (strategy, identity_key) for a stage.

    Cascade: semantic refId slug > (type, normalized name). The content fingerprint
    is applied later, as a rescue pass, so it lives in the matcher not here.
    """
    ref = str(stage.get("refId", ""))
    if ref and _SEMANTIC_REFID.match(ref) and not _NUMERIC_REFID.match(ref):
        return ("semantic-refid", ref)
    name = (stage.get("name") or "").strip()
    return ("type-name", f"{stage.get('type', '')}::{name}")


def _match_stages(old_stages: list[dict], new_stages: list[dict]) -> list[dict]:
    """Match stages across versions. Returns match records:

        {old, new, strategy, confidence}  (either side may be None)
    """
    old_by_key: dict[tuple[str, str], list[int]] = {}
    for i, s in enumerate(old_stages):
        old_by_key.setdefault(_stage_identity(s), []).append(i)

    matched_old: set[int] = set()
    matched_new: set[int] = set()
    records: list[dict] = []

    # Pass 1: identity match (semantic-refid or type+name).
    for j, s in enumerate(new_stages):
        key = _stage_identity(s)
        candidates = [i for i in old_by_key.get(key, []) if i not in matched_old]
        if candidates:
            i = candidates[0]
            matched_old.add(i)
            matched_new.add(j)
            records.append({
                "old": old_stages[i], "new": s,
                "strategy": key[0], "confidence": 1.0,
            })

    # Pass 2: fingerprint rescue for renamed+lightly-edited stages.
    old_fp: dict[str, list[int]] = {}
    for i, s in enumerate(old_stages):
        if i not in matched_old:
            old_fp.setdefault(_stage_fingerprint(s), []).append(i)
    for j, s in enumerate(new_stages):
        if j in matched_new:
            continue
        fp = _stage_fingerprint(s)
        candidates = [i for i in old_fp.get(fp, []) if i not in matched_old]
        if candidates:
            i = candidates[0]
            matched_old.add(i)
            matched_new.add(j)
            records.append({
                "old": old_stages[i], "new": s,
                "strategy": "fingerprint", "confidence": 0.8,
            })

    # Leftovers: removed / added.
    for i, s in enumerate(old_stages):
        if i not in matched_old:
            records.append({"old": s, "new": None, "strategy": None, "confidence": 1.0})
    for j, s in enumerate(new_stages):
        if j not in matched_new:
            records.append({"old": None, "new": s, "strategy": None, "confidence": 1.0})

    return records


# ---------------------------------------------------------------------------
# Field-level diff
# ---------------------------------------------------------------------------

# Which stage sub-keys map to which change category / severity.
def _classify_field(path: str) -> tuple[str, str]:
    """Return (category, severity) for a changed stage field path.

    Bracketed list keys are blanked out first: a manifest named
    ``sdk-cache-enqueue-${parameters.env}-live`` would otherwise make every field
    under it look like an env-var change.
    """
    p = re.sub(r"\[[^\]]*\]", "[]", path)
    if p == "requisiteStageRefIds":
        return ("dag-edge-change", "critical")
    if "command" in p:
        return ("shell-change", "critical")
    if p.endswith(".image") or p == "image":
        return ("image-change", "critical")
    if "stageEnabled" in p or p.endswith(".expression"):
        return ("spel-change", "critical")
    if ".env[" in p or p.endswith(".env") or p == "env":
        return ("env-change", "major")
    if "resources" in p or "helmChartVersion" in p or "replicas" in p or "instanceCount" in p:
        return ("resource-change", "major")
    if "serviceAccount" in p or "moniker" in p or "account" in p or "credentials" in p:
        return ("identity-change", "major")
    if "backoffLimit" in p or "restartPolicy" in p or "ttlSecondsAfterFinished" in p:
        return ("retry-change", "major")
    if "consumeArtifactSource" in p or "propertyFile" in p:
        return ("propertyfile-change", "major")
    if "skipExpressionEvaluation" in p:
        return ("skip-eval-change", "major")
    if "suspend" in p or "schedule" in p or "concurrencyPolicy" in p:
        return ("schedule-change", "critical")
    if "activeDeadlineSeconds" in p or "startingDeadlineSeconds" in p:
        return ("timeout-change", "major")
    if p.startswith("variables[") or p == "variables":
        return ("variable-change", "major")
    if "annotations" in p:
        return ("annotation-change", "minor")
    if "labels" in p:
        return ("label-change", "minor")
    if p in ("name", "comments"):
        return ("rename", "minor")
    return ("field-change", "minor")


# Keys that are pure noise (never meaningfully changed on their own).
_IGNORE_STAGE_KEYS = {"refId", "requisiteStageRefIds"}


def _item_key(item: dict, index: int) -> str:
    """A stable, human-readable key for a dict inside a list.

    Kubernetes and Spinnaker both address list members by name (containers, env
    vars, volumes, manifests), and the serialized order of those lists churns, so
    keying by name is both more readable and less noisy than keying by index.
    """
    for k in ("name", "key", "displayName", "id"):
        v = item.get(k)
        if isinstance(v, (str, int)) and str(v):
            kind = item.get("kind")
            if k == "name" and isinstance(kind, str) and kind:
                return f"{kind}/{v}"
            return str(v)
    meta_name = (item.get("metadata") or {}).get("name") if isinstance(item.get("metadata"), dict) else None
    if isinstance(meta_name, str) and meta_name:
        kind = item.get("kind")
        return f"{kind}/{meta_name}" if isinstance(kind, str) and kind else meta_name
    return str(index)


def _keyed(value: list) -> dict[str, Any] | None:
    """Index a list of dicts by ``_item_key``, or None if that is not possible."""
    if not value or not all(isinstance(v, dict) for v in value):
        return None
    keys = [_item_key(v, i) for i, v in enumerate(value)]
    if len(set(keys)) != len(keys):
        return None
    return dict(zip(keys, value))


def _diff_tree(old: Any, new: Any, path: str = "") -> list[dict]:
    """Structural diff of two JSON subtrees into rows the UI can list.

    Rows are ``{path, changeType, old, new}``. Recursion stops at the shallowest
    point where one side is absent, so an added env var is one ``env[MAX_TIME]``
    row rather than a ``.name`` row plus a ``.value`` row. Lists of dicts are
    matched by name (Kubernetes addresses them that way and their serialized order
    churns); everything else compares as a whole value.
    """
    if _canonical_json(old) == _canonical_json(new):
        return []

    if isinstance(old, dict) and isinstance(new, dict):
        rows: list[dict] = []
        for k in sorted(set(old) | set(new)):
            sub = f"{path}.{k}" if path else k
            if k not in new:
                rows.append({"path": sub, "changeType": "removed", "old": old[k], "new": None})
            elif k not in old:
                rows.append({"path": sub, "changeType": "added", "old": None, "new": new[k]})
            else:
                rows.extend(_diff_tree(old[k], new[k], sub))
        return rows

    if isinstance(old, list) and isinstance(new, list):
        old_map, new_map = _keyed(old), _keyed(new)
        if old_map is not None and new_map is not None:
            rows = []
            for k in sorted(set(old_map) | set(new_map)):
                sub = f"{path}[{k}]"
                if k not in new_map:
                    rows.append({"path": sub, "changeType": "removed", "old": old_map[k], "new": None})
                elif k not in old_map:
                    rows.append({"path": sub, "changeType": "added", "old": None, "new": new_map[k]})
                else:
                    rows.extend(_diff_tree(old_map[k], new_map[k], sub))
            return rows

    return [{"path": path, "changeType": "changed", "old": old, "new": new}]


def _mask_scripts(stage: dict) -> dict:
    """Deep copy of a stage with every embedded script replaced by a sentinel.

    Field diffing then reports the *shape* of a container (image, env, args flags)
    while the script body is left to ``_shell_diffs``, which renders it as bash.
    Without this, a one-line script edit reappears as a multi-kilobyte JSON row.
    """
    clone = json.loads(json.dumps(stage))
    for entry in _container_entries(clone):
        slot = _script_slot(entry["container"])
        if slot:
            entry["container"][slot[0]][slot[1]] = _SCRIPT_MASK
    return clone


def _field_changes(old_stage: dict, new_stage: dict) -> list[dict]:
    """Compute field-level changes between two matched stages (excluding refIds/deps
    and embedded shell, which get dedicated treatment)."""
    def strip(stage: dict) -> dict:
        return {k: v for k, v in _mask_scripts(stage).items() if k not in _IGNORE_STAGE_KEYS}

    changes: list[dict] = []
    for row in _diff_tree(strip(old_stage), strip(new_stage)):
        category, severity = _classify_field(row["path"])
        # A command/args list change with the script masked out means the
        # interpreter or its flags moved — still a shell concern.
        if re.search(r"\.(command|args)$", row["path"]):
            category, severity = ("shell-change", "critical")
        changes.append({**row, "category": category, "severity": severity})
    return changes


# Keys worth listing when a whole stage arrives or departs. A wholly-added stage has
# no counterpart to diff against, so a structural diff would emit one row per
# top-level key — `manifests` alone being a multi-kilobyte blob. These are the leaves
# a reviewer actually checks on a new deploy stage.
_CONTENT_KEYS = {
    "image", "imagePullPolicy", "serviceAccountName", "restartPolicy",
    "schedule", "suspend", "concurrencyPolicy", "startingDeadlineSeconds",
    "successfulJobsHistoryLimit", "failedJobsHistoryLimit",
    "activeDeadlineSeconds", "ttlSecondsAfterFinished", "backoffLimit",
    "parallelism", "completions", "replicas",
    "cpu", "memory", "expression", "account", "namespace",
}


def _content_rows(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Walk a subtree collecting ``(path, value)`` for the leaves in ``_CONTENT_KEYS``.

    Env vars and container command lines are always collected: they are the payload
    of a job stage regardless of which key spelled them.
    """
    rows: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{path}.{k}" if path else k
            if k == "env" and isinstance(v, list):
                for i, e in enumerate(v):
                    if isinstance(e, dict) and isinstance(e.get("name"), str):
                        val = e.get("value")
                        if val is None and "valueFrom" in e:
                            val = e["valueFrom"]
                        rows.append((f"{sub}[{e['name']}]", val))
                    else:
                        rows.append((f"{sub}[{i}]", e))
                continue
            if k in ("command", "args") and isinstance(v, list):
                rows.append((sub, v))
                continue
            if isinstance(v, (dict, list)):
                rows.extend(_content_rows(v, sub))
            elif k in _CONTENT_KEYS:
                rows.append((sub, v))
    elif isinstance(node, list):
        keyed = _keyed(node)
        items = keyed.items() if keyed else ((str(i), v) for i, v in enumerate(node))
        for k, v in items:
            rows.extend(_content_rows(v, f"{path}[{k}]"))
    return rows


def _stage_contents(stage: dict, side: str) -> list[dict]:
    """One-sided "what is in this stage" rows for a stage that was added or removed.

    Shaped exactly like ``_field_changes`` output so the UI needs no second row type:
    an added stage fills ``new``, a removed stage fills ``old``.
    """
    masked = {k: v for k, v in _mask_scripts(stage).items() if k not in _IGNORE_STAGE_KEYS}
    rows: list[dict] = []
    for path, value in sorted(_content_rows(masked), key=lambda r: r[0]):
        category, severity = _classify_field(path)
        if re.search(r"\.(command|args)$", path):
            category, severity = ("shell-change", "critical")
        rows.append({
            "path": path,
            "changeType": side,
            "old": value if side == "removed" else None,
            "new": value if side == "added" else None,
            "category": category,
            "severity": severity,
        })
    return rows


# ---------------------------------------------------------------------------
# Embedded-shell diff
# ---------------------------------------------------------------------------

def _shell_hunks(old_script: str, new_script: str) -> list[dict]:
    """Line-diff two shell scripts into hunks the frontend can render."""
    old_lines = old_script.splitlines()
    new_lines = new_script.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "op": {"replace": "change", "delete": "del", "insert": "add"}[tag],
            "oldStart": i1 + 1, "oldLines": old_lines[i1:i2],
            "newStart": j1 + 1, "newLines": new_lines[j1:j2],
        })
    return hunks


def _shell_diffs(old_stage: dict | None, new_stage: dict | None) -> list[dict]:
    """Per-container embedded-shell diffs for a matched stage.

    Keyed by ``manifest label + container name``: a deployManifest stage deploying
    four CronJob legs has four containers all called ``sdk-cache-enqueue``, and
    each carries its own script.
    """
    def shells(stage: dict | None) -> dict[str, dict]:
        result: dict[str, dict] = {}
        if not stage:
            return result
        for entry in _container_entries(stage):
            slot = _script_slot(entry["container"])
            if not slot:
                continue
            result[f"{entry['manifest']}\x00{entry['name']}"] = {
                "container": entry["name"],
                "manifest": entry["manifest"],
                "kind": entry["kind"],
                "location": f"{slot[0]}[{slot[1]}]",
                "script": entry["container"][slot[0]][slot[1]],
            }
        return result

    old_sh = shells(old_stage)
    new_sh = shells(new_stage)
    diffs: list[dict] = []
    for key in sorted(set(old_sh) | set(new_sh)):
        o = old_sh.get(key)
        n = new_sh.get(key)
        old_script = o["script"] if o else ""
        new_script = n["script"] if n else ""
        if old_script == new_script:
            continue
        side = n or o
        diffs.append({
            "container": side["container"],
            "manifest": side["manifest"],
            "kind": side["kind"],
            "location": side["location"],
            "changed": True,
            "old": old_script,
            "new": new_script,
            "hunks": _shell_hunks(old_script, new_script),
        })
    return diffs


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

def _lint_stage(stage: dict) -> list[dict]:
    """Run the lint checks against a single (added or changed) stage."""
    flags: list[dict] = []
    stype = stage.get("type")
    is_job = stype in ("runJobManifest", "deployManifest")

    for c in _stage_containers(stage):
        image = c.get("image")
        # An image built from SpEL (`${image}`, `#stage(...)`) resolves at execution
        # time, so its registry is unknowable here — linting it only cries wolf.
        unresolved = isinstance(image, str) and ("${" in image or "#{" in image or "#stage(" in image)
        allowed = _allowed_image_prefixes()
        if (allowed and isinstance(image, str) and not unresolved
                and not any(image.startswith(p) for p in allowed)):
            flags.append({
                "id": "nonmirror-image",
                "severity": "critical",
                "message": f"Image '{image}' is not from an approved registry (PIPELINE_IMAGE_REGISTRY_PREFIXES).",
            })
        shell = _extract_shell(c)
        if shell:
            has_spel = "${parameters" in shell or "${trigger" in shell or "#stage(" in shell
            has_shell_expansion = bool(re.search(r"\$\{[A-Za-z_]", shell))
            skip_eval = stage.get("skipExpressionEvaluation") is True
            if has_spel and has_shell_expansion and not skip_eval:
                flags.append({
                    "id": "spel-shell-collision",
                    "severity": "serious",
                    "message": "Command contains both SpEL (${parameters/#stage}) and raw shell ${VAR} "
                               "expansion without skipExpressionEvaluation — SpEL v4 will eat the shell.",
                })
            if stage.get("propertyFile") and (re.search(r"(^|\s)#", shell) or "$(" in shell) and not skip_eval:
                flags.append({
                    "id": "annotation-hash-spel",
                    "severity": "warning",
                    "message": "propertyFile stage embeds '#' or $(...) without skipExpressionEvaluation — "
                               "risks EL1041E on last-applied-configuration.",
                })

    if is_job:
        moniker = stage.get("moniker") or {}
        if not moniker.get("app"):
            flags.append({
                "id": "moniker-shape",
                "severity": "serious",
                "message": "runJob/deployManifest stage missing moniker.app — risks the Moniker cast crash.",
            })
        # Job retry semantics — CronJob nests the job spec under jobTemplate.
        for label, manifest in _stage_manifests(stage):
            spec = manifest.get("spec") or {}
            if manifest.get("kind") == "CronJob":
                spec = (spec.get("jobTemplate") or {}).get("spec") or {}
            elif manifest.get("kind") != "Job":
                continue
            if spec.get("backoffLimit") not in (0, "0"):
                flags.append({
                    "id": "job-retry",
                    "severity": "warning",
                    "message": f"{label} does not set backoffLimit: 0 — a retried job can be "
                               "dangerous and hides pod errors.",
                })
    return flags


# ---------------------------------------------------------------------------
# Keyed collection diffs (parameters, triggers, notifications, artifacts)
# ---------------------------------------------------------------------------

def _diff_keyed(old_list, new_list, key_fn, category, severity) -> list[dict]:
    """Diff two lists as maps keyed by key_fn(item)."""
    old_list = old_list or []
    new_list = new_list or []
    old_map = {key_fn(x): x for x in old_list if isinstance(x, dict)}
    new_map = {key_fn(x): x for x in new_list if isinstance(x, dict)}
    out: list[dict] = []
    for k in sorted(set(old_map) | set(new_map)):
        o = old_map.get(k)
        n = new_map.get(k)
        if o is None:
            out.append({"identity": k, "changeType": "added", "category": category,
                        "severity": severity, "new": n})
        elif n is None:
            out.append({"identity": k, "changeType": "removed", "category": category,
                        "severity": severity, "old": o})
        elif _canonical_json(o) != _canonical_json(n):
            out.append({"identity": k, "changeType": "changed", "category": category,
                        "severity": severity, "old": o, "new": n})
    return out


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

def _refid_to_identity(stages: list[dict]) -> dict[str, str]:
    """Map each stage's refId to a stable identity string (display name based)."""
    out: dict[str, str] = {}
    for s in stages:
        ref = str(s.get("refId", ""))
        out[ref] = (s.get("name") or ref).strip()
    return out


def _edge_set(stages: list[dict]) -> set[tuple[str, str]]:
    """Directed edges (parent -> child) in stage-identity space."""
    id_map = _refid_to_identity(stages)
    edges: set[tuple[str, str]] = set()
    for s in stages:
        child = (s.get("name") or str(s.get("refId", ""))).strip()
        for parent_ref in (s.get("requisiteStageRefIds") or []):
            parent = id_map.get(str(parent_ref), str(parent_ref))
            edges.add((parent, child))
    return edges


def _roots(stages: list[dict]) -> set[str]:
    return {
        (s.get("name") or str(s.get("refId", ""))).strip()
        for s in stages
        if not (s.get("requisiteStageRefIds") or [])
    }


def _dag_diff(old_stages: list[dict], new_stages: list[dict]) -> dict:
    e_old = _edge_set(old_stages)
    e_new = _edge_set(new_stages)
    r_old = _roots(old_stages)
    r_new = _roots(new_stages)
    to_edge = lambda pair: {"from": pair[0], "to": pair[1]}
    return {
        "edgesAdded": [to_edge(e) for e in sorted(e_new - e_old)],
        "edgesRemoved": [to_edge(e) for e in sorted(e_old - e_new)],
        "rootsAdded": sorted(r_new - r_old),
        "rootsRemoved": sorted(r_old - r_new),
    }


def _layer_levels(stages: list[dict]) -> dict[str, int]:
    """Longest-path depth (topological level) per stage identity, for L→R layout.

    Level 0 = root (no requisites). Cycles / dangling refs degrade gracefully to
    level 0 rather than looping forever.
    """
    id_map = _refid_to_identity(stages)
    # child identity -> list of parent identities
    parents: dict[str, list[str]] = {}
    for s in stages:
        child = (s.get("name") or str(s.get("refId", ""))).strip()
        parents[child] = [
            id_map.get(str(p), str(p)) for p in (s.get("requisiteStageRefIds") or [])
        ]

    level: dict[str, int] = {}

    def depth(node: str, seen: frozenset) -> int:
        if node in level:
            return level[node]
        ps = [p for p in parents.get(node, []) if p in parents and p not in seen]
        d = 0 if not ps else 1 + max(depth(p, seen | {node}) for p in ps)
        level[node] = d
        return d

    for node in parents:
        depth(node, frozenset())
    return level


def _dag_graph(old_stages, new_stages, stage_change_by_id: dict[str, str]) -> dict:
    """Full renderable graph: union of nodes (with change status + layer) and edges.

    Layers come from the NEW pipeline where a node exists there, else the OLD one,
    so removed nodes still get a sensible column.
    """
    old_ids = {(s.get("name") or str(s.get("refId", ""))).strip(): s for s in old_stages}
    new_ids = {(s.get("name") or str(s.get("refId", ""))).strip(): s for s in new_stages}
    type_of = {}
    for ident, s in {**old_ids, **new_ids}.items():
        type_of[ident] = s.get("type", "")

    lvl_new = _layer_levels(new_stages)
    lvl_old = _layer_levels(old_stages)

    nodes = []
    for ident in sorted(set(old_ids) | set(new_ids)):
        if ident in new_ids and ident not in old_ids:
            status = "added"
        elif ident in old_ids and ident not in new_ids:
            status = "removed"
        else:
            status = stage_change_by_id.get(ident, "unchanged")
        level = lvl_new.get(ident, lvl_old.get(ident, 0))
        nodes.append({
            "id": ident,
            "type": type_of.get(ident, ""),
            "status": status,
            "level": level,
        })

    e_old = _edge_set(old_stages)
    e_new = _edge_set(new_stages)
    edges = []
    for e in sorted(e_old | e_new):
        if e in e_new and e not in e_old:
            status = "added"
        elif e in e_old and e not in e_new:
            status = "removed"
        else:
            status = "unchanged"
        edges.append({"from": e[0], "to": e[1], "status": status})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def _worst(severities: list[str]) -> str:
    if not severities:
        return "minor"
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0))


def _stage_display(stage: dict) -> str:
    return (stage.get("name") or str(stage.get("refId", "stage"))).strip()


def build_pipeline_diff(old_content: str | None, new_content: str | None) -> dict | None:
    """Build a semantic diff object for a changed .pp file.

    Returns None if neither side parses as a pipeline (caller falls back to raw diff).
    For an added file, old_content is None; for a deleted file, new_content is None.
    """
    old = _parse_pipeline(old_content)
    new = _parse_pipeline(new_content)
    if old is None and new is None:
        return None

    old_stages = (old or {}).get("stages", [])
    new_stages = (new or {}).get("stages", [])

    if old is None:
        file_change_type = "added"
    elif new is None:
        file_change_type = "deleted"
    else:
        file_change_type = "modified"

    # ---- per-stage ----
    records = _match_stages(old_stages, new_stages)
    stage_results: list[dict] = []
    all_severities: list[str] = []
    counts = {"added": 0, "removed": 0, "renamed": 0, "changed": 0, "unchanged": 0}
    total_lint = 0

    for rec in records:
        o, n = rec["old"], rec["new"]
        if o is None:
            # added stage
            lint = _lint_stage(n)
            total_lint += len(lint)
            counts["added"] += 1
            all_severities.append("critical")
            stage_results.append({
                "identity": _stage_display(n),
                "displayName": _stage_display(n),
                "changeType": "added",
                "changeCategories": ["stage-added"],
                "severity": "critical",
                "matchStrategy": None,
                "matchConfidence": 1.0,
                "old": None,
                "new": {"refId": n.get("refId"), "type": n.get("type")},
                "fieldChanges": _stage_contents(n, "added"),
                "shellDiffs": _shell_diffs(None, n),
                "lintFlags": lint,
            })
            continue
        if n is None:
            counts["removed"] += 1
            all_severities.append("critical")
            stage_results.append({
                "identity": _stage_display(o),
                "displayName": _stage_display(o),
                "changeType": "removed",
                "changeCategories": ["stage-removed"],
                "severity": "critical",
                "matchStrategy": None,
                "matchConfidence": 1.0,
                "old": {"refId": o.get("refId"), "type": o.get("type")},
                "new": None,
                "fieldChanges": _stage_contents(o, "removed"),
                "shellDiffs": _shell_diffs(o, None),
                "lintFlags": [],
            })
            continue

        # matched — compute field + shell diffs
        field_changes = _field_changes(o, n)
        shell_diffs = _shell_diffs(o, n)
        # dependency change?
        dep_old = _canonical_json(o.get("requisiteStageRefIds") or [])
        dep_new = _canonical_json(n.get("requisiteStageRefIds") or [])
        deps_changed = dep_old != dep_new
        renamed = (o.get("name") or "") != (n.get("name") or "")

        categories: list[str] = []
        severities: list[str] = []
        for fc in field_changes:
            categories.append(fc["category"])
            severities.append(fc["severity"])
        if shell_diffs:
            categories.append("shell-change")
            severities.append("critical")
        if deps_changed:
            categories.append("dag-edge-change")
            severities.append("critical")
        if renamed:
            categories.append("rename")
            severities.append("minor")

        if not categories:
            counts["unchanged"] += 1
            continue

        lint = _lint_stage(n)
        total_lint += len(lint)
        if renamed and set(categories) == {"rename"}:
            change_type = "renamed"
            counts["renamed"] += 1
        else:
            change_type = "changed"
            counts["changed"] += 1

        severity = _worst(severities)
        all_severities.append(severity)
        # append a synthetic dep field-change for the UI
        if deps_changed:
            field_changes.append({
                "path": "requisiteStageRefIds",
                "category": "dag-edge-change",
                "severity": "critical",
                "old": o.get("requisiteStageRefIds") or [],
                "new": n.get("requisiteStageRefIds") or [],
            })

        stage_results.append({
            "identity": _stage_display(n),
            "displayName": _stage_display(n),
            "changeType": change_type,
            "changeCategories": sorted(set(categories)),
            "severity": severity,
            "matchStrategy": rec["strategy"],
            "matchConfidence": rec["confidence"],
            "old": {"refId": o.get("refId"), "name": o.get("name")},
            "new": {"refId": n.get("refId"), "name": n.get("name")},
            "fieldChanges": field_changes,
            "shellDiffs": shell_diffs,
            "lintFlags": lint,
        })

    # ---- pipeline-level collections ----
    parameters = _diff_keyed(
        (old or {}).get("parameterConfig"), (new or {}).get("parameterConfig"),
        lambda x: x.get("name", ""), "parameter", "major",
    )
    triggers = _diff_keyed(
        (old or {}).get("triggers"), (new or {}).get("triggers"),
        lambda x: f"{x.get('type', '')}:{x.get('source', x.get('id', ''))}", "trigger", "major",
    )
    notifications = _diff_keyed(
        (old or {}).get("notifications"), (new or {}).get("notifications"),
        lambda x: f"{x.get('type', '')}:{x.get('address', '')}", "notification", "minor",
    )
    artifacts = _diff_keyed(
        (old or {}).get("expectedArtifacts"), (new or {}).get("expectedArtifacts"),
        lambda x: str(x.get("displayName", x.get("id", ""))), "artifact", "minor",
    )
    for group in (parameters, triggers, notifications, artifacts):
        for item in group:
            all_severities.append(item["severity"])

    # (meta severities folded in after meta is computed below)

    # ---- pipeline-level meta knobs ----
    # Diff every top-level key EXCEPT the ones handled by dedicated diffs above.
    # Flatten so nested config (e.g. a templated pipeline's variables.helmChartVersion,
    # which lives outside stages[]) is captured, not just top-level scalars.
    meta: list[dict] = []
    _META_SKIP = {"stages", "parameterConfig", "triggers", "notifications",
                  "expectedArtifacts"}
    if old is not None and new is not None:
        old_meta = {k: v for k, v in old.items() if k not in _META_SKIP}
        new_meta = {k: v for k, v in new.items() if k not in _META_SKIP}
        for row in _diff_tree(old_meta, new_meta):
            _, severity = _classify_field(row["path"])
            meta.append({**row, "category": "pipeline-config", "severity": severity})
            all_severities.append(severity)

    # ---- DAG ----
    dag = _dag_diff(old_stages, new_stages)
    # Full renderable graph (nodes + edges w/ status + layer) for the visual DAG.
    stage_change_by_id = {sr["identity"]: sr["changeType"] for sr in stage_results}
    dag["graph"] = _dag_graph(old_stages, new_stages, stage_change_by_id)

    # ---- bulk-change collapsing (same field+old+new across many stages) ----
    bulk_index: dict[tuple, list[str]] = {}
    for sr in stage_results:
        for fc in sr.get("fieldChanges", []):
            if fc["category"] in ("dag-edge-change", "shell-change"):
                continue
            k = (fc["path"], _canonical_json(fc.get("old")), _canonical_json(fc.get("new")))
            bulk_index.setdefault(k, []).append(sr["identity"])
    bulk_changes = [
        {"path": k[0], "old": json.loads(k[1]), "new": json.loads(k[2]),
         "count": len(v), "stages": sorted(v)}
        for k, v in bulk_index.items() if len(v) >= 3
    ]

    shell_edits = sum(1 for sr in stage_results if sr.get("shellDiffs"))
    shell_added = shell_removed = 0
    for sr in stage_results:
        for sd in sr.get("shellDiffs", []):
            for h in sd.get("hunks", []):
                shell_added += len(h.get("newLines") or [])
                shell_removed += len(h.get("oldLines") or [])

    summary = {
        "stagesAdded": counts["added"],
        "stagesRemoved": counts["removed"],
        "stagesRenamed": counts["renamed"],
        "stagesChanged": counts["changed"],
        "edgesAdded": len(dag["edgesAdded"]),
        "edgesRemoved": len(dag["edgesRemoved"]),
        "shellEdits": shell_edits,
        "shellLinesAdded": shell_added,
        "shellLinesRemoved": shell_removed,
        "lintFlags": total_lint,
        "pureReorder": (counts["added"] == 0 and counts["removed"] == 0
                        and counts["changed"] == 0 and counts["renamed"] == 0
                        and (dag["edgesAdded"] or dag["edgesRemoved"])),
        "topSeverity": _worst(all_severities),
        "bulkChanges": bulk_changes,
    }

    pipeline_name = (new or old or {}).get("name", "")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "pipeline": {"name": pipeline_name},
        "fileChangeType": file_change_type,
        "summary": summary,
        "meta": meta,
        "parameters": parameters,
        "triggers": triggers,
        "notifications": notifications,
        "expectedArtifacts": artifacts,
        "dag": dag,
        "stages": stage_results,
    }
