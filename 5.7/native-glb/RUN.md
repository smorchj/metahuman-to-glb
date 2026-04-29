# RUN.md — 5.7 native-glb pipeline orchestrator (Haiku entry point)

You are an orchestrator agent. The operator gave you a UE
MetaHumanCharacter asset path (or just a character id). Your job is to
turn it into a finished web-ready GLB by running this pipeline's five
stages in sequence. Each stage runs in its own sub-agent so the work is
isolated.

## Your inputs

The operator gives you one of these forms:

- A UE asset path: `/Game/Ada/MHC_Ada` (most common)
- A UE folder path: `/Game/Ada`
- Just an id: `ada` (assumes `/Game/Ada` exists in the project)

## Your job (5 steps total)

### 1. Bootstrap the character folder

Run the bootstrap script to create `characters/<id>/` from the template:

```
python <pipeline_root>/tools/bootstrap_character.py --asset <asset_path>
```

(or `--id <id>` if the operator gave just an id).

It writes `characters/<id>/manifest.json` with `character_id`,
`mh_folder`, `output_name`, and every stage set to `pending`.

If the script exits 1 ("character already exists"), ask the operator
whether to re-export. If yes, re-run with `--force`. If no, abort.

### 2. Dispatch stage 00 → 01 → 02 → 03 → 04

For each stage in order, spawn a sub-Haiku agent with the prompt below.
**Wait for each sub-agent to complete before starting the next.** Read
the manifest after each one; if `stages.<stage>.status != "done"`, abort
and report which stage failed.

Use the Agent tool with `subagent_type: general-purpose` and
`model: haiku`.

### 3. Sub-agent prompt template

Substitute `<NN>`, `<stage_dir>`, `<id>`, and `<workspace>` with concrete
values for each spawn:

```
You are running stage <NN>-<stage_name> for character `<id>` in pipeline `5.7/native-glb`.

WORKSPACE ROOT (absolute path): `<workspace>`
PIPELINE ROOT: `<workspace>/5.7/native-glb/`

You are scoped to ONE stage and ONE character.

Required reading (in order, then stop):

1. `5.7/native-glb/stages/<NN>-<stage_name>/CONTEXT.md` — the contract
2. `5.7/native-glb/characters/<id>/manifest.json` — current state

Do NOT read other stages' CONTEXT.md, other characters' manifests, the Python source under `tools/`, or any file outside `5.7/native-glb/stages/<NN>-<stage_name>/` and `5.7/native-glb/characters/<id>/`.

Execute the Process steps from CONTEXT.md exactly:
- Verify preconditions (FILE EXISTENCE, not manifest status fields)
- Use the launcher script the contract names — don't bypass it
- Verify outputs after run
- Update manifest

CRITICAL: When updating the manifest, modify ONLY the `stages.<NN>_<stage_key>` block. Do not read, modify, or "fix" any other stage's status — even if state looks inconsistent. The dispatcher owns cross-stage state.

Report under 200 words: launcher exit code, output files verified (paths + sizes), this stage's final status in the manifest, any failure modes.
```

### 4. Stage names (use exactly these)

| `<NN>` | `<stage_name>` | manifest key | timing |
|---|---|---|---|
| 00 | `unreal-assemble` | `00_unreal_assemble` | ~1-2 min (UE startup + assemble) |
| 01 | `unreal-glb-export` | `01_unreal_glb_export` | ~1-2 min (UE GLB + Sequencer bake) |
| 02 | `blender-assemble` | `02_blender_assemble` | ~60-90 s |
| 03 | `export-to-glb` | `03_glb_export` | ~30-60 s |
| 04 | `webview-build` | `04_webview_build` | ~5 s |

### 5. Final report

When all five stages report `status: "done"`, tell the operator:

- Final GLB path: `5.7/native-glb/docs/characters/<id>/<id>.glb`
- File size + tri count from `characters/<id>/03-glb/glb_manifest.json`
- Where to view it: `<file:// path or http://localhost URL>`

If any stage failed, tell the operator which stage and why (its
`errors[]` field), and stop. Do not try to skip ahead or "fix" the
manifest — escalate to the operator (or to Opus) for diagnosis.

## Things you must NOT do

- Read or modify other characters' manifests.
- Run multiple stages in parallel (they have file-system dependencies).
- Read stage CONTEXT.md files that aren't the one you're currently
  dispatching.
- Try to "fix" stale manifest state. The whole bootstrap step puts
  everything in `pending`; per-stage sub-agents only touch their own
  block.
- Modify any pipeline code (`stages/*/tools/*.py`, `*.ps1`,
  `_config/pipeline.yaml`). If something looks broken, escalate to the
  operator.
