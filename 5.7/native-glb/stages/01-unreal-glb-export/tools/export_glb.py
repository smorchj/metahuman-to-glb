"""
Stage 01 / UE 5.7 Native GLB — MetaHuman → .glb per SkeletalMesh.

Runs inside UnrealEditor-Cmd.exe as:
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/export_glb.py -- --char=<id>"

Requires the `GLTFExporter` plugin (EnabledByDefault=true, ships with 5.7).
AssetExportTask resolves the `.glb` extension to the GLTFSkeletalMeshExporter,
which writes a single binary glTF containing geometry + skeleton + skin
weights + morph targets + a first-pass PBR material bake.

For every SkeletalMesh under `/Game/<Name>/` we emit `<name>.glb` into
`01-glb/` under the character's pipeline folder. Hair-card StaticMeshes
under `/Game/<Name>/Grooms/` are also emitted so stage 02 can re-parent
them to the face mesh's head bone.

The exporter's material bake uses UE's shader graph so the per-LOD MH
baked atlases survive (skin BC/N/SRMF/Scatter) — no 5.6/5.7 rename step
needed. Stage 02 rebuilds the skin + eye shaders in Blender on top of
the baked textures embedded in each .glb.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import traceback

import unreal


def _log(msg):
    unreal.log(f"[mh-glb57] {msg}")


def _parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=False, default=None,
                   help="(optional) pipeline workspace root; falls back to MH_PIPELINE_WORKSPACE env")
    ns = p.parse_args(argv)
    if not ns.workspace:
        ns.workspace = os.environ.get("MH_PIPELINE_WORKSPACE")
    if not ns.workspace:
        raise RuntimeError("workspace not provided — pass --workspace or set MH_PIPELINE_WORKSPACE")
    return ns


def _ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def _iso_now():
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _list_under(folder, class_name):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    # Force-scan the folder before filtering. UE's AssetRegistry lazy-
    # loads sub-paths; without an explicit scan, /Game/<char>/Body and
    # /Game/<char>/Outfits can silently return 0 hits even though the
    # uassets exist on disk (the FaceMesh tends to be auto-loaded via
    # editor refs, the body+outfits often aren't). That produces a
    # partial export that's only visible if you cross-check disk vs.
    # manifest — a regression we saw on Karl.
    try:
        ar.scan_paths_synchronous([folder], force_rescan=True)
    except Exception as e:
        _log(f"  scan_paths_synchronous({folder}) raised: {e}")
    try:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", class_name)],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_names=[class_name],
            recursive_paths=True,
        )
    out = []
    dropped = []
    for a in ar.get_assets(filt) or []:
        try:
            obj = a.get_asset()
        except Exception:
            obj = None
        if obj is None:
            try:
                obj = unreal.EditorAssetLibrary.load_asset(f"{a.package_name}.{a.asset_name}")
            except Exception as e:
                obj = None
                dropped.append(f"{a.package_name}.{a.asset_name} ({e})")
        if obj is not None:
            out.append(obj)
        elif a not in (None,):
            dropped.append(f"{getattr(a, 'package_name', '?')}.{getattr(a, 'asset_name', '?')}")
    if dropped:
        _log(f"  WARN: _list_under({folder}, {class_name}) dropped {len(dropped)} asset(s): {dropped}")
    return out


def _make_gltf_options():
    """Build GLTFExportOptions for MH data extraction.

    Key flags:
      - bake_material_inputs=USE_MESH_DATA — render each material's PBR
        outputs (BC/N/ORM/Scatter…) using the mesh's own UVs so the
        baked textures line up with the geometry on import.
      - export_morph_targets=True — ARKit shape keys survive.
      - export_vertex_skin_weights=True — skeleton + skinning survive.
      - export_uniform_scale=0.01 — UE is cm; glTF expects metres.
      - texture_image_format=PNG — lossless skin textures.

    Bake size stays at the exporter default (1024). The quality problem
    is not the bake output dimensions — it's that the SOURCE textures
    used by the bake are low-res when build_meta_human runs before
    `has_high_resolution_textures` flips. Fix that in stage 0, not here.
    """
    opts = unreal.GLTFExportOptions()
    for k, v in (
        ("bake_material_inputs", unreal.GLTFMaterialBakeMode.USE_MESH_DATA),
        # Disable GLB morph export — adds 858 raw RigLogic morphs that
        # bloat the GLB by ~750 MB and ship as 8-primitive × 858-target
        # arrays even on primitives that don't deform (teeth, eyeballs).
        # Stage 02 bakes the 51 named ARKit shape keys onto the GLB face
        # mesh from the Sequencer-baked LSE FBX (`LS_arkit_full.fbx`),
        # so the raw morphs from this GLB export are unused dead weight.
        ("export_morph_targets", False),
        ("export_vertex_skin_weights", True),
        ("export_uniform_scale", 0.01),
        ("texture_image_format", unreal.GLTFTextureImageFormat.PNG),
        ("default_level_of_detail", 0),
        ("export_preview_mesh", False),
        ("skip_near_default_values", True),
    ):
        try: opts.set_editor_property(k, v)
        except Exception as e: _log(f"  GLTFExportOption.{k} not set: {e}")
    return opts


def _export_one(asset, filepath, opts):
    task = unreal.AssetExportTask()
    task.object = asset
    task.filename = filepath
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.use_file_archive = False
    task.write_empty_files = False
    task.options = opts
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(f"glTF export failed: {asset.get_path_name()} -> {filepath}")


def _export_arkit_shape_sources(face_skm, out_dir):
    """Export the source files stage 02 needs to bake the ARKit shape
    keys onto the face mesh:

      <face>.fbx              face SkeletalMesh + 858 raw RigLogic
                              morphs baked from the joints+blend-
                              shapes auto-rig.
      <as>.fbx                AS_MetaHuman_ARKit_Mapping animation -
                              one frame per ARKit pose, drives the
                              underlying raw morphs to compose the
                              ARKit blendshape.
      arkit_pose_names.json   ordered list of pose names from
                              PA_MetaHuman_ARKit_Mapping. Stage 02
                              maps frame N -> pose_names[N] when
                              naming each shape key.

    UE's GLTFExporter doesn't carry shape-key NAMES through (it
    serializes them as `target_0`, `target_1` ...), so the GLB path
    can't deliver named ARKit shapes on its own. The FBX path does
    preserve names for raw morphs, and stepping the AnimSequence in
    Blender with `shape_key_add(from_mix=True)` per frame yields a
    proper named ARKit shape per pose."""
    pa_path = "/MetaHumanCharacter/Face/ARKit/PA_MetaHuman_ARKit_Mapping"
    as_path = "/MetaHumanCharacter/Face/ARKit/AS_MetaHuman_ARKit_Mapping"

    pa = unreal.EditorAssetLibrary.load_asset(pa_path)
    a_seq = unreal.EditorAssetLibrary.load_asset(as_path)
    if pa is None or a_seq is None:
        _log(f"  ARKit asset(s) missing - PA={pa is not None}, AS={a_seq is not None}")
        return None

    pose_names = list(pa.get_pose_names() or [])
    _log(f"  ARKit poses ({len(pose_names)}): "
         f"{[str(n) for n in pose_names[:6]]}... "
         f"{[str(n) for n in pose_names[-3:]]}")
    pose_names_json = os.path.join(out_dir, "arkit_pose_names.json")
    with open(pose_names_json, "w", encoding="utf-8") as f:
        json.dump([str(n) for n in pose_names], f, indent=2)

    # Per-pose curve weights extracted from the AnimSequence. The
    # AS drives `ctrl_expressions_*` curves which are RigLogic INPUTS,
    # not morph weights directly - but for ARKit primary shapes the
    # MetaHuman rig exposes the underlying SKM morphs with names
    # mirroring the control names (e.g. `ctrl_expressions_eyeblinkl`
    # has a corresponding `eye_blink_L` morph target on the SKM).
    # Stage 02 reads this JSON and maps the curves onto the FBX face's
    # shape keys, sets the weights, then bakes the deformed mesh as a
    # named shape key via `from_mix=True`. This bypasses Blender's FBX
    # morph-curve round-trip (which silently drops the AS curves).
    pose_curves = {}
    eval_opts = unreal.AnimPoseEvaluationOptions()
    for i, pose_name in enumerate(pose_names):
        try:
            anim_pose = a_seq.get_anim_pose_at_frame(i, eval_opts)
        except Exception as e:
            _log(f"    pose[{i}]={pose_name} eval failed: {e}")
            continue
        curve_names = list(anim_pose.get_curve_names() or [])
        weights = {}
        for cn in curve_names:
            try:
                w = anim_pose.get_curve_weight(cn)
            except Exception:
                w = 0.0
            if abs(w) > 1e-4:  # skip noise floor
                weights[str(cn)] = float(w)
        pose_curves[str(pose_name)] = weights
    pose_curves_json = os.path.join(out_dir, "arkit_pose_curves.json")
    with open(pose_curves_json, "w", encoding="utf-8") as f:
        json.dump(pose_curves, f, indent=2)
    nz = sum(len(v) for v in pose_curves.values())
    _log(f"  ARKit pose curves: {len(pose_curves)} poses, "
         f"{nz} non-zero curve values total")

    # Per-pose BONE TRANSFORMS. The MH face is a JOINTS_AND_BLEND_SHAPES rig
    # — joints do bulk motion (jaw rotation, eye rotation, eyelid roll),
    # morphs add fine detail. Curves alone give ~7mm jaw open; the rest of
    # the deformation lives in joint pose. UE's FBX exporter only emits
    # direct bone keyframes and silently drops curve-driven RigLogic outputs,
    # so the AnimSequence FBX has empty bone tracks. We extract the resolved
    # bone poses ourselves via AnimPose.get_relative_to_ref_pose_transform()
    # and let stage 02 reapply them in Blender. Only non-identity bones
    # (those that actually move for a given pose) are recorded.
    bone_eps_loc, bone_eps_rot, bone_eps_scl = 1e-5, 1e-5, 1e-5
    pose0 = a_seq.get_anim_pose_at_frame(0, eval_opts)
    bone_names = [str(n) for n in (pose0.get_bone_names() or [])]
    pose_bones = {}
    for i, pose_name in enumerate(pose_names):
        try:
            anim_pose = a_seq.get_anim_pose_at_frame(i, eval_opts)
        except Exception as e:
            _log(f"    bone-pose[{i}]={pose_name} eval failed: {e}")
            continue
        moved = {}
        for bn in bone_names:
            try:
                t = anim_pose.get_relative_to_ref_pose_transform(bn)
            except Exception:
                continue
            loc = t.translation
            rot = t.rotation
            scl = t.scale3d
            if (abs(loc.x) < bone_eps_loc and abs(loc.y) < bone_eps_loc
                    and abs(loc.z) < bone_eps_loc
                    and abs(rot.x) < bone_eps_rot and abs(rot.y) < bone_eps_rot
                    and abs(rot.z) < bone_eps_rot
                    and abs(rot.w - 1.0) < bone_eps_rot
                    and abs(scl.x - 1.0) < bone_eps_scl
                    and abs(scl.y - 1.0) < bone_eps_scl
                    and abs(scl.z - 1.0) < bone_eps_scl):
                continue  # bone at rest for this pose
            moved[bn] = {
                "loc":   [float(loc.x), float(loc.y), float(loc.z)],
                "rot":   [float(rot.x), float(rot.y), float(rot.z), float(rot.w)],
                "scale": [float(scl.x), float(scl.y), float(scl.z)],
            }
        pose_bones[str(pose_name)] = moved
    pose_bones_json = os.path.join(out_dir, "arkit_pose_bones.json")
    with open(pose_bones_json, "w", encoding="utf-8") as f:
        json.dump(pose_bones, f, indent=2)
    nz_bones = sum(len(v) for v in pose_bones.values())
    _log(f"  ARKit pose bones: {len(pose_bones)} poses, "
         f"{nz_bones} non-rest bone-pose entries (across {len(bone_names)} bones)")

    # SkeletalMesh FBX with morph targets
    face_fbx = os.path.join(out_dir, f"{face_skm.get_name()}.fbx")
    _run_fbx_export_task(face_skm, face_fbx, is_skeletal=True)

    # AnimSequence FBX (one frame per ARKit pose) — kept around for
    # debugging, even though Blender's importer drops the curve tracks.
    as_fbx = os.path.join(out_dir, "AS_MetaHuman_ARKit_Mapping.fbx")
    _run_fbx_export_task(a_seq, as_fbx, is_skeletal=False)

    # Sequencer-bake FBX. Drives the face SKM with the ARKit AnimSequence
    # via a transient Level Sequence and exports the whole scene as FBX.
    # This is the only path that round-trips the resolved RigLogic bone
    # transforms through to Blender — direct AnimSequence FBX export
    # silently drops the curve-driven bones (action ends up empty).
    # Limitations: morph weight keyframes do NOT round-trip through
    # FBX; only bones do. The bones alone capture most ARKit motion
    # (jaw rotation, eye rotation, eyelid roll); morph deltas would
    # add fine detail (lip squash, wrinkle correctives) which we lose.
    lse_fbx = os.path.join(out_dir, "LS_arkit_full.fbx")
    lse_ok = _export_sequencer_bake(face_skm, a_seq, lse_fbx)
    if not lse_ok:
        _log("  Sequencer bake failed; downstream stage 02 will not have "
             "ARKit shape keys.")

    return {
        "face_fbx": os.path.basename(face_fbx),
        "anim_fbx": os.path.basename(as_fbx),
        "lse_fbx": os.path.basename(lse_fbx) if lse_ok else None,
        "pose_names": "arkit_pose_names.json",
        "pose_curves": "arkit_pose_curves.json",
        "pose_bones": "arkit_pose_bones.json",
        "pose_count": len(pose_names),
    }


def _export_sequencer_bake(face_skm, a_seq, fbx_out):
    """Build a transient Level Sequence with the face SKM driven by the
    ARKit AnimSequence, then FBX-export the whole scene. Output FBX
    contains:
      - mesh + skeleton (rest pose)
      - per-frame bone keyframes resolved by RigLogic + correctives
    Returns True on success, False on failure (and the caller is free
    to continue without the sequencer bake)."""
    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
        if world is None:
            _log("  Sequencer: no editor world available")
            return False
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.SkeletalMeshActor, unreal.Vector(0, 0, 0),
            unreal.Rotator(0, 0, 0))
        smc = actor.skeletal_mesh_component
        smc.set_skeletal_mesh(face_skm)
        smc.set_skinned_asset_and_update(face_skm)

        seq_path = "/Game/Temp/LS_arkit_export"
        if unreal.EditorAssetLibrary.does_asset_exist(seq_path):
            unreal.EditorAssetLibrary.delete_asset(seq_path)
        factory = unreal.LevelSequenceFactoryNew()
        seq = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            asset_name="LS_arkit_export", package_path="/Game/Temp",
            asset_class=unreal.LevelSequence, factory=factory)
        binding = seq.add_possessable(actor)
        anim_track = binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)
        section = anim_track.add_section()
        section.params.animation = a_seq

        # AS_MetaHuman_ARKit_Mapping is a 24fps animation with 66 keyframes
        # (one per pose). If we leave the level sequence at the default
        # 30fps, the bake interpolates between poses — so sampling at any
        # frame gives a BLEND of the two adjacent ARKit shapes (e.g. pose
        # browDownLeft would land at frame 53.5 and we'd actually capture
        # ~50% browDownLeft + 50% mouthUpperUpRight). Setting display
        # rate to match the source AS makes each integer bake frame
        # correspond to one source pose exactly, no interpolation.
        fps = 24
        seq.set_display_rate(unreal.FrameRate(fps, 1))
        length_frames = int(a_seq.get_play_length() * fps + 0.5)
        section.set_range(0, length_frames)
        seq.set_playback_start(0)
        seq.set_playback_end(length_frames)

        params = unreal.SequencerExportFBXParams()
        params.world = world
        params.sequence = seq
        params.root_sequence = seq
        params.bindings = [binding]
        params.tracks = []
        params.fbx_file_name = fbx_out
        opts = unreal.FbxExportOption()
        opts.set_editor_property("export_morph_targets", True)
        opts.set_editor_property("level_of_detail", False)
        params.override_options = opts

        ok = unreal.SequencerTools.export_level_sequence_fbx(params)
        if not ok or not os.path.exists(fbx_out):
            _log(f"  Sequencer FBX export failed: ok={ok}")
            return False
        _log(f"  + {os.path.basename(fbx_out)}  "
             f"({os.path.getsize(fbx_out)/1_000_000:.1f} MB) "
             f"[Sequencer-baked AnimSequence at 30fps over {length_frames} frames]")
        # NOTE: not deleting the transient /Game/Temp/LS_arkit_export
        # uasset or destroying the SkeletalMeshActor — UE crashes on
        # delete_asset for an asset that's still referenced by an
        # actor in the world. The FBX is what stage 02 consumes; the
        # uasset is harmless leftover that the next run will overwrite.
        return True
    except Exception as e:
        _log(f"  Sequencer bake exception: {e}")
        return False


def _run_fbx_export_task(asset, filepath, is_skeletal):
    task = unreal.AssetExportTask()
    task.object = asset
    task.filename = filepath
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.use_file_archive = False
    task.write_empty_files = False
    opts = unreal.FbxExportOption()
    try: opts.set_editor_property("export_morph_targets", True)
    except Exception: pass
    if is_skeletal:
        try: opts.set_editor_property("level_of_detail", False)
        except Exception: pass
        try: opts.set_editor_property("collision", False)
        except Exception: pass
    task.options = opts
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(f"FBX export failed: {asset.get_path_name()} -> {filepath}")
    sz = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    _log(f"  + {os.path.basename(filepath)}  ({sz/1_000_000:.1f} MB)")


def _iter_materials(mesh):
    """Yield (slot_name, material_interface) for each non-null slot
    on the mesh. Handles both static_materials and materials property
    name variants."""
    for attr in ("materials", "static_materials"):
        try:
            slots = mesh.get_editor_property(attr)
        except Exception:
            slots = None
        if slots:
            for s in slots:
                try:
                    mi = s.get_editor_property("material_interface")
                    slot = str(s.get_editor_property("material_slot_name"))
                except Exception:
                    mi = getattr(s, "material_interface", None)
                    slot = str(getattr(s, "material_slot_name", "") or "")
                if mi is not None:
                    yield slot, mi
            return


def _textures_in_material(mi, seen):
    """Yield (param_name_or_None, texture) for every Texture2D the
    material references — first via named texture parameters, then
    via get_used_textures() to catch hard-wired refs in the parent
    material graph."""
    try:
        names = unreal.MaterialEditingLibrary.get_texture_parameter_names(mi) or []
    except Exception:
        names = []
    for n in names:
        try:
            tex = unreal.MaterialEditingLibrary.get_texture_parameter_value(mi, n)
        except Exception:
            tex = None
        if tex is not None and tex not in seen:
            seen.add(tex)
            yield str(n), tex
    try:
        used = unreal.MaterialEditingLibrary.get_used_textures(mi) or []
    except Exception:
        used = []
    for t in used:
        if t is not None and t not in seen:
            seen.add(t)
            yield None, t


def _export_texture_png(texture, filepath):
    """AssetExportTask resolves a Texture2D + .png filename to the
    PNGImageExporter, which writes the source mip0 (full resolution)."""
    task = unreal.AssetExportTask()
    task.object = texture
    task.filename = filepath
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.use_file_archive = False
    task.write_empty_files = False
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(
            f"PNG export failed: {texture.get_path_name()} -> {filepath}")


def _read_mi_params(mi):
    """Return {'vectors': {name: [r,g,b,a]}, 'scalars': {name: float}}
    for a MaterialInstance. MH hair-card materials expose
    `hairMelanin` / `hairRedness` / `hairDye` (per the MH plugin's
    legacy hair shader) - stage 02 reads these to synthesize the
    hair Base Color procedurally, since hair cards have no albedo
    texture (only Attribute + Coverage data atlases). Lifted from
    5.6/cinematic/stages/01-metahuman-engine-export/tools/export_mh.py."""
    out = {"vectors": {}, "scalars": {}}

    def _pname(entry):
        try:
            pi = entry.get_editor_property("parameter_info")
            n = pi.get_editor_property("name") if pi else None
            return str(n) if n else None
        except Exception:
            try:
                return str(entry.parameter_info.name)
            except Exception:
                return None

    try:
        vecs = mi.get_editor_property("vector_parameter_values") or []
    except Exception:
        vecs = []
    for v in vecs:
        name = _pname(v)
        if not name:
            continue
        try:
            pv = v.get_editor_property("parameter_value")
            out["vectors"][name] = [float(pv.r), float(pv.g),
                                    float(pv.b), float(pv.a)]
        except Exception:
            continue

    try:
        scls = mi.get_editor_property("scalar_parameter_values") or []
    except Exception:
        scls = []
    for s in scls:
        name = _pname(s)
        if not name:
            continue
        try:
            out["scalars"][name] = float(
                s.get_editor_property("parameter_value"))
        except Exception:
            continue

    return out


def _export_sidecar_textures(asset, textures_dir, seen_tex):
    """Walk every material slot on `asset`, export every texture
    parameter (and used-texture) as a PNG into `textures_dir`. Returns
    a list of records describing what was exported, keyed by the
    asset that referenced them, so stage 02 can wire the sidecar
    PNGs into Blender materials when GLTFExporter's bake leaves a
    mesh with WorldGridMaterial / empty-texture placeholders.

    Why this exists: MH hair-card and eyelash materials use custom
    shading nodes that GLTFExporter can't translate via USE_MESH_DATA.
    Those meshes ship in the .glb with no textures attached. The
    underlying source Texture2D assets are still on disk, so we
    just export them directly."""
    records = []
    for slot, mi in _iter_materials(asset):
        try:
            mi_path = mi.get_path_name()
        except Exception:
            mi_path = ""
        for param, tex in _textures_in_material(mi, seen_tex):
            try:
                tex_name = tex.get_name()
            except Exception:
                continue
            rel = f"textures/{tex_name}.png"
            abs_path = os.path.join(textures_dir, f"{tex_name}.png")
            try:
                _export_texture_png(tex, abs_path)
                sz = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
                records.append({
                    "asset_path": tex.get_path_name(),
                    "file_path": rel,
                    "size_bytes": sz,
                    "param": param or "",
                    "material": mi_path,
                    "slot": slot,
                })
            except Exception as e:
                _log(f"    sidecar PNG fail {tex_name}: {e}")
    return records


def main():
    args = _parse_args()
    workspace = os.path.abspath(args.workspace)
    _log(f"char={args.char}  workspace={workspace}")

    char_root = os.path.join(workspace, "characters", args.char)
    char_manifest = json.load(open(os.path.join(char_root, "manifest.json"), encoding="utf-8"))
    mh_folder = char_manifest["mh_folder"]

    out_dir = _ensure_dir(os.path.join(char_root, "01-glb"))
    textures_dir = _ensure_dir(os.path.join(out_dir, "textures"))

    # Clear stale GLB outputs before re-running. Without this, a partial
    # re-run (e.g. AR misses an asset, or _export_one fails for one mesh)
    # leaves orphan GLBs from a previous successful run on disk while
    # the new manifest references this run's subset only — masking the
    # incompleteness from any downstream check that lists the dir.
    # Manifest is the single source of truth for what was exported.
    import glob as _glob
    for stale in _glob.glob(os.path.join(out_dir, "*.glb")):
        try:
            os.remove(stale)
        except Exception as e:
            _log(f"  could not remove stale {stale}: {e}")

    # SkeletalMeshes — body, face, outfits
    skm = _list_under(mh_folder, "SkeletalMesh")
    skm_names = [a.get_name() for a in skm]
    # A MetaHuman character ALWAYS has a face mesh + body mesh; outfits
    # are optional (base humans without clothes don't ship one). If
    # face or body are missing here, the AR scan returned an incomplete
    # listing — fail loud rather than silently producing a partial
    # export with a face but no body.
    have_face = any("FaceMesh" in n for n in skm_names)
    have_body = any("BodyMesh" in n for n in skm_names)
    if not (have_face and have_body):
        raise RuntimeError(
            f"AssetRegistry returned incomplete SkeletalMesh list under "
            f"{mh_folder}: {skm_names} (face={have_face}, body={have_body}). "
            f"Both FaceMesh and BodyMesh are required for a MetaHuman. "
            f"Re-run stage 00 to refresh /Game/<char>/, or check that the "
            f"MetaHuman build actually completed and emitted a body mesh.")

    # StaticMeshes — groom card fallbacks under /Game/<Name>/Grooms/.
    # MH naming is inconsistent: Hair_*/Eyebrows_* use plural "CardsMesh"
    # but Beard_*/Mustache_* use singular "CardMesh". Match both, plus
    # the LOD0 variant only (other LODs are gitignored downstream).
    stm_all = _list_under(mh_folder, "StaticMesh")
    hair_cards = [
        m for m in stm_all
        if (("CardsMesh" in m.get_name()) or ("CardMesh" in m.get_name()))
        and "_LOD0" in m.get_name()
    ]

    _log(f"found {len(skm)} SkeletalMesh(es) ({skm_names}) + "
         f"{len(hair_cards)} hair-card StaticMesh(es)")

    opts = _make_gltf_options()
    manifest_records = []
    warnings = []
    seen_tex = set()  # de-dupe Texture2D exports across assets
    for asset in skm + hair_cards:
        name = asset.get_name()
        rel = f"{name}.glb"
        abs_path = os.path.join(out_dir, rel)
        try:
            _export_one(asset, abs_path, opts)
            sz = os.path.getsize(abs_path)
            _log(f"  + {rel}  ({sz/1_000_000:.1f} MB)")
            manifest_records.append({
                "asset_path": asset.get_path_name(),
                "file_path": rel,
                "size_bytes": sz,
                "mesh_type": type(asset).__name__,
                "role": _infer_role(asset),
            })
        except Exception as e:
            warnings.append(f"{name}: {e}")
            _log(f"  ERROR {name}: {e}")

    # Sidecar texture export for hair-card materials.
    #
    # GLTFExporter's USE_MESH_DATA bake assigns WorldGridMaterial to
    # hair-card StaticMeshes because the MH hair-card shader doesn't
    # translate cleanly. The actual hair-card textures (BaseColor,
    # Coverage/Alpha, Tangent/Normal) live at a deterministic path:
    # /Game/<char>/Grooms/Textures/. Stage 02 reads this manifest to
    # rebuild the hair card materials in Blender.
    sidecar_records = []
    grooms_textures = mh_folder + "/Grooms/Textures"
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        ar.scan_paths_synchronous([grooms_textures], force_rescan=True)
    except Exception as e:
        _log(f"  sidecar: scan {grooms_textures} failed: {e}")
    try:
        groom_assets = ar.get_assets_by_path(
            grooms_textures, recursive=True, include_only_on_disk_assets=False)
    except Exception:
        groom_assets = []
    tex_assets = [a for a in groom_assets
                  if str(getattr(a, "asset_class_path", a).asset_name) == "Texture2D"]
    _log(f"  sidecar: {len(tex_assets)} Texture2D in {grooms_textures}")

    # Card-atlas textures for hair, eyebrows, beard, mustache, eyelashes
    # all live in the engine plugin content (NOT under /Game/<char>/),
    # with one folder per style. Discover the character's groom styles
    # by listing GroomAssets under /Game/<char>/Grooms/, then pull the
    # matching plugin atlases by predictable path.
    plugin_eyebrows  = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Eyebrows"
    plugin_hair      = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Hair"
    plugin_beards    = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Beards"
    plugin_mustaches = "/MetaHumanCharacter/Optional/Grooms/GroomAssets/Mustaches"
    plugin_lashes_textures = "/MetaHumanCharacter/Textures/Eyelashes"
    try:
        groom_root_assets = ar.get_assets_by_path(
            mh_folder + "/Grooms", recursive=False,
            include_only_on_disk_assets=False)
    except Exception:
        groom_root_assets = []
    groom_styles = []
    for a in groom_root_assets or []:
        cls = str(getattr(a, "asset_class_path", a).asset_name)
        if cls == "GroomAsset":
            groom_styles.append(str(a.asset_name))
    _log(f"  sidecar: groom styles in /Game/{args.char}/Grooms: {groom_styles}")

    # Force-index the plugin content roots that hold the card atlases.
    # They aren't auto-indexed at editor startup (the MetaHuman plugin
    # uses lazy loading for its Optional content), so LoadAsset returns
    # None unless we scan first.
    try:
        ar.scan_paths_synchronous(
            [plugin_eyebrows, plugin_hair, plugin_beards,
             plugin_mustaches, plugin_lashes_textures],
            force_rescan=False)
    except Exception as e:
        _log(f"  sidecar: scan plugin paths failed: {e}")

    # Map groom-style name prefix -> plugin-content folder. Hair, brows,
    # beard, mustache all follow the same `<style>_CardsAtlas_{kind}`
    # convention; eyelashes are special-cased (single Coverage texture
    # under a different plugin path).
    style_to_plugin = (
        ("Eyebrows_", plugin_eyebrows),
        ("Hair_",     plugin_hair),
        ("Beard_",    plugin_beards),
        ("Mustache_", plugin_mustaches),
    )

    plugin_atlas_paths = []
    for style in groom_styles:
        matched = False
        for prefix, plugin_dir in style_to_plugin:
            if style.startswith(prefix):
                for atlas_kind in ("Attribute", "Tangent"):
                    plugin_atlas_paths.append(
                        f"{plugin_dir}/{style}/{style}_CardsAtlas_{atlas_kind}"
                        f".{style}_CardsAtlas_{atlas_kind}")
                matched = True
                break
        if matched:
            continue
        if style.startswith("Eyelashes_"):
            plugin_atlas_paths.append(
                f"{plugin_lashes_textures}/T_{style}_Coverage"
                f".T_{style}_Coverage")
    for p in plugin_atlas_paths:
        try:
            tex = unreal.EditorAssetLibrary.load_asset(p)
        except Exception as e:
            _log(f"    sidecar: load {p} raised: {e}")
            continue
        if tex is None:
            _log(f"    sidecar: load {p} returned None")
            continue
        tex_name = tex.get_name()
        rel = f"textures/{tex_name}.png"
        abs_path = os.path.join(textures_dir, f"{tex_name}.png")
        try:
            _export_texture_png(tex, abs_path)
            sz = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
            sidecar_records.append({
                "asset_path": tex.get_path_name(),
                "file_path": rel,
                "size_bytes": sz,
                "name": tex_name,
            })
            _log(f"    + plugin: {rel} ({sz/1024:.0f} KB)")
        except Exception as e:
            _log(f"    sidecar PNG fail {tex_name}: {e}")

    # MI parameter dump for groom materials. Hair-card meshes have no
    # albedo texture; their color comes from MI scalar/vector params
    # (hairMelanin, hairRedness, hairDye). Walk every MaterialInstance
    # under /Game/<char>/Grooms/ and record its params keyed by name.
    grooms_path = mh_folder + "/Grooms"
    try:
        groom_mi_assets = ar.get_assets_by_path(
            grooms_path, recursive=True, include_only_on_disk_assets=False)
    except Exception:
        groom_mi_assets = []
    groom_materials = {}
    for a in groom_mi_assets or []:
        cls = str(getattr(a, "asset_class_path", a).asset_name)
        if "Material" not in cls:
            continue
        try:
            mi = a.get_asset()
        except Exception:
            mi = None
        if mi is None:
            continue
        params = _read_mi_params(mi)
        if not (params["vectors"] or params["scalars"]):
            continue
        mi_name = str(a.asset_name)
        groom_materials[mi_name] = {
            "asset_path": str(a.package_name),
            "vectors": params["vectors"],
            "scalars": params["scalars"],
        }
        _log(f"    groom mi[{mi_name}]: vectors={list(params['vectors'])} "
             f"scalars={list(params['scalars'])}")

    for a in tex_assets:
        try:
            tex = a.get_asset()
        except Exception as e:
            _log(f"    sidecar: load {a.package_name} raised: {e}")
            continue
        if tex is None:
            continue
        tex_name = str(a.asset_name)
        rel = f"textures/{tex_name}.png"
        abs_path = os.path.join(textures_dir, f"{tex_name}.png")
        try:
            _export_texture_png(tex, abs_path)
            sz = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
            sidecar_records.append({
                "asset_path": str(tex.get_path_name()),
                "file_path": rel,
                "size_bytes": sz,
                "name": tex_name,
            })
            _log(f"    + {rel} ({sz/1024:.0f} KB)")
        except Exception as e:
            _log(f"    sidecar PNG fail {tex_name}: {e}")

    # ARKit blendshape source export. UE GLTFExporter loses morph names,
    # so the GLB we just wrote has the 858 raw RigLogic shapes as
    # `target_0`...`target_857`. Stage 02 needs the FBX files (which
    # preserve morph names) plus the AnimSequence frames to bake the
    # ARKit poses as named shape keys. Find the face SkeletalMesh in
    # the export set and export those alongside the GLBs.
    arkit_sources = None
    face_skm = next((a for a in skm if "FaceMesh" in a.get_name()), None)
    if face_skm is not None:
        try:
            arkit_sources = _export_arkit_shape_sources(face_skm, out_dir)
        except Exception as e:
            _log(f"  ARKit source export failed: {e}")
            warnings.append(f"arkit_sources: {e}")

    mh_manifest = {
        "character_id": args.char,
        "ue_version": char_manifest.get("ue_version", "5.7"),
        "pipeline": "native-glb",
        "exported_at": _iso_now(),
        "mh_folder": mh_folder,
        "assets": manifest_records,
        "sidecar_textures": sidecar_records,
        "groom_materials": groom_materials,
        "arkit_sources": arkit_sources,
        "warnings": warnings,
    }
    with open(os.path.join(out_dir, "mh_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(mh_manifest, f, indent=2)
    _log(f"wrote {os.path.join(out_dir, 'mh_manifest.json')}  "
         f"({len(manifest_records)} assets, {len(warnings)} warnings)")


def _infer_role(asset):
    n = asset.get_name().lower()
    if "facemesh" in n:   return "face"
    if "bodymesh" in n:   return "body"
    if "outfit" in n:     return "outfit"
    # MH naming: Hair_*/Eyebrows_* use plural "cardsmesh"; Beard_*/
    # Mustache_* use singular "cardmesh". Both are facial/head grooms
    # that stage 02 parents onto the head bone — same role.
    if "cardsmesh" in n or "cardmesh" in n: return "hair"
    return "other"


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"FATAL: {e}")
        _log(traceback.format_exc())
        sys.exit(1)
