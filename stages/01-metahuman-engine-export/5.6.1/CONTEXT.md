# Stage 01 / UE 5.6.1 — MetaHuman → FBX + Textures

## Precondition

The character must be **assembled** in UE before this stage runs. If the character was
created with the in-engine 5.6 MetaHuman Creator, hit **Assembly → UE Optimized → Assemble**
once in the editor. Legacy web-MHC characters (downloaded via Quixel Bridge) are already
assembled by definition.

The editor must be **closed** on the project when this stage runs (UE commandlet locks
the project file). For debugging, the same Python script can be pasted into the editor's
Output Log — see `references/debug_in_editor.md`.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | all | UE project path, editor exe, texture format, LOD |
| Character manifest | `characters/<id>/manifest.json` | `mh_folder`, `source`, `archetype` | Which MH to export |
| Character pointer | `characters/<id>/source/README.md` | all | Human-readable context only |
| MH asset reference | `../../../skills/mh_5.6.1_asset_layout.md` | full | Where assets live inside `/Game/MetaHumans/` |
| FBX export rules | `../../../skills/fbx_export_rules.md` | full | Required FBX options + texture pass |
| Shared skeleton | `../../../skills/shared_skeleton.md` | full | Dedupe rule for skeleton + archetype body |

## Process

1. Read `_config/pipeline.yaml` and `characters/<id>/manifest.json`.
2. Invoke the launcher:
   `tools/run_export.ps1 <id>`
   which internally runs:
   `UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<abs>/tools/export_mh.py -- --char=<id> --workspace=<abs workspace root>"`
3. Launcher blocks until UE exits. Exit code must be 0.
4. Run `tools/validate_fbx.py --char=<id>` — standalone Python, no UE.
5. If validation passes, update `characters/<id>/manifest.json`:
   - `stages.01_fbx_export.status = "done"`
   - `stages.01_fbx_export.completed_at = <ISO timestamp>`
6. If anything fails, set `status = "failed"`, append to `errors[]`, leave artifacts in
   place for inspection.

## Mesh discovery (why there's more than body + face)

The MH character Blueprint (`/Game/MetaHumans/<Name>/BP_<Name>`) is the authoritative
list of skinned meshes. It references:

- **body + face** — under `/Game/MetaHumans/<Name>/Body/` and `.../Face/`
- **clothing** (top, bottom, shoes, etc.) — under shared archetype folders like
  `/Game/MetaHumans/Common/Female/Medium/NormalWeight/Tops/Shirt/`

The exporter walks `AssetRegistry.get_dependencies(BP_<Name>)` to find every
`SkeletalMesh` the BP references and exports all of them, then falls back to a
folder scan under `/Game/MetaHumans/<Name>/` for naked / BP-less cases. **Do not**
rely on a char-folder scan alone — it will silently miss clothing.

Each mesh record in `mh_manifest.json` carries a `role` field inferred from the
asset path: `face` / `body` / `top` / `bottom` / `shoes` / `hair` / etc. Stage 02
uses `role` to decide material reconstruction and merge behavior.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Skeletal mesh FBXs | `characters/<id>/01-fbx/meshes/*.fbx` | one FBX per mesh (body + face + every clothing mesh) containing **all LODs** (Blender sees them as `*_LOD0`, `*_LOD1`, …). Stage 03 emits one GLB per LOD. |
| Textures | `characters/<id>/01-fbx/textures/*.tga` | all referenced `Texture2D` assets |
| Skeleton FBX | `characters/_shared/5.6.1/skeleton/metahuman_base_skel.fbx` | written once per run, referenced from manifest |
| Export manifest | `characters/<id>/01-fbx/mh_manifest.json` | machine-readable index of everything above |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.01_fbx_export` fields |

## mh_manifest.json schema (what stage 02 reads)

```json
{
  "character_id": "ada",
  "ue_version": "5.6.1",
  "exported_at": "ISO-8601",
  "archetype": "f_med_nrw",
  "meshes": [
    {"component": "f_med_nrw_body", "role": "body", "asset_path": "/Game/...", "fbx_path": "meshes/f_med_nrw_body.fbx", "lod_count": 4, "materials": [{"slot": "...", "material": "/Game/..."}]},
    {"component": "ada_facemesh",   "role": "face", "asset_path": "/Game/...", "fbx_path": "meshes/ada_facemesh.fbx",   "lod_count": 8, "materials": [...]},
    {"component": "f_med_nrw_top_shirt_nrm_cinematic",  "role": "top",    "asset_path": "/Game/MetaHumans/Common/Female/.../Tops/Shirt/...",    "fbx_path": "meshes/...", "lod_count": N, "materials": [...]},
    {"component": "f_med_nrw_btm_slacks_nrm_cinematic", "role": "bottom", "asset_path": "/Game/MetaHumans/Common/Female/.../Bottoms/Slacks/...", "fbx_path": "meshes/...", "lod_count": N, "materials": [...]},
    {"component": "f_med_nrw_shs_flats_cinematic",      "role": "shoes",  "asset_path": "/Game/MetaHumans/Common/Female/.../Shoes/Flats/...",     "fbx_path": "meshes/...", "lod_count": N, "materials": [...]}
  ],
  "textures": [
    {"asset_path": "/Game/...", "file_path": "textures/T_....tga", "material": "MI_...", "slot": "BaseColor"}
  ],
  "skeleton": {"asset_path": "/Game/...", "fbx_path": "../../_shared/5.6.1/skeleton/metahuman_base_skel.fbx"},
  "groom": "unsupported_v1",
  "warnings": []
}
```

## Idempotency

Re-running is safe. The launcher unconditionally overwrites `characters/<id>/01-fbx/`
and the shared skeleton. It does not read prior manifest status — the dispatcher decides
whether to skip; this stage just runs when invoked.

## Failure modes (known)

- `/Game/MetaHumans/<Name>/` missing → character not assembled. Fail with actionable msg.
- UE editor running → project locked. Fail early, ask user to close editor.
- Only body + face exported, no clothing → BP dep walk returned nothing. Check the
  character has a Blueprint at `/Game/MetaHumans/<Name>/BP_<Name>` and it references
  the outfit SkeletalMesh assets. Folder scan alone is a silent fail mode.
- Groom asset encountered → warn, skip, record as `groom: unsupported_v1`.
