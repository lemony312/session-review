"""Validation for pipeline_diff against synthetic .pp before/after pairs.

Runs with plain python3 (no Docker, no git clones). Fixtures live in
tests/fixtures/pipelines/<case>.old.pp and <case>.new.pp and assert the semantic
diff detects the change a reviewer cares about. Run:  python3 test_pipeline_diff.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from pipeline_diff import _stage_containers, build_pipeline_diff  # noqa: E402

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "pipelines"


def fx(name: str, side: str) -> str:
    return (FIXTURES / f"{name}.{side}.pp").read_text()


def pair(name: str):
    return fx(name, "old"), fx(name, "new")


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# --- Case 1: stages-removed: 2 stages + 2 edges removed ---
print("Case 1: stages-removed (2 stages + 2 edges removed)")
old, new = pair("stages-removed")
d = build_pipeline_diff(old, new)
check("parses to a diff object", d is not None)
if d:
    check("fileChangeType modified", d["fileChangeType"] == "modified", d["fileChangeType"])
    check("2 stages removed", d["summary"]["stagesRemoved"] == 2, d["summary"]["stagesRemoved"])
    check("edges removed >= 2", d["summary"]["edgesRemoved"] >= 2, d["summary"]["edgesRemoved"])
    removed = [s["displayName"] for s in d["stages"] if s["changeType"] == "removed"]
    check("removed stages named", len(removed) == 2, removed)


# --- Case 2: trigger-added: webhook trigger added ---
print("Case 2: trigger-added (webhook trigger added)")
old, new = pair("trigger-added")
d = build_pipeline_diff(old, new)
check("parses to a diff object", d is not None)
if d:
    added_triggers = [t for t in d["triggers"] if t["changeType"] == "added"]
    check("1 trigger added", len(added_triggers) == 1, [t["identity"] for t in d["triggers"]])
    check("trigger is a webhook", any("webhook" in t["identity"] for t in added_triggers),
          [t["identity"] for t in added_triggers])


# --- Case 3: meta-bump: chart version bump in a templated pipeline ---
print("Case 3: meta-bump helmChartVersion-style bump (templated, no stages)")
old, new = pair("meta-bump")
d = build_pipeline_diff(old, new)
check("parses to a diff object", d is not None)
if d:
    # This is a templated pipeline (variables.chartVersion, no stages) — the
    # bump lands in `meta`. Assert it's detected with the right old→new values.
    hcv = [m for m in d["meta"] if "chartVersion" in m["path"]]
    check("chartVersion change detected in meta", bool(hcv),
          f"meta paths={[m['path'] for m in d['meta']]}")
    if hcv:
        check("old 0.20.3 -> new 0.21.0",
              hcv[0]["old"] == "0.20.3" and hcv[0]["new"] == "0.21.0",
              f"{hcv[0]['old']} -> {hcv[0]['new']}")


# --- Case 4: pure re-serialization is NOT a semantic change ---
print("Case 4: reordered keys / whitespace = no semantic change")
sample = fx("stages-removed", "old")
if sample:
    obj = json.loads(sample)
    # Re-serialize with different key order + indentation
    reordered = json.dumps(obj, sort_keys=True, indent=4)
    scrambled = json.dumps({k: obj[k] for k in reversed(list(obj))}, indent=2)
    d = build_pipeline_diff(scrambled, reordered)
    check("parses", d is not None)
    if d:
        s = d["summary"]
        noise = (s["stagesAdded"] + s["stagesRemoved"] + s["stagesChanged"]
                 + s["stagesRenamed"] + s["edgesAdded"] + s["edgesRemoved"])
        check("zero semantic changes from re-serialization", noise == 0,
              f"noise={noise} summary={s}")


# --- Case 5: added pipeline (old=None) ---
print("Case 5: brand-new pipeline (old side absent)")
sample = fx("stages-removed", "old")
d = build_pipeline_diff(None, sample)
check("parses", d is not None)
if d:
    check("fileChangeType added", d["fileChangeType"] == "added", d["fileChangeType"])
    check("all stages added", d["summary"]["stagesAdded"] == len(d["stages"]),
          f"{d['summary']['stagesAdded']} vs {len(d['stages'])}")


# --- Case 6: non-pipeline .pp (Puppet / junk) -> None, falls back to raw diff ---
print("Case 6: non-pipeline content returns None (raw-diff fallback)")
check("puppet-ish text -> None", build_pipeline_diff("class foo { }", "class foo { bar }") is None)
check("json without stages -> None", build_pipeline_diff('{"a":1}', '{"a":2}') is None)
check("both empty -> None", build_pipeline_diff(None, None) is None)


# A field change whose old/new is a wall of JSON is the failure mode this renderer
# exists to avoid: the real edit is in there, but nobody can read it.
BLOB = 300


def blobs_in(diff) -> list[str]:
    out = []
    for st in diff["stages"]:
        for fc in st["fieldChanges"]:
            size = max(len(json.dumps(fc.get("old"))), len(json.dumps(fc.get("new"))))
            if size > BLOB:
                out.append(f"{st['displayName']}:{fc['path']}({size}B)")
    return out


def paths_in(diff) -> list[str]:
    return [fc["path"] for st in diff["stages"] for fc in st["fieldChanges"]]


# --- Case 7: shell-edit: a pure embedded-bash edit ---
print("Case 7: shell-edit (bash-only edit reads as bash, not JSON)")
old, new = pair("shell-edit")
d = build_pipeline_diff(old, new)
check("parses", d is not None)
if d:
    check("2 shell edits", d["summary"]["shellEdits"] == 2, d["summary"]["shellEdits"])
    check("shell line counts reported",
          d["summary"].get("shellLinesAdded", 0) > 0 and d["summary"].get("shellLinesRemoved", 0) > 0,
          f"+{d['summary'].get('shellLinesAdded')} -{d['summary'].get('shellLinesRemoved')}")
    check("no JSON-blob field rows", not blobs_in(d), blobs_in(d))
    # The script body belongs to shellDiffs only — masked out of fieldChanges.
    check("script not duplicated as a field change",
          not any(".command" in p or p.endswith("containers") for p in paths_in(d)),
          paths_in(d))
    sd = [s for st in d["stages"] for s in st["shellDiffs"]]
    check("shell diff located", all(s.get("location") for s in sd),
          [s.get("location") for s in sd])
    check("shell diff labelled with its manifest", all(s.get("manifest") for s in sd),
          [s.get("manifest") for s in sd])


# --- Case 8: cronjob: deployManifest + manifests[] + CronJob ---
print("Case 8: cronjob (manifests[] / CronJob nesting)")
old, new = pair("cronjob")
d = build_pipeline_diff(old, new)
check("parses", d is not None)
if d:
    p = paths_in(d)
    check("no JSON-blob field rows", not blobs_in(d), blobs_in(d))
    check("new env var surfaced by name", any("MAX_TIME" in x for x in p), p[:8])
    check("ttlSecondsAfterFinished surfaced", any("ttlSecondsAfterFinished" in x for x in p), p[:8])
    check("activeDeadlineSeconds surfaced", any("activeDeadlineSeconds" in x for x in p), p[:8])
    check("not written off as minor", d["summary"]["topSeverity"] != "minor",
          d["summary"]["topSeverity"])
    # CronJob containers live under spec.jobTemplate.spec.template.spec.containers.
    cronjob_stage = json.loads(new)["stages"][1]
    check("CronJob containers discovered", len(_stage_containers(cronjob_stage)) >= 1,
          len(_stage_containers(cronjob_stage)))


# --- Case 9: shell script passed via args, not command[2] ---
print("Case 9: script in args[] is still a shell diff")


def one_stage_pipeline(container: dict) -> str:
    return json.dumps({
        "name": "p", "stages": [{
            "refId": "1", "type": "runJobManifest", "name": "Run",
            "moniker": {"app": "x"}, "requisiteStageRefIds": [],
            "manifest": {"kind": "Job", "spec": {"backoffLimit": 0, "template": {
                "spec": {"containers": [container]}}}},
        }],
    })


base = {"name": "job", "image": "registry.example.com/tools/alpine:3",
        "command": ["/bin/bash"], "args": ["-c", "set -eu\necho one\necho two\n"]}
edited = json.loads(json.dumps(base))
edited["args"][1] = "set -eu\necho ONE\necho two\n"
d = build_pipeline_diff(one_stage_pipeline(base), one_stage_pipeline(edited))
check("parses", d is not None)
if d:
    check("1 shell edit", d["summary"]["shellEdits"] == 1, d["summary"]["shellEdits"])
    sd = d["stages"][0]["shellDiffs"]
    check("located in args", bool(sd) and sd[0]["location"] == "args[1]",
          [s.get("location") for s in sd])
    check("no JSON-blob field rows", not blobs_in(d), blobs_in(d))


# --- Case 10: an unresolved SpEL image is not a mirror violation ---
print("Case 10: ${image} is not linted as a non-mirror image")
os.environ["PIPELINE_IMAGE_REGISTRY_PREFIXES"] = "registry.example.com/tools/, mirror.example.com/"
spel = json.loads(json.dumps(base))
spel["image"] = "${image}"
d = build_pipeline_diff(None, one_stage_pipeline(spel))
check("parses", d is not None)
if d:
    ids = [f["id"] for f in d["stages"][0]["lintFlags"]]
    check("no nonmirror-image flag on a SpEL image", "nonmirror-image" not in ids, ids)
    hard = json.loads(json.dumps(base))
    hard["image"] = "docker.io/library/alpine:3"
    d2 = build_pipeline_diff(None, one_stage_pipeline(hard))
    ids2 = [f["id"] for f in d2["stages"][0]["lintFlags"]]
    check("still flags a literal non-mirror image", "nonmirror-image" in ids2, ids2)
    ok = json.loads(json.dumps(base))
    ok["image"] = "mirror.example.com/x/alpine:3"
    ids3 = [f["id"] for f in build_pipeline_diff(None, one_stage_pipeline(ok))["stages"][0]["lintFlags"]]
    check("image under any configured prefix is not flagged", "nonmirror-image" not in ids3, ids3)
    del os.environ["PIPELINE_IMAGE_REGISTRY_PREFIXES"]
    ids4 = [f["id"] for f in build_pipeline_diff(None, one_stage_pipeline(hard))["stages"][0]["lintFlags"]]
    check("no prefixes configured: image lint is off", "nonmirror-image" not in ids4, ids4)


# --- Case 11: a stage added or removed wholesale still shows what is in it ---
# "Stage added, critical" with nothing under it tells a reviewer nothing: the image,
# the schedule, the env and the SpEL that decides whether the leg runs are the change.
print("Case 11: added/removed stages carry their contents")
old, new = pair("legs")
d = build_pipeline_diff(old, new)
check("parses", d is not None and old is not None and new is not None)
if d:
    added = [s for s in d["stages"] if s["changeType"] == "added"]
    removed = [s for s in d["stages"] if s["changeType"] == "removed"]
    check("3 added / 2 removed stages", len(added) == 3 and len(removed) == 2,
          f"{len(added)}/{len(removed)}")
    qa = next((s for s in added if "qa" in s["displayName"]), None)
    rows = {fc["path"]: fc for fc in (qa or {}).get("fieldChanges", [])}
    check("added stage has contents", len(rows) > 3, len(rows))
    check("its schedule is shown", any("schedule" in p for p in rows), sorted(rows)[:8])
    check("its image is shown", any(p.endswith(".image") for p in rows), sorted(rows)[:8])
    check("its env is shown by name", any(".env[" in p for p in rows), sorted(rows)[:8])
    check("the SpEL gating the leg is shown",
          any("stageEnabled" in p for p in rows), sorted(rows)[:8])
    check("contents are one-sided (new only)",
          all(fc["old"] is None and fc["changeType"] == "added" for fc in rows.values()))
    check("no JSON-blob field rows", not blobs_in(d), blobs_in(d))
    check("removed stage has contents too",
          bool(removed) and len(removed[0]["fieldChanges"]) > 3,
          len(removed[0]["fieldChanges"]) if removed else None)
    check("removed contents are one-sided (old only)",
          all(fc["new"] is None and fc["changeType"] == "removed"
              for fc in removed[0]["fieldChanges"]) if removed else False)

# A deleted script is a deleted script — show it, one-sided.
gone = build_pipeline_diff(one_stage_pipeline(base), json.dumps({"name": "p", "stages": []}))
check("removing a stage parses", gone is not None)
if gone:
    sd = gone["stages"][0]["shellDiffs"]
    check("deleted script surfaces as an all-removed shell diff",
          bool(sd) and sd[0]["new"] == "" and "echo one" in sd[0]["old"],
          [(s.get("location"), len(s.get("old", "")), len(s.get("new", ""))) for s in sd])
brand_new = build_pipeline_diff(json.dumps({"name": "p", "stages": []}),
                                one_stage_pipeline(base))
if brand_new:
    sd = brand_new["stages"][0]["shellDiffs"]
    check("new script surfaces as an all-added shell diff",
          bool(sd) and sd[0]["old"] == "" and "echo one" in sd[0]["new"],
          [(s.get("location"), len(s.get("old", "")), len(s.get("new", ""))) for s in sd])


print()
print(f"{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
