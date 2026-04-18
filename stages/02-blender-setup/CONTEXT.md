# Stage 02 — Blender Setup

Headless Blender stage. Consumes stage 01's FBXs + manifest; imports them into a clean
scene, wires up minimal PBR materials on LOD0, and saves a `.blend`. Armature merge
and clothing fit-up are planned but not implemented yet.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `blender_exe` | Blender executable path |
| Character manifest | `characters/<id>/manifest.json` | `character_id`, `ue_version` | Which character |
| Stage 01 manifest | `characters/<id>/01-fbx/mh_manifest.json` | `meshes[]`, `textures[]` | FBX file list + role tags + MI param blocks |
| Stage 01 FBXs | `characters/<id>/01-fbx/meshes/*.fbx` | all | The geometry to import |
| Material ref | `references/mh_material_reconstruction.md` | full | MH material-family decoding rules + MI param/texture conventions. Consult before editing `tools/import_fbx.py`'s material builders or adding a character whose outfit isn't covered by the current cloth classification table. |

## Process

1. Read `_config/pipeline.yaml` to learn `blender_exe`.
2. Invoke the launcher: `tools/run_setup.ps1 -Char <id>`. It resolves `blender_exe`
   from the config and runs:
   `blender --background --python tools/import_fbx.py -- --char <id> --workspace <abs>`
3. Launcher blocks until Blender exits. Exit code must be 0.
4. Verify outputs exist (see Outputs table). **No standalone validator script yet**
   — verification is: exit 0 + both output files present + imported mesh count
   matches `len(mh_manifest.meshes)` in `blend_manifest.json`.
5. Update `characters/<id>/manifest.json`:
   - `stages.02_blender_setup.status = "done"` on success, `"failed"` with errors otherwise.
   - `stages.02_blender_setup.completed_at = <ISO timestamp>`.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Assembled blend file | `characters/<id>/02-blend/<id>.blend` | All mesh FBXs imported; non-LOD0 meshes hidden (viewport + render); one armature per imported FBX (not merged yet). |
| Scene manifest | `characters/<id>/02-blend/blend_manifest.json` | Machine-readable scene summary |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.02_blender_setup` fields |

## blend_manifest.json schema

```json
{
  "character_id": "ada",
  "blend_path": "02-blend/ada.blend",
  "imported": [
    {"component": "ada_facemesh", "fbx": "meshes/ada_facemesh.fbx", "lod_count_declared": 8}
  ],
  "scene": {
    "object_count": 39,
    "mesh_count": 24,
    "armature_count": 5,
    "mesh_names": ["Ada_FaceMesh_LOD0", "..."],
    "armature_names": ["root", "root.001", "..."]
  },
  "hidden_non_lod0": ["Ada_FaceMesh_LOD1", "..."],
  "materials_applied": [
    {"mesh": "f_med_nrw_body_LOD0", "slot_index": 0, "slot": "MI_BodySynthesized",
     "material_name": "f_med_nrw_body_MI_BodySynthesized",
     "material_source": "/Game/MetaHumans/Ada/Materials/MI_BodySynthesized.MI_BodySynthesized",
     "textures": {"basecolor": "BodyBaseColor.tga", "normal": "female_body_normal_map.tga",
                  "roughness": "female_body_roughness_map.tga", "ao": "female_body_cavity_map.tga"}}
  ]
}
```

## Idempotency

Re-running is safe. The launcher unconditionally overwrites
`characters/<id>/02-blend/<id>.blend` and `blend_manifest.json`. It does not read
prior manifest status — the dispatcher decides whether to skip; this stage just
runs when invoked. `run_setup.ps1` also renders a diagnostic preview PNG by default;
pass `-SkipPreview` to skip it (preview is not a required Output).

## Known current behavior (v0)

- **All LODs imported as sibling meshes** named `<Component>_LOD<N>`. Non-LOD0 are
  hidden in viewport + render; geometry still lives in the file. Stage 03 will pick
  per-LOD subsets when exporting per-LOD GLBs.
- **One armature per FBX** — every MH skeletal mesh FBX embeds its own copy of the
  skeleton, so Blender imports `root`, `root.001`, `root.002`, … These must be
  deduplicated / merged before stage 03; not done in v0.
- **Materials (LOD0 only)**: for each LOD0 mesh, each material slot is rebuilt as a
  Principled BSDF. Textures are grouped by `mh_manifest.textures[].material` (the
  material path, not by filename guessing on the mesh side) and classified by
  filename:
    - `*BaseColor*` / `*Color_MAIN*` / `*color_map*` / `*_diffuse*` → Base Color
    - `*_N.tga` / `*Normal_MAIN*` / `*_normal_map*` (excluding `_WM1/2/3` wrinkle
      maps and `_LOD`/`_LOD1` face variants) → Normal (via Normal Map node)
    - `*Roughness*` (non-LOD) → Roughness
    - `*_AO.tga` / `*Cavity*` → recorded but not hooked up (reserved for stage 03 ORM)
  Slots whose material has no mapped textures (some face sub-materials like eyes /
  teeth / saliva — they use parametric shaders in UE) get a blank Principled BSDF.
- **Clothing included**: stage 01 exports 5 mesh FBXs for Ada — body, face, shirt,
  slacks, flats. All 5 must appear in the `.blend` after import. If only body + face
  appear, stage 01's BP dependency walk failed silently — re-check stage 01 outputs.

## Failure modes (known)

- `mh_manifest.json` missing → stage 01 hasn't run. Ask operator to run stage 01 first.
- Blender exe path in config wrong → launcher fails with "file not found". Fix config.
- FBX path in manifest doesn't exist on disk → stage 01 output was deleted/moved. Re-run 01.
