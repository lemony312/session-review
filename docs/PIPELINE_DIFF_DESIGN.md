# Pipeline Diff Viewer: Design

A specialised renderer for Spinnaker pipeline (`.pp`, JSON) diffs. It replaces the raw-JSON line diff, which is unreadable for pipelines, with a semantic view.

## The problem

A `.pp` is hundreds of lines of JSON encoding a DAG of deploy stages. A `git diff` on it is poor for review:

- The real logic lives in embedded shell strings (`command[2]`), so a 40-line script edit collapses to one very long changed JSON line.
- Stage relationships live in `refId` / `requisiteStageRefIds` arrays, so moving a stage shows as a delete and re-add with no "same stage moved" signal.
- Round-tripping a pipeline through the Spinnaker UI or API rewrites the whole file (key order, refId renumbering), so a cosmetic edit looks like a total rewrite.
- Risk (SpEL/shell collisions, images outside approved registries, ordering races) is semantic and invisible in a line diff.

Matching therefore has to be structural, not textual.

## Architecture

```
.pp file changed
  -> src/pipeline_diff.py: parse both blobs, normalise, match stages by identity,
     compute a semantic diff object
  -> stored on the review file (render_mode="pipeline", pipeline_diff JSON)
  -> static/js/pipeline-diff.js renders the summary, stage cards and shell sub-diffs
```

## Data model (`pipeline_diff.py`)

### Stage identity

`refId` is not stable: some pipelines use numeric `1..n`, others semantic slugs. Stages are matched with a cascade:

1. Semantic refId (non-numeric slug), matched directly.
2. `(type, normalised name)`.
3. Content fingerprint (type, `moniker.app`, image repo, normalised script, sorted env names), which rescues rename-plus-edit. The result records `matchStrategy` and `matchConfidence`.
4. Unmatched old stages are `removed`; unmatched new stages are `added`.

### Normalisation

Parse the JSON, sort keys recursively, and key arrays by their natural identity (`requisiteStageRefIds` as a set of stage identities; `env`, `params`, `triggers` as maps). `stages[]` order is kept. Pure key-order or refId-renumber churn diffs to nothing.

### Change taxonomy

- **Critical:** stage added/removed, DAG edge change, embedded-shell change, image change, `stageEnabled`/SpEL change.
- **Major:** env vars, resources, chart version, replicas, service account, account, job retry semantics, property-file plumbing, `skipExpressionEvaluation`, triggers, parameters.
- **Minor:** rename, description/labels, notifications, expected artifacts. Pure reordering is suppressed.

Identical changes repeated across many stages (for example one version bumped in every stage) collapse into a single summary row.

### DAG diff

Edges are expressed in stage-identity space: `edgesAdded`/`edgesRemoved`, `rootsAdded`/`rootsRemoved`.

### Embedded shell

Each container's script is extracted from `command[2]` or `args[]`, unescaped, and line-diffed into hunks. A script in an added or removed stage is shown one-sided.

### Lint flags

Inline flags for common authoring mistakes: `spel-shell-collision` (SpEL and shell `${...}` in one script without `skipExpressionEvaluation`), `nonmirror-image`, `annotation-hash-spel`, `moniker-shape`, `sa-ordering`, `job-retry`, `propertyfile-contract`.

`nonmirror-image` flags a literal image that starts with none of the prefixes in `PIPELINE_IMAGE_REGISTRY_PREFIXES` (comma-separated). It is off when the variable is unset, and images built from SpEL are skipped because their registry is unknown until execution.

### Output

```jsonc
{
  "schemaVersion": "1.0",
  "pipeline": { "file", "name", "oldRef", "newRef" },
  "fileChangeType": "modified|added|deleted|renamed",
  "summary": { "stagesAdded", "stagesRemoved", "stagesRenamed", "stagesChanged",
               "edgesAdded", "edgesRemoved", "shellEdits", "topSeverity", "bulkChanges": [] },
  "meta": [], "parameters": [], "triggers": [], "notifications": [], "expectedArtifacts": [],
  "dag": { "edgesAdded", "edgesRemoved", "rootsAdded", "rootsRemoved" },
  "stages": [ { "identity", "displayName", "changeType", "changeCategories", "severity",
                "fieldChanges": [{ "path", "category", "severity", "old", "new" }],
                "shellDiffs": [{ "container", "manifest", "location", "changed", "hunks" }],
                "lintFlags": [{ "id", "severity", "message" }] } ]
}
```

## Rendering (`pipeline-diff.js`)

A summary header, then one card per changed stage, most severe first. Each card shows lint flags, the embedded shell diff (always open, with unchanged runs folded and changed spans highlighted) and the config rows. Many config rows fold behind a labelled count so they cannot bury the script. Added and removed stages list what they brought or took away (schedule, image, env, gating SpEL). Kubernetes boilerplate in field paths is compacted. Status is never conveyed by colour alone: each uses a sigil as well.

## Integration

- `.pp` files get `render_mode="pipeline"` (`diff_builder.py`). The payload is computed at generation time, from both refs, inside the container.
- The server returns `render_mode` and `pipeline_diff` with each file of `GET /api/reviews/{id}`.
- A `.pp` that is not a Spinnaker pipeline (for example Puppet) fails to parse as JSON with a `stages` array, so `build_pipeline_diff` returns `None` and the raw diff is shown. Parse failures (merge markers, mid-edit) fall back the same way.
- The frontend is vanilla JS with no build step; `pipeline-diff.js` loads before `review.js` and is cache-busted with `?v=`.

## Validation

`test_pipeline_diff.py` and `test_pipeline_render.js` use the synthetic fixtures in `tests/fixtures/pipelines/`. For an end-to-end check, point `/review-ui` at a branch that edits a `.pp` and compare the rendered output with `git show`.
