# CONTEXT.md — 5.7 Optimized Pipeline

Fully-isolated pipeline for UE 5.7, optimized build profile.

## Stages (execution order)

| # | Folder | Input | Output |
|---|---|---|---|
| 01 | `stages/01-metahuman-engine-export/` | Assembled MH in UE project (5.7) | `characters/<id>/01-fbx/` + `mh_manifest.json` |
| 02 | `stages/02-blender-setup/`           | `01-fbx/` | `characters/<id>/02-blend/<id>.blend` |
| 03 | `stages/03-export-to-glb/`           | `02-blend/<id>.blend` | `characters/<id>/03-glb/<id>.glb` |
| 04 | `stages/04-webview-build/`           | `03-glb/<id>.glb` | `<workspace>/docs/characters/<id>/` |

## Boundary

All paths in this pipeline resolve relative to this folder (`5.7/optimized`). Never reach into:
- Other pipeline roots (e.g. 5.6/cinematic from within 5.7/optimized)
- Workspace-global `characters/` (does not exist — per-pipeline only)

Stage 04 is the one exception: it writes into the workspace-global `docs/characters/`
for GitHub Pages. Treat that write as the pipeline's only publish point.

## Pipeline-specific notes

- UE 5.7 project required. Config: `_config/pipeline.yaml → ue_by_version.5.7`.
- Stage 01 drives `build_meta_human` with `MetaHumanDefaultPipelineType.OPTIMIZED`:
  smaller LODs, wrinkles come from normal maps (not mesh). Lower memory than Cinematic.
- Textures use the 5.7 `_BC_VT` / `_N_VT` / `_SRMF_VT` / `_Scatter_VT` VT-suffix
  convention. Stage 01 renames them to 5.6-canonical names
  (`_BaseColor`, `_Normal`, `_SRMF`, `_Scatter`) on disk so stage 02's classifier
  recognizes them.
- Groom atlases live in the MetaHumanCharacter plugin's Optional content.
  Stage 01 forces an AR scan of `/MetaHumanCharacter/Optional/Grooms/GroomAssets`
  before dep walking, and duplicates plugin-mount atlases into the character's
  `/Game/<Name>/Grooms/Textures/` so they export.
- Eyelash grooms are strand-only; a 5.6 coverage atlas ships as
  `stages/01-metahuman-engine-export/references/fallbacks/eyelashes_fallback_coverage.png`
  and is used when no per-character eyelash atlas is found.
