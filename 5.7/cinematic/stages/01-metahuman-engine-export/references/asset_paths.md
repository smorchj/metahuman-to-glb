# Asset Paths — UE 5.7 (WIP — run `tools/run_probe.ps1 <id>` to fill this in)

## What we know

- MH 5.7 collapses the per-character assembly into a single
  `MetaHumanCharacter` asset instead of the 5.6-style
  `BP_<Name>` Blueprint + per-part skeletal meshes.
- The `MetaHumanCharacter` plugin owns the asset type and exposes
  subsystem APIs for reading the member meshes.

## What we need the probe to confirm

1. **Character asset path.** Where is the `MetaHumanCharacter` asset for a
   newly-assembled 5.7 character? Likely somewhere under
   `/Game/MetaHumans/` or `/Game/Characters/` — probe to confirm.
2. **Member mesh enumeration.** Which Python call on the
   `MetaHumanCharacter` asset returns the list of SkeletalMesh components
   (face, body, top, bottom, shoes, etc.)? Candidates:
   - `character.get_mesh_components()`
   - `character.get_face_mesh()` / `character.get_body_mesh()` etc.
   - A subsystem call on `MetaHumanCharacterEditorSubsystem`.
3. **Shared / common asset location.** Where do the shared archetype
   bodies live in 5.7? 5.6 had them under
   `/Game/MetaHumans/Common/Female/Medium/NormalWeight/Body/`. That path
   may have moved or collapsed.
4. **Simplified face textures.** The `Face/Textures/Simplified/` folder
   used by MH cinematic materials may have moved.
5. **Material graph access.** `unreal.MaterialEditingLibrary.get_used_textures`
   should still work for `MaterialInstanceDynamic`/`MaterialInstanceConstant`;
   confirm by probing a 5.7 MH.

## How to fill this in

Run:

```powershell
.\tools\run_probe.ps1 <id>
```

The probe script should be edited to walk from a known `MetaHumanCharacter`
asset and dump:
- Its class chain + editor-exposed properties.
- Any referenced SkeletalMesh paths and their parent-folder structure.
- Any referenced textures and their parent-folder structure.
- Any referenced materials and what `MaterialEditingLibrary` returns for them.

Copy the output into this file under `## Actual 5.7 paths (observed)` as
the probe reveals them, so the next pair of eyes (human or LLM) does not
have to re-derive.

## Actual 5.7 paths (observed)

### `MetaHumanCharacterEditorBuildParameters` Python attrs

Enumerated live on UE 5.7.4:

```
absolute_build_path, animation_system_name, bake_makeup, common_folder_path,
enable_wardrobe_item_validation, export_zip_file, name_override,
pipeline_quality, pipeline_type
```

### Pipeline types (`unreal.MetaHumanDefaultPipelineType`)

- `Cinematic` (0) — default. Builds in-engine `SkeletalMesh` assets into `/Game/<Name>/` (crashed in our first attempt with "Attempting to replace an object that hasn't been fully loaded: BP_Kleb" — needs `/Game/<Name>/` cleaned before retry).
- `Optimized` (1) — game-ready LOD-reduced in-engine build. Also produces SkeletalMesh assets. Likely safer for web/GLB target.
- `UEFN` (2) — Fortnite profile.
- `DCC` (3) — Maya/zip export. **Produces no FBX** (see below).

### DCC pipeline output structure — `Kleb.zip` (166 MB, 74 files)

Confirmed by running `build_meta_human(character, params)` with
`pipeline_type=DCC`, `export_zip_file=True`, `absolute_build_path=C:/tmp/mh/out`,
`name_override=Kleb`.

```
head.dna                                (61 MB, rig + geometry binary)
body.dna                                (4.6 MB, rig + geometry binary)
Maps/*.png                              (26 textures: body, chest, head basecolor/normal/cavity,
                                         eyes, teeth, eyelashes, animated CM/WM maps)
SourceAssets/maps/                      (skin LUT, jitter, irradiance DDS)
SourceAssets/masks/*.tga                (34 facial animation masks, 256x256 each)
SourceAssets/shaders/*.fx               (5 Maya DX11 shaders)
ExportManifest.json                     (metaHumanName, engine version, timestamp only)
Kleb.png                                (thumbnail)
```

**Critical finding**: the DCC export has **no FBX / no explicit mesh**. Face + body geometry
are baked inside the `.dna` files and reconstructed by the Maya plugin at import time.
For a Blender → GLB pipeline, DCC alone is not enough — we need either:

1. A separate run with `pipeline_type=Cinematic` or `Optimized` that writes `SkeletalMesh`
   assets into `/Game/<Name>/`, which we then FBX-export using the same `export_mh.py`
   surface as 5.6.
2. Or a DNA → mesh extraction step (`dna_calibration` / RigLogic CLI) that decodes
   `head.dna` + `body.dna` into OBJ/FBX directly. Epic's MetaHuman DNA Calibration
   repo on GitHub has Python bindings.

### State machine timing (Kleb smoke test, 5.7.4)

```
STARTING    -> REQUESTED   ~1.9 s  (try_add_object_to_edit + request_texture_sources)
REQUESTED   -> WAITING     ~3.7 s  (first tick logs high_res=False, face_tex=9)
high_res flips True        +11.3 s (body texture download done)
can_build flips True       +10.6 s (face + sync LODs settle)
BUILDING    -> DONE        +55.5 s (DCC zip pack + write)

Total wall-clock from STARTING to DONE: ~3m 3s (includes ~2m UE boot).
```

### Gotchas confirmed live in 5.7.4

- `-ExecCmds="py <script> -- --foo=..."` works, but the script path cannot contain
  spaces — quotes inside the already-quoted ExecCmds value get mangled. We copy
  `build_metahuman.py` to `C:/tmp/mh/build_mh.py` before launch.
- `pythonscript` commandlet does **not** tick Slate, which means
  `request_texture_sources` never completes. Texture-dependent builds MUST launch
  via GUI editor startup (`-ExecCmds`) so the Slate post-tick callback fires.
  `-unattended -RenderOffScreen -nosplash` makes this effectively headless
  (no windows, no modal dialogs) while still ticking.
- Boot is blocked by the `Restore Packages` autosave dialog if a previous UE
  session was killed mid-save. `-unattended` dismisses it; as belt-and-braces
  the launcher pre-deletes `<Project>/Saved/Autosaves/Game/<Name>_Auto*.uasset`.
- `MetaHumanCharacterEditorBuildParameters` does **not** expose `b_export_zip_file`
  (the Python name strips the `b` prefix — use `export_zip_file`).
- EOS `Login failed - error code: EOS_InvalidAuth` / `EOS_InvalidParameters`
  warnings in the log are cosmetic; texture downloads still complete via the
  editor's existing Epic login session.
