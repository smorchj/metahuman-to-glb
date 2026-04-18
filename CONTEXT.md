# CONTEXT.md — Pipeline Task Routing (Layer 1)

## Goal

Turn a UE MetaHuman into a web-ready GLB. Pipeline is character-indexed and stage-ordered.

## Stages (execution order)

| # | Folder | Input | Output |
|---|---|---|---|
| 01 | `stages/01-metahuman-engine-export/<ue-version>/` | Assembled MH in UE project | `characters/<id>/01-fbx/` + `mh_manifest.json` |
| 02 | `stages/02-blender-setup/` | `01-fbx/` | `characters/<id>/02-blend/<id>.blend` |
| 03 | `stages/03-export-to-glb/` | `02-blend/<id>.blend` | `characters/<id>/03-glb/<id>.glb` |
| 04 | `stages/04-webview-build/` | `03-glb/<id>.glb` | `docs/characters/<id>/` (GitHub Pages) |

Stage 01 is **version-routed**: subfolder matches `ue_version` in `_config/pipeline.yaml`.
Stages 02 and 03 are source-agnostic — they read only `01-fbx/` + `mh_manifest.json`.
Stage 04 is pure-Python (no Blender / UE) — builds a static site under `docs/`.

## Dispatch rules

Given a character id `<id>`:

1. Read `characters/<id>/manifest.json` → find first stage with `status != "done"`.
2. Read `_config/pipeline.yaml` → resolve UE version for stage 01 routing.
3. Load **only** the target stage's `CONTEXT.md` + files it names in Inputs.
4. Run the stage's launcher script (one per stage, named in the stage CONTEXT.md).
5. Validate outputs against the stage's Outputs table.
6. Update `characters/<id>/manifest.json` for that stage.
7. If the next stage exists, loop.

## Operator intents (natural language → dispatch)

Operators write short asks. Map them to the dispatch rules above — do not ask for
clarification unless the intent is genuinely ambiguous.

| Operator says | Do |
|---|---|
| "export `<id>`", "process `<id>`", "run `<id>`" | Apply dispatch rules — first non-done stage, then loop |
| "re-export `<id>`", "redo `<id>` from scratch" | Reset all stages in `manifest.json` to `pending`, then dispatch |
| "redo stage `<N>` for `<id>`", "re-run 02 on `<id>`" | Reset stage `<N>` and everything after it to `pending`, then dispatch |
| "status of `<id>`", "where is `<id>`" | Read `characters/<id>/manifest.json` and report done/pending/failed per stage |
| "add character `<id>`" | Copy `characters/_template/` → `characters/<id>/`, ask operator only for `mh_folder` |
| no `<id>` given | Use `_config/pipeline.yaml → active_character` |

## Character registry

Each character is a folder under `characters/`. Required shape:

```
characters/<id>/
  manifest.json            # single source of truth for per-character status
  source/README.md         # pointer to UE project + MH folder name
  01-fbx/                  # stage 01 output (meshes/, textures/, mh_manifest.json)
  02-blend/                # stage 02 output
  03-glb/                  # stage 03 output
```

Stage 04 writes to the workspace-global `docs/` folder (GitHub Pages root),
not per-character.

Copy `characters/_template/` to add a new character.

## Haiku spawn prompt (reference)

When a higher-level agent dispatches Haiku for one stage, use this prompt shape
**verbatim** — do not pad it. Extra guardrails ("read these files in this order",
"report ambiguities", "don't read tools/") belong in the stage's CONTEXT.md so every
invocation inherits them, not in the per-run prompt.

  You are running stage <NN> for character <id>.
  Read <stage CONTEXT.md> for the contract.
  Read characters/<id>/manifest.json for current state.
  Your tools are in <stage>/tools/ only.
  Do not load other stages.
  Execute the Process. Verify Outputs. Update manifest.json. Report.

## Active config

See `_config/pipeline.yaml` for:
- `ue_project_path` — absolute path to the .uproject
- `ue_editor_cmd` — path to UnrealEditor-Cmd.exe
- `ue_version` — active UE version (routes stage 01)
- `blender_exe` — path to blender.exe for stages 02/03
- `active_character` — default character id for single-char runs
