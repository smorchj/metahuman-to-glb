# Stage 00 — UE Assemble (5.7 Cinematic)

Turn an unrigged `MetaHumanCharacter` asset into an in-engine, saved
SkeletalMesh + texture tree under `/Game/<Name>/` so stage 01 can
FBX-export it.

## Scope (hard rule)

You run this stage's launcher and verify its outputs. Nothing else.

Do **not** modify pipeline code: `stages/*/tools/*.py`, `tools/*.ps1`,
`_config/`, `RUN.md`, `CLAUDE.md`. If a script throws or produces wrong
output, surface the actual error to the operator. Silently working
around it (try/except around imports, skipping failed assets, lowering
thresholds) turns a fixable bug into an invisible regression. Do not
touch other stages' contracts, code, or manifest blocks, or other
characters' artifacts.

## Precondition

- UE 5.6 project with the `MetaHumanCharacter` plugin enabled (e.g.
  `MetaHumans3.uproject`).
- A `MetaHumanCharacter` asset exists at `mh_folder` (e.g.
  `/Game/MetaHumans/Gabo.Gabo`).
- Editor must be closed (the launcher boots a GUI editor and drives it
  headlessly via `-unattended -ExecCmds`).

## Process

**Before launching UE**, the launcher extracts the Content Browser
thumbnail embedded in the MetaHumanCharacter `.uasset` file and saves
it to `characters/<id>/source/thumbnail.jpg`. This is a fast standalone
Python step (no editor needed). The thumbnail lets the operator verify
the correct character before committing to the expensive build cycle.

The launcher then runs `tools/build_metahuman.py` inside the editor's
Python environment. The script:

1. `try_add_object_to_edit(character)`
2. If `is_auto_rigged` is False → `request_auto_rigging(character)` (Epic
   cloud call, ~8 seconds; uses the editor's active Epic login).
3. After a short grace → `request_texture_sources(character)` (downloads
   high-res body + face textures from Epic cloud).
4. Tick loop: wait for BOTH `can_build_meta_human(character)` AND
   `has_high_resolution_textures` to flip true. Gating only on
   `can_build` produces 1024 atlases baked from 256-res preview
   thumbnails — labelled at the right pixel count but visibly upsampled.
   120 s escape hatch if `has_high_resolution_textures` never flips.
5. `build_meta_human(character, params)` with
   `pipeline_type = Cinematic` (UE 5.6 MH plugin only exposes Cinematic
   + DCC; Optimized landed in 5.7).
6. Save `/Game/<Name>/` so the resulting SkeletalMesh assets persist
   on disk for stage 01.
7. Reference screenshot: spawn the assembled face / body / outfits /
   groom-card actors in the editor world, point the perspective
   viewport at the head, fire `HighResShot 1024x1024`, and copy the
   resulting PNG to `characters/<id>/source/reference.png`. This is
   UE's own render of the character — downstream stages compare their
   output against it to catch material / shader regressions (flat-gray
   hair, wrong beard saturation, missing scalp shadow, etc.). The
   screenshot is best-effort: failures log a warning but do not fail
   the stage.

All state transitions are written to the `--status` JSON file so an
external poller / orchestrator can observe progress without reading
the UE log.

## Inputs

| Source | File | Why |
|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `ue_editor_cmd`, `ue_project_path` |
| Character manifest | `characters/<id>/manifest.json` | `mh_folder`, `ue_version` |

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| SkeletalMesh assets | `<UE project>/Content/<Name>/Body\|Face\|Clothing\|Grooms/` | Saved in-engine |
| Thumbnail (pre-build) | `characters/<id>/source/thumbnail.jpg` | JPEG extracted from the .uasset Content Browser thumbnail. Operator review gate before the build. |
| Reference screenshot | `characters/<id>/source/reference.png` | 1024x1024 UE-rendered headshot of the assembled MH. Ground truth for downstream visual diffing. |
| Status JSON | (transient) | `C:/tmp/mh/status.json` |

Stage 01 picks up from the saved `/Game/<Name>/` content. Stage 04's
preview should visually match `source/reference.png` modulo the slight
PBR differences between UE's MH shader and three.js's procedural hair
shader injection — significant divergence (flat hair, missing scalp
shadow, wrong beard saturation) signals a regression in stages 02-04.

## Launcher

```powershell
./tools/run_assemble.ps1 -Char <id>
```

(or invoke `build_metahuman.py` directly via `-ExecCmds` if you have
a live editor running.)
