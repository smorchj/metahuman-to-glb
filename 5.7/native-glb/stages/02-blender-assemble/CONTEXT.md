# Stage 02 / 5.7 native-glb — Blender Assemble + ARKit Bake

Headless Blender stage. Imports the per-mesh GLBs from stage 01, bakes
51 ARKit shape keys onto the face mesh from the Sequencer-bake LSE FBX,
propagates those shape keys onto facial-groom card meshes (eyebrows /
mustache / beard), wires up hair-card materials from sidecar textures,
and saves a `.blend` for stage 03.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `blender_exe` | Blender executable path |
| Character manifest | `characters/<id>/manifest.json` | `stages.01_unreal_glb_export.status` | Verify stage 01 done |
| Stage 01 manifest | `characters/<id>/01-glb/mh_manifest.json` | `assets[]`, `arkit_sources`, `sidecar_textures`, `groom_materials` | What to import + ARKit source paths + groom MI params |
| Stage 01 GLBs | `characters/<id>/01-glb/*.glb` | all | Per-mesh geometry + textures (no morphs in this pass) |
| Stage 01 LSE FBX | `characters/<id>/01-glb/LS_arkit_full.fbx` | full | Mesh + skeleton + per-pose bone keyframes — the ARKit shape-key source |
| Stage 01 pose list | `characters/<id>/01-glb/arkit_pose_names.json` | full | Maps frame N → ARKit pose name |
| Stage 01 textures | `characters/<id>/01-glb/textures/*.png` | hair / brow / lash atlases | Wired onto reconstructed groom materials |

## Process

1. Read `_config/pipeline.yaml` to learn `blender_exe`.
2. Invoke launcher: `tools/run_assemble.ps1 -Char <id>`. It resolves
   `blender_exe` from `_config/pipeline.yaml` and runs:
   ```
   <blender_exe> --background --python <abs>/tools/import_glb.py -- --char <id> --workspace <abs pipeline root>
   ```
3. Launcher blocks until Blender exits. Exit code must be 0.
4. The script does (in order):
   - Import every GLB listed in `mh_manifest.assets[]` into a clean scene.
   - Hide non-LOD0 / collision meshes.
   - Bake 51 ARKit shape keys onto the face mesh via
     `_bake_arkit_from_lse_fbx`: import LSE FBX, scrub frame N for pose N
     (1:1 mapping at 24fps bake), capture deformed mesh via
     `evaluated_get(depsgraph)`, transfer to the GLB face by kdtree
     position match (max=0.00mm; same UE SKM, same topology, perfect
     vertex correspondence).
   - Propagate the face's ARKit shape keys onto groom card meshes
     (`Eyebrows_*`, `Mustache_*`, `Beard_*`, `Stubble_*`, …) via k=4
     inverse-distance² weighting in world space (see
     `_apply_arkit_to_grooms`). Eyelashes are NOT a separate mesh — they
     live as a material slot on the face mesh and inherit shape keys
     for free.
   - Reconstruct hair-card / eyebrow / lash materials using sidecar
     atlases + groom MI params (`_wire_card_materials`).
   - Tune invisible MH face slots (eyeShell, M_Hide, lacrimal, saliva)
     to fully transparent Principled BSDF so they don't paint over
     irises.
   - Parent hair-card StaticMeshes to the face armature's head bone so
     they track head motion.
   - Emit `mh_materials.json` (used by stage 04 viewer for hair / lash
     shader injection).
5. Verify outputs (Outputs table). Required: `<id>.blend`, `mh_materials.json`,
   `blend_manifest.json`.
6. Update `characters/<id>/manifest.json`:
   - `stages.02_blender_assemble.status = "done"` on success
   - `stages.02_blender_assemble.completed_at = <ISO timestamp>`
7. On failure: `status = "failed"`, append actionable message to
   `errors[]`, leave artifacts in place.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Assembled blend | `characters/<id>/02-blend/<id>.blend` | All meshes imported; non-LOD0 hidden; face mesh + groom cards have 51 ARKit shape keys; hair-card materials reconstructed from sidecar atlases. |
| Material spec | `characters/<id>/02-blend/mh_materials.json` | Schema matches 5.6/cinematic so stage 04's viewer.js handles hair / lash injection without changes. |
| Scene manifest | `characters/<id>/02-blend/blend_manifest.json` | Machine-readable summary: imported count, mesh names, armature names, hair_parented count. |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.02_blender_assemble` fields |

## Why bake ARKit shape keys here (not in UE)

Stage 01 has the LSE FBX with bone keyframes that resolve RigLogic.
This stage replays that animation in Blender, captures deformed mesh
per frame, and writes them as static morph targets on the GLB face.
That's the only path that round-trips bone-driven ARKit motion into
glTF morph targets (UE GLTFExporter can't bake animation to morphs;
FBX morph keyframes don't round-trip cleanly).

The kdtree match is `0.00mm` because the LSE FBX face mesh and the GLB
face mesh come from the same UE SkeletalMesh — same topology, same
vertex count (34657). UV-seam vertices land at identical positions
across both export formats; the kdtree just lets us map between the two
exporters' arbitrary vertex orderings without assuming index equality.

## Why the groom prop is k=4 inverse-distance²

Ported from 5.6/cinematic apply_arkit52_grooms.py. k=1 (nearest vert)
pops at seams; k=8 over-smooths and kills corner definition. With
inverse-distance² weights, a groom vert sitting on a face vert (d≈0)
gets ~100% of that face vert's delta; a vert drifting between two face
verts gets a clean blend.

Eyebrows_* card mesh has ~6429 verts sitting ~1.6mm avg from the face
surface. Real ARKit shapes activate ~46 of the 51 keys on the eyebrow
card (5 are skipped because all-zero in the brow region — jawForward,
mouthFunnel, etc.).

## Idempotency

Unconditional overwrite of `characters/<id>/02-blend/`. No prior-state
read. Re-running is safe.

## Failure modes (known)

- `mh_manifest.json` missing → stage 01 hasn't run. Ask operator.
- LSE FBX missing → stage 01 didn't produce it. Check stage 01 logs for
  `Sequencer bake exception`.
- LSE FBX import produces no MESH → file corrupted or empty bake. Check
  file size (~46 MB expected).
- kdtree match max > 5mm → LSE face and GLB face came from different
  SKM builds. Re-run stage 01 to get a consistent pair.
