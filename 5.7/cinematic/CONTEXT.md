# CONTEXT.md — 5.7 Cinematic Pipeline

Fully-isolated pipeline for UE 5.7, cinematic build profile.

## Stages (execution order)

| # | Folder | Input | Output |
|---|---|---|---|
| 01 | `stages/01-metahuman-engine-export/` | Assembled MH in UE project (5.7) | `characters/<id>/01-fbx/` + `mh_manifest.json` |
| 02 | `stages/02-blender-setup/`           | `01-fbx/` | `characters/<id>/02-blend/<id>.blend` |
| 03 | `stages/03-export-to-glb/`           | `02-blend/<id>.blend` | `characters/<id>/03-glb/<id>.glb` |
| 04 | `stages/04-webview-build/`           | `03-glb/<id>.glb` | `<workspace>/docs/characters/<id>/` |

## Boundary

All paths in this pipeline resolve relative to this folder (`5.7/cinematic`). Never reach into:
- Other pipeline roots (e.g. 5.6/cinematic from within 5.7/optimized)
- Workspace-global `characters/` (does not exist — per-pipeline only)

Stage 04 is the one exception: it writes into the workspace-global `docs/characters/`
for GitHub Pages. Treat that write as the pipeline's only publish point.

## Pipeline-specific notes

- UE 5.7 project required. Config: `_config/pipeline.yaml → ue_by_version.5.7`.
- Stage 01 drives `build_meta_human` with `MetaHumanDefaultPipelineType.CINEMATIC`:
  full-quality mesh wrinkles, higher LODs, heavier output. Higher memory (~8 GB
  peak during build — may OOM on low-memory systems; fall back to Optimized if so).
- Same texture naming conventions as 5.7 Optimized.
