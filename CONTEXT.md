# CONTEXT.md — Pipeline Task Routing (Layer 1)

## Goal

Turn a UE MetaHuman into a web-ready GLB. Each UE version × pipeline type is a
fully-isolated self-contained pipeline.

## Pipeline roots

| Path | What |
|---|---|
| `5.6/cinematic/` | UE 5.6 + Cinematic pipeline (the original shipped pipeline) |
| `5.7/optimized/` | UE 5.7 + Optimized pipeline (normal-map wrinkles, smaller; web-default) |
| `5.7/cinematic/` | UE 5.7 + Cinematic pipeline (mesh wrinkles, heavier; higher fidelity) |

Each pipeline root is fully self-contained:

```
<version>/<pipeline>/
  CONTEXT.md               # pipeline-scoped task routing
  stages/
    01-metahuman-engine-export/
      CONTEXT.md
      tools/
      references/
    02-blender-setup/
      CONTEXT.md
      tools/
      references/
    03-export-to-glb/
      CONTEXT.md
      tools/
    04-webview-build/
      CONTEXT.md
      tools/
      templates/
  characters/
    _template/
    _shared/
    <id>/
      manifest.json
      source/
      01-fbx/
      02-blend/
      03-glb/
```

**No cross-pipeline reach.** Stage 02 of 5.7-optimized does not import from
5.6-cinematic. Changes in one pipeline cannot affect another.

## Workspace-wide (NOT per-pipeline)

| Folder | Purpose |
|---|---|
| `_config/pipeline.yaml` | Global tool paths (blender_exe, per-version UE editor binaries) |
| `skills/` | Reference material (MH asset layout, FBX rules) that applies across pipelines |
| `docs/` | GitHub Pages output — stage 04 of each pipeline publishes its characters into `docs/characters/<id>/` |

## Dispatch rules

1. Pick the pipeline root: `<version>/<pipeline>/`.
2. Read `<pipeline>/characters/<id>/manifest.json` → find first stage with `status != "done"`.
3. Load **only** that stage's `CONTEXT.md` + files it names.
4. Run the stage's launcher script. All paths are relative to the pipeline root.
5. Validate outputs. Update the character's `manifest.json`. Loop.

## Operator intents

| Operator says | Do |
|---|---|
| "export `<id>` via 5.7 optimized" | `cd 5.7/optimized` then dispatch |
| "export `<id>`" (no pipeline) | Use `_config/pipeline.yaml → active_pipeline` |
| "re-export `<id>`" | Reset manifest, dispatch |
| "redo stage `<N>`" | Reset stage N and later to pending, dispatch |
| "status of `<id>`" | Read pipeline's `characters/<id>/manifest.json` |
| "add character `<id>`" to `<pipeline>` | `cp -r <pipeline>/characters/_template <pipeline>/characters/<id>/` |

## Haiku spawn prompt (reference)

```
You are running stage <NN> for character <id> in pipeline <version>/<pipeline>.
Read <version>/<pipeline>/stages/<NN>-*/CONTEXT.md for the contract.
Read <version>/<pipeline>/characters/<id>/manifest.json for current state.
Your tools are in <version>/<pipeline>/stages/<NN>-*/tools/ only.
Do not reach outside the pipeline root.
Execute the Process. Verify Outputs. Update manifest.json. Report.
```

## Active config

`_config/pipeline.yaml`:
- `active_pipeline` — default `<version>/<pipeline>` for single-char runs
- `active_character` — default character id
- `blender_exe` — path to blender.exe
- `ue_by_version` — per-version UE project + editor paths (`5.6.1`, `5.7`)
