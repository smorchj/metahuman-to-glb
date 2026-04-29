# Stage 00 — UE Assemble (5.7 Optimized)

Turn an unrigged `MetaHumanCharacter` asset into an in-engine, saved
SkeletalMesh + texture tree under `/Game/<Name>/` so stage 01 can
FBX-export it.

## Precondition

- UE 5.6 project with the `MetaHumanCharacter` plugin enabled (e.g.
  `MetaHumans3.uproject`).
- A `MetaHumanCharacter` asset exists at `mh_folder` (e.g.
  `/Game/MetaHumans/Gabo.Gabo`).
- Editor must be closed (the launcher boots a GUI editor and drives it
  headlessly via `-unattended -ExecCmds`).

## Process

The launcher runs `tools/build_metahuman.py` inside the editor's
Python environment. The script:

1. `try_add_object_to_edit(character)`
2. If `is_auto_rigged` is False → `request_auto_rigging(character)` (Epic
   cloud call, ~8 seconds; uses the editor's active Epic login).
3. After a short grace → `request_texture_sources(character)` (downloads
   high-res body + face textures from Epic cloud).
4. Tick loop: wait for `can_build_meta_human(character)` to flip true.
5. `build_meta_human(character, params)` with
   `pipeline_type = Cinematic` (UE 5.6 MH plugin only exposes Cinematic
   + DCC; Optimized landed in 5.7).
6. Save `/Game/<Name>/` so the resulting SkeletalMesh assets persist
   on disk for stage 01.

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
| SkeletalMesh assets | `<UE project>/Content/<Name>/Body|Face|Clothing|Grooms/` | Saved in-engine |
| Status JSON | (transient) | `C:/tmp/mh/status.json` |

Stage 01 picks up from the saved `/Game/<Name>/` content.

## Launcher

```powershell
./tools/run_assemble.ps1 -Char <id>
```

(or invoke `build_metahuman.py` directly via `-ExecCmds` if you have
a live editor running.)
