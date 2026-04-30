# CONTEXT.md — 5.6 Cinematic Pipeline

Fully-isolated pipeline for UE 5.6, cinematic build profile.

## Stages (execution order)

| # | Folder | Input | Output |
|---|---|---|---|
| 01 | `stages/01-metahuman-engine-export/` | Assembled MH in UE project (5.6) | `characters/<id>/01-fbx/` + `mh_manifest.json` |
| 02 | `stages/02-blender-setup/`           | `01-fbx/` | `characters/<id>/02-blend/<id>.blend` |
| 03 | `stages/03-export-to-glb/`           | `02-blend/<id>.blend` | `characters/<id>/03-glb/<id>.glb` |
| 04 | `stages/04-webview-build/`           | `03-glb/<id>.glb` | `<workspace>/docs/characters/<id>/` |

## Boundary

All paths in this pipeline resolve relative to this folder (`5.6/cinematic`). Never reach into:
- Other pipeline roots (e.g. 5.6/cinematic from within 5.7/native-glb)
- Workspace-global `characters/` (does not exist — per-pipeline only)

Stage 04 is the one exception: it writes into the workspace-global `docs/characters/`
for GitHub Pages. Treat that write as the pipeline's only publish point.

## Pipeline-specific notes

- UE 5.6.1 project required. Config: `_config/pipeline.yaml → ue_by_version.5.6.1`.
- Stage 01 is the original pipeline — walks `BP_<Name>` dependency tree.
- Textures follow 5.6 naming (`T_Head_BaseColor`, `FaceColor_MAIN`, etc.).
