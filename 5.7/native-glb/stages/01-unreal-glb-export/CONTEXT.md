# Stage 01 / 5.7 native-glb — UE → GLB + ARKit Sequencer Bake

## Precondition

Stage 00 must be done: the character must be assembled in the 5.7 UE
project at `/Game/<id>/` with `request_auto_rigging` having run with
`rig_type=JOINTS_AND_BLEND_SHAPES`. The face SkeletalMesh must carry
the 858 raw RigLogic morph targets.

The editor must be **closed** on the project when this stage runs (UE
commandlet locks the project file).

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `ue_by_version.5.7`, `glb_constraints` | UE project + editor exe paths, max texture size |
| Character manifest | `characters/<id>/manifest.json` | `character_id`, `output_name` (or fallback to `<id>`) | Identify the character + UE folder. DO NOT read or modify other stages' status fields. |
| Character source | `characters/<id>/source/README.md` | all | Human-readable context only |
| Engine asset | `/MetaHumanCharacter/Face/ARKit/AS_MetaHuman_ARKit_Mapping` | full | Source AnimSequence (24fps, 66 keyframes, 1-per-pose) — the curve-driven ARKit mapping |
| Engine asset | `/MetaHumanCharacter/Face/ARKit/PA_MetaHuman_ARKit_Mapping` | pose names | PoseAsset whose names define the ARKit pose order Stage 02 expects |

## Preconditions (UE-side, not manifest)

Verify the UE project has `/Game/<output_name>/` containing assembled
MetaHuman assets (face + body SkeletalMesh, hair-card StaticMeshes).
This is what stage 00 produces. Do **not** read the manifest's
`stages.00_unreal_assemble.status` field — it can be stale; the
project state on disk is ground truth.

If `/Game/<output_name>/` is missing, abort with an actionable message
("UE assets not found at /Game/<output_name>/ — run stage 00 first").
Do not "fix" any manifest field.

## Process

1. Read `_config/pipeline.yaml` and `characters/<id>/manifest.json`
   (only to learn the character id / output_name — do not read or write
   other stages' status).
2. Verify the precondition above (UE project has `/Game/<output_name>/`).
3. Invoke launcher: `tools/run_export.ps1 -Char <id>`. It runs:
   `UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="C:/tmp/mh/export_glb.py -- --char=<id>" -AllowCommandletRendering -unattended -nosplash`
   `-AllowCommandletRendering` is mandatory: the GLTFExporter
   `USE_MESH_DATA` material bake AND the Sequencer FBX export both need
   a rendering subsystem. Default headless mode silently produces empty
   bakes / crashes on `MeshObject` assertion.
4. Launcher blocks until UE exits. Exit code must be 0.
5. Verify outputs (Outputs table). Required: every `*.glb` plus the
   ARKit sources (`LS_arkit_full.fbx`, `arkit_pose_names.json`,
   `arkit_pose_curves.json`, `mh_manifest.json`).
6. If validation passes, update `characters/<id>/manifest.json` —
   **only** the `stages.01_unreal_glb_export` block. Do not read,
   modify, or "fix" any other stage's status, timestamps, or errors.
   The dispatcher (Opus) owns those fields:
   - `stages.01_unreal_glb_export.status = "done"`
   - `stages.01_unreal_glb_export.started_at = <ISO timestamp at launch>`
   - `stages.01_unreal_glb_export.completed_at = <ISO timestamp at finish>`
   - `stages.01_unreal_glb_export.errors = []`
7. On failure: `stages.01_unreal_glb_export.status = "failed"`, append
   actionable message to `stages.01_unreal_glb_export.errors[]`, leave
   artifacts in place. Touch only this stage's block.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Per-mesh GLBs | `characters/<id>/01-glb/*.glb` | One per SkeletalMesh / hair-card StaticMesh under `/Game/<id>/`. Materials baked via `USE_MESH_DATA`; morphs disabled in this pass (replaced by the Sequencer LSE FBX below). |
| Side-car textures | `characters/<id>/01-glb/textures/*.png` | Hair-card, eyebrow, eyelash atlas PNGs — pulled directly from `/Game/<id>/Grooms/Textures/`. Stage 02 wires them onto reconstructed materials since GLTFExporter can't translate MH's hair-card shader. |
| Face SKM FBX | `characters/<id>/01-glb/SKM_<id>_FaceMesh.fbx` | Face mesh + 858 named raw RigLogic morphs. Currently kept around for debugging; stage 02 doesn't consume it on the LSE-bake path. |
| AnimSequence FBX | `characters/<id>/01-glb/AS_MetaHuman_ARKit_Mapping.fbx` | Empty-action FBX kept for debugging only. Curve-driven bones don't round-trip through this path. |
| Sequencer-bake FBX | `characters/<id>/01-glb/LS_arkit_full.fbx` | **The ARKit-shape source.** Built by spawning a transient SkeletalMeshActor + Level Sequence at 24fps display rate with the AS track, then `SequencerTools.export_level_sequence_fbx`. Contains mesh + skeleton + per-frame bone keyframes for all 66 poses with RigLogic / correctives / bone resolution applied natively. |
| Pose name list | `characters/<id>/01-glb/arkit_pose_names.json` | Ordered list of 66 pose names from `PA_MetaHuman_ARKit_Mapping`. Frame N in the LSE FBX = pose N exactly (24fps bake matches source rate). |
| Per-pose curve dump | `characters/<id>/01-glb/arkit_pose_curves.json` | Per-pose dict of `{curve_name: weight}` from `AnimSequence.get_anim_pose_at_frame()`. Reference data; stage 02 doesn't consume on the LSE-bake path but it's documented for future per-pose morph-weight injection. |
| Per-pose bone dump | `characters/<id>/01-glb/arkit_pose_bones.json` | Per-pose dict of `{bone_name: {loc/rot/scale}}` from `AnimPose.get_relative_to_ref_pose_transform()`. Empty for curve-driven AS (RigLogic isn't run by AnimPose alone) — kept for completeness. |
| MH export manifest | `characters/<id>/01-glb/mh_manifest.json` | Machine-readable index of every artifact above. `arkit_sources` block tells stage 02 which file holds which thing. |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.01_unreal_glb_export` fields |

## Why a Sequencer bake

UE Python exposes `AnimPose.get_curve_weight()` and
`get_relative_to_ref_pose_transform()`, but neither runs RigLogic. For a
curve-driven AnimSequence (which is what the ARKit mapping is), the
former returns INPUT control values and the latter returns identity for
every bone. The only Python-reachable path that fires RigLogic +
correctives + bone resolution is to drive a live SkeletalMeshComponent
through Sequencer and bake the result. We do that with a transient Level
Sequence + `SequencerTools.export_level_sequence_fbx`.

The 24fps display rate is mandatory: the source AS plays at 24fps with
1 frame per pose. If the level sequence stayed at the default 30fps,
the bake would interpolate between adjacent ARKit poses and stage 02
would capture blends (e.g. browDownLeft would carry residual
mouthUpperUpRight motion).

## Idempotency

Re-running unconditionally overwrites `characters/<id>/01-glb/`. The
transient `/Game/Temp/LS_arkit_export` Level Sequence asset gets left in
the project (we don't delete it because UE crashes when deleting
referenced assets). It's harmless and overwrites cleanly on next run.

## Failure modes (known)

- `/Game/<output_name>/` missing → stage 00 not done. Fail with actionable msg.
- UE editor running → project locked. Fail early, ask user to close editor.
- `LS_arkit_full.fbx` not produced → check log for `MeshObject`
  assertion. Means `-AllowCommandletRendering` was missing on the UE
  invocation. The launcher always passes it; only fails if someone
  invoked UE directly without it.
- LSE FBX too small (< 5 MB) → Sequencer bake produced empty bones.
  Likely means the Sequencer bind couldn't find the actor — check the
  spawn path in `_export_sequencer_bake`.
