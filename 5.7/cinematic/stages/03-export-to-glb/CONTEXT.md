# Stage 03 — Blender → GLB

Headless Blender stage. Consumes stage 02's `.blend`; exports a single
web-ready GLB containing LOD0 meshes + one armature + embedded textures,
compressed with Draco.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `blender_exe`, `glb_constraints` | Blender path + web limits |
| Character manifest | `characters/<id>/manifest.json` | `character_id` | Which character |
| Stage 02 blend | `characters/<id>/02-blend/<id>.blend` | full scene | Source geometry + materials |
| Stage 02 scene manifest | `characters/<id>/02-blend/blend_manifest.json` | `scene`, `hidden_non_lod0` | Determines which meshes are LOD0 (exportable) |

## Process

1. Read `_config/pipeline.yaml` → `blender_exe` + `glb_constraints`.
2. Invoke `tools/run_export.ps1 -Char <id>`. It runs:
   `blender --background <blend> --python tools/export_glb.py -- --char <id> --workspace <abs>`
3. `export_glb.py`:
   - Opens `<id>.blend`.
   - Deletes hidden (non-LOD0) meshes so only LOD0 geometry ships.
   - Flips the G channel on every normal-map image in place (UE authors
     normals in DirectX convention with +Y down; glTF 2.0 mandates OpenGL
     convention with +Y up). Matched by filename against the same hints
     stage 02 uses to classify normals.
   - Downsamples any image texture whose largest dimension exceeds
     `glb_constraints.max_texture_px` (default 2048), in place.
   - Optionally joins the per-FBX armatures into one (v1: kept separate;
     armature merge is a stage-02 concern, not done yet).
   - Runs `bpy.ops.export_scene.gltf` with:
     - `export_format='GLB'`
     - `export_draco_mesh_compression_enable=True` (if `draco_compression: true`)
     - `export_apply=True` (applies modifiers; freezes the pose at rest)
     - `use_visible=True` (skips anything we've hidden)
     - `export_image_format='AUTO'` (keeps PNG/JPEG per source)
   - Writes `<id>.glb` + `glb_manifest.json`.
4. Launcher blocks until Blender exits 0.
5. Update `characters/<id>/manifest.json`:
   - `stages.03_glb_export.status = "done"` on success, `"failed"` with errors otherwise.
   - `stages.03_glb_export.completed_at = <ISO timestamp>`.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Web-ready GLB | `characters/<id>/03-glb/<id>.glb` | Single file, Draco-compressed, textures embedded |
| Export manifest | `characters/<id>/03-glb/glb_manifest.json` | `file_size_bytes`, `tri_count`, `mesh_count`, `material_count`, `image_count`, `max_texture_px_used` |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.03_glb_export` fields |

## glb_manifest.json schema

```json
{
  "character_id": "ada",
  "glb_path": "03-glb/ada.glb",
  "file_size_bytes": 12345678,
  "tri_count": 58000,
  "mesh_count": 5,
  "material_count": 22,
  "image_count": 18,
  "max_texture_px_used": 2048,
  "normal_maps_g_flipped": 6,
  "draco": true,
  "tri_budget": 60000,
  "over_budget": false,
  "exported_meshes": ["Ada_FaceMesh_LOD0", "f_med_nrw_body_LOD0", "..."]
}
```

## Idempotency

Re-running is safe. The launcher unconditionally overwrites `<id>.glb`
and `glb_manifest.json`. It does not mutate the source `.blend` on disk —
the script operates on the in-memory scene after opening.

## Known current behavior (v1)

- **Armatures kept separate**: one armature per source FBX. Most GLB
  viewers handle this fine; stage 02 armature-merge is TODO.
- **No decimation**: if `tri_count > target_tri_budget`, `over_budget: true`
  is flagged in the manifest but nothing is decimated. Decimation is a
  stage-02 concern (you want to review the loss visually).
- **Textures embedded**: GLB has all images packed inline. Consider
  external `.gltf + .bin + images/` for large characters if hosting cares.
- **Groom hair skipped**: `glb_constraints.skip_groom: true` — card hair
  from stage 02 is still exported (mesh), but UE groom curves never
  entered the pipeline.

## Failure modes (known)

- `<id>.blend` missing → stage 02 hasn't run. Ask operator to run stage 02 first.
- Blender exe path wrong → launcher fails with "file not found". Fix config.
- Draco export fails ("Draco library unavailable") → Blender install missing
  the Draco shared lib. Retry with `draco_compression: false` in config.
