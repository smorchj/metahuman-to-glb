"""
Drive the 5.7 MetaHuman build lifecycle on an existing MetaHumanCharacter
asset by pumping the editor's tick loop rather than blocking Python:

  try_add_object_to_edit
  request_texture_sources      (async, Epic cloud)
  <wait for can_build_meta_human>
  build_meta_human             (sync)
  <write status file>

Meant to be invoked on Editor STARTUP (not commandlet — ticks don't run
there and the texture downloads will stall). The shell wrapper launches
UnrealEditor.exe with -ExecCmds="py <this> -- --asset=... --status=...".

Status file transitions: STARTING -> REQUESTED -> WAITING -> BUILDING
-> DONE | FAILED. External poller reads it to know when to stop.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import unreal

STATE = {
    "asset": "",
    "status_path": "",
    "tick_handle": None,
    "started_at": 0.0,
    "texture_request_fired": False,
    "last_logged_state": None,
    "timeout_s": 600,
    "character": None,
    "subsystem": None,
    "new_skel_before": set(),
    # Set to True before `build_meta_human` returns. Blocks tick
    # re-entry so we don't start a second build on the following frame
    # (that crashed MetaHumanCharacterPalette.dll natively during the
    # first test run).
    "building": False,
    "finished": False,
    "awaiting_rig": False,
}


def log(msg: str) -> None:
    unreal.log(f"[build_mh] {msg}")


def write_status(phase: str, **kv) -> None:
    payload = {"phase": phase, "ts": time.time(), **kv}
    Path(STATE["status_path"]).write_text(json.dumps(payload), encoding="utf-8")


def list_skel() -> set[str]:
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        f = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "SkeletalMesh")],
            recursive_paths=True, package_paths=["/Game"],
        )
    except Exception:
        f = unreal.ARFilter(class_names=["SkeletalMesh"], recursive_paths=True, package_paths=["/Game"])
    out = set()
    for d in reg.get_assets(f):
        pkg = getattr(d, "package_name", None)
        name = getattr(d, "asset_name", None)
        if pkg and name:
            out.add(f"{pkg}.{name}")
    return out


def _iter_materials(mesh):
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


def _run_export_task(asset, filepath):
    task = unreal.AssetExportTask()
    task.object = asset
    task.filename = filepath
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.use_file_archive = False
    task.write_empty_files = False
    if isinstance(asset, unreal.SkeletalMesh):
        opts = unreal.FbxExportOption()
        for k, v in (("ascii", False), ("force_front_x_axis", False),
                     ("vertex_color", True), ("level_of_detail", True),
                     ("collision", False), ("export_morph_targets", True),
                     ("export_preview_mesh", False),
                     ("map_skeletal_motion_to_root", False),
                     ("export_local_time", True)):
            try: opts.set_editor_property(k, v)
            except Exception: pass
        task.options = opts
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(f"export failed: {asset.get_path_name()} -> {filepath}")


def _export_fbx_and_textures(asset_paths, out_dir):
    """FBX-export every SkeletalMesh in asset_paths, walk its materials, and
    TGA-export every Texture2D they reference. Writes mh_manifest.json next
    to the FBX files."""
    meshes_dir = Path(out_dir) / "meshes"
    textures_dir = Path(out_dir) / "textures"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)
    mesh_records, tex_records = [], []
    seen_tex = set()
    for p in asset_paths:
        asset = unreal.load_asset(p)
        if asset is None:
            log(f"  load_asset({p}) returned None — skipped")
            continue
        if not isinstance(asset, unreal.SkeletalMesh):
            log(f"  skipping non-SkeletalMesh: {p} ({type(asset).__name__})")
            continue
        name = asset.get_name().lower()
        fbx_rel = f"meshes/{name}.fbx"
        fbx_abs = str(meshes_dir / f"{name}.fbx")
        try:
            _run_export_task(asset, fbx_abs)
            log(f"  + {fbx_rel}")
        except Exception as e:
            log(f"  FBX error for {p}: {e}")
            continue
        mats = []
        for slot, mi in _iter_materials(asset):
            mi_path = mi.get_path_name()
            mats.append({"slot": slot, "material": mi_path})
            for param, tex in _textures_in_material(mi, seen_tex):
                tp = tex.get_path_name()
                tex_rel = f"textures/{tex.get_name()}.tga"
                tex_abs = str(textures_dir / f"{tex.get_name()}.tga")
                try:
                    _run_export_task(tex, tex_abs)
                    tex_records.append({
                        "asset_path": tp, "file_path": tex_rel,
                        "material": mi_path, "param": param,
                    })
                except Exception as e:
                    log(f"  TGA error for {tp}: {e}")
        mesh_records.append({
            "component": name,
            "asset_path": p,
            "fbx_path": fbx_rel,
            "materials": mats,
        })
    manifest = {
        "ue_version": "5.7",
        "meshes": mesh_records,
        "textures": tex_records,
    }
    manifest_path = str(Path(out_dir) / "mh_manifest.json")
    Path(manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "meshes": [m["fbx_path"] for m in mesh_records],
        "textures": [t["file_path"] for t in tex_records],
        "manifest_path": manifest_path,
    }


def on_tick(dt: float) -> None:
    """Slate post-tick callback. Keeps ticking the state machine without
    blocking Python."""
    if STATE["building"] or STATE["finished"]:
        return
    try:
        character = STATE["character"]
        subsystem = STATE["subsystem"]

        # Auto-rigging is async but `is_auto_rigged()` lags — the proxy
        # doesn't always refresh to True even after UE logs
        # "Auto-Rigging finished in N.N seconds". Rather than gating on
        # it, fire the texture request once on the tick after rigging was
        # requested; from there the usual can_build wait drives the rest.
        if STATE.get("awaiting_rig"):
            elapsed_rig = time.time() - STATE.get("rig_started", STATE["started_at"])
            # Give the cloud call at least 15s, then fire textures. The
            # tex downloads won't start until the rig is done server-side
            # anyway — this just tells UE to queue them.
            if elapsed_rig < 15:
                if STATE["last_logged_state"] != ("rig_warmup", int(elapsed_rig)):
                    STATE["last_logged_state"] = ("rig_warmup", int(elapsed_rig))
                    log(f"  auto-rigging warmup ({elapsed_rig:.0f}s)")
                    write_status("RIGGING", elapsed=round(elapsed_rig, 1))
                return
            log("auto-rig grace elapsed; firing request_texture_sources")
            try:
                try:
                    params = unreal.MetaHumanCharacterTextureRequestParams()
                    subsystem.request_texture_sources(character, params)
                except Exception:
                    subsystem.request_texture_sources(character)
            except Exception as e:
                log(f"request_texture_sources (post-rig) error: {e}")
                write_status("FAILED", error=f"request_texture_sources: {e}")
                _stop()
                return
            STATE["awaiting_rig"] = False
            STATE["last_logged_state"] = None
            write_status("REQUESTED")

        hi_res = character.has_high_resolution_textures
        faces = len(list(character.synthesized_face_textures.items()))
        can_build = subsystem.can_build_meta_human(character)
        state = (hi_res, faces, can_build)
        if state != STATE["last_logged_state"]:
            log(f"  state: high_res={hi_res} face_tex={faces} can_build={can_build}")
            STATE["last_logged_state"] = state
            write_status("WAITING", high_res=hi_res, face_tex=faces, can_build=can_build)

        elapsed = time.time() - STATE["started_at"]
        # Two-phase gate:
        #
        # (1) Wait for has_high_resolution_textures to flip true. That
        #     signals Epic cloud has acknowledged the hi-res request and
        #     begun delivery. HI_RES_GRACE_S is the escape hatch in case
        #     the flag never flips.
        #
        # (2) Even after the flag flips, MH's editor shows a low-res
        #     preview until the hi-res textures finish streaming into
        #     memory. Opening the editor manually and "waiting for it
        #     to load" is exactly the same effect. Baking before that
        #     completes produces a 1024 atlas containing ~256 of real
        #     content (the original "looks like 256" complaint).
        #     POST_HIRES_GRACE_S is the extra wait AFTER the flag flips
        #     so streaming can finish.
        HI_RES_GRACE_S = 120
        POST_HIRES_GRACE_S = 90

        if can_build and not hi_res:
            if elapsed < HI_RES_GRACE_S:
                return  # keep waiting for the hi-res flag
            log(f"WARNING: high_res=False after {elapsed:.0f}s — proceeding "
                f"with cached sources. Atlas will be nominal-res but "
                f"content will look upsampled.")
            write_status("WAITING_HIRES_TIMEOUT", elapsed=round(elapsed, 1))

        # Post-hi-res streaming wait.
        if can_build and hi_res:
            if "hires_seen_at" not in STATE:
                STATE["hires_seen_at"] = time.time()
                log("high_res=True — starting post-hi-res streaming grace")
                write_status("STREAMING", high_res=True, face_tex=faces)
            since_hires = time.time() - STATE["hires_seen_at"]
            if since_hires < POST_HIRES_GRACE_S:
                # Log once every 15s so we can see progress without spam.
                floor = int(since_hires // 15) * 15
                if STATE.get("last_streaming_floor") != floor:
                    STATE["last_streaming_floor"] = floor
                    log(f"  streaming wait {since_hires:.0f}/{POST_HIRES_GRACE_S}s")
                return  # keep ticking until grace elapses

        if can_build:
            # Mark + unregister BEFORE the blocking build call so a
            # re-entrant tick cannot fire a second build.
            STATE["building"] = True
            _stop()
            log(f"textures ready (high_res={hi_res}, face_tex={faces}). "
                f"calling build_meta_human (pipeline={STATE['pipeline']}) ...")
            write_status("BUILDING", high_res=hi_res, face_tex=faces)
            try:
                params = unreal.MetaHumanCharacterEditorBuildParameters()
                # Enumerate the struct's Python attributes so we can see
                # the real names and set only what exists.
                attrs = [a for a in dir(params) if not a.startswith("_") and not callable(getattr(params, a, None))]
                log(f"build params attrs: {attrs}")

                def set_if(names, value):
                    for n in names:
                        try:
                            setattr(params, n, value)
                            log(f"  set {n} = {value!r}")
                            return True
                        except Exception as e:
                            log(f"  set {n} failed: {e}")
                    return False

                # Stock UE pipeline. pipeline_type chooses the MH runtime
                # template (Cinematic, Optimized, DCC). All other build
                # parameters stay at the plugin's defaults — no overrides
                # to pipeline_quality, no force-deletes, no plugin asset
                # mutations. The 1024 web ceiling is enforced downstream
                # in the GLB export stage, not here.
                pipeline_enum = {
                    "cinematic": unreal.MetaHumanDefaultPipelineType.CINEMATIC,
                    "optimized": unreal.MetaHumanDefaultPipelineType.OPTIMIZED,
                    "dcc":       unreal.MetaHumanDefaultPipelineType.DCC,
                }[STATE["pipeline"].lower()]
                set_if(["pipeline_type", "PipelineType"], pipeline_enum)

                if STATE["pipeline"].lower() == "dcc":
                    set_if(["absolute_build_path", "AbsoluteBuildPath"],
                           STATE["output_dir"])
                    set_if(["name_override", "NameOverride"],
                           STATE["output_name"])
                    set_if(["export_zip_file"], True)

                subsystem.build_meta_human(character, params)
            except Exception as e:
                log(f"build_meta_human error: {e}\n{traceback.format_exc()}")
                write_status("FAILED", error=str(e))
                STATE["finished"] = True
                return

            produced = []
            if STATE["pipeline"].lower() == "dcc":
                # DCC output is on disk in output_dir.
                out_dir = Path(STATE["output_dir"])
                if out_dir.exists():
                    for p in out_dir.rglob("*"):
                        if p.is_file():
                            produced.append(str(p.relative_to(out_dir)))
                log(f"build complete; {len(produced)} files in {out_dir}")
                for f in produced[:15]:
                    log(f"  + {f}")
                write_status("DONE", produced=produced, output_dir=str(out_dir))
            else:
                # Cinematic/Optimized materialize new SkeletalMesh assets
                # under /Game/<Name>/. Save them, then FBX-export plus TGA
                # textures into output_dir for the downstream Blender stage.
                now_skel = list_skel()
                before = set(STATE["new_skel_before"]) if isinstance(STATE["new_skel_before"], (set, list)) else set()
                new_assets = sorted(now_skel - before)
                log(f"build complete; {len(new_assets)} new SkeletalMesh asset(s) in /Game")
                for a in new_assets[:20]:
                    log(f"  + {a}")
                write_status("SAVING", produced=new_assets)
                game_folder = f"/Game/{STATE['output_name']}"
                try:
                    unreal.EditorAssetLibrary.save_directory(
                        game_folder, only_if_is_dirty=False, recursive=True)
                except Exception as e:
                    log(f"  save_directory({game_folder}) warning: {e}")
                unreal.EditorAssetLibrary.save_loaded_asset(character)

                write_status("EXPORTING", produced=new_assets)
                exported = _export_fbx_and_textures(new_assets, STATE["output_dir"])
                log(f"export complete; {len(exported['meshes'])} fbx + "
                    f"{len(exported['textures'])} textures in {STATE['output_dir']}")
                write_status("DONE", produced=new_assets,
                             output_dir=str(STATE["output_dir"]),
                             fbx=exported["meshes"],
                             textures=exported["textures"],
                             mh_manifest=exported["manifest_path"])
            STATE["finished"] = True
            # Quit the editor so the launcher's process actually terminates.
            # Without this, UE idles forever after the build completes and
            # the pipeline isn't truly unattended.
            try:
                log("requesting editor quit")
                unreal.SystemLibrary.quit_editor()
            except Exception as e:
                log(f"  quit_editor failed (will rely on launcher): {e}")
            return

        if elapsed > STATE["timeout_s"]:
            log(f"TIMEOUT after {STATE['timeout_s']}s")
            write_status("FAILED", error="timeout", high_res=hi_res, face_tex=faces, can_build=can_build)
            _stop()
            try: unreal.SystemLibrary.quit_editor()
            except Exception: pass
            return

    except Exception as e:
        log(f"on_tick exception: {e}\n{traceback.format_exc()}")
        write_status("FAILED", error=f"tick: {e}")
        _stop()


def _stop() -> None:
    h = STATE["tick_handle"]
    if h is not None:
        try:
            unreal.unregister_slate_post_tick_callback(h)
        except Exception:
            pass
        STATE["tick_handle"] = None


def main() -> int:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = argv[1:]
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--status", required=True, help="path to JSON status file")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--output-dir", default="C:/tmp/mh/out",
                   help="disk folder where the DCC-pipeline FBX+textures land")
    p.add_argument("--name", default=None,
                   help="override for the output name; defaults to the asset's leaf name")
    p.add_argument("--pipeline", default="cinematic",
                   choices=["cinematic", "optimized", "dcc"],
                   help="which MetaHumanDefaultPipelineType to build with")
    args = p.parse_args(argv)

    STATE["asset"] = args.asset
    STATE["status_path"] = args.status
    STATE["timeout_s"] = args.timeout
    STATE["started_at"] = time.time()
    STATE["new_skel_before"] = list_skel()
    STATE["output_dir"] = args.output_dir
    STATE["output_name"] = args.name or args.asset.rsplit("/", 1)[-1].split(".")[0]
    STATE["pipeline"] = args.pipeline
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    write_status("STARTING")

    subsystem = unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
    if subsystem is None:
        write_status("FAILED", error="MetaHumanCharacterEditorSubsystem not available")
        return 2
    STATE["subsystem"] = subsystem

    character = unreal.load_asset(args.asset)
    if character is None:
        write_status("FAILED", error=f"asset not found: {args.asset}")
        return 2
    if not isinstance(character, unreal.MetaHumanCharacter):
        write_status("FAILED", error=f"wrong class: {type(character).__name__}")
        return 2
    STATE["character"] = character

    log(f"asset: {args.asset}")
    log(f"try_add_object_to_edit: {subsystem.try_add_object_to_edit(character)}")
    log(f"can_build_meta_human (initial): {subsystem.can_build_meta_human(character)}")

    # Auto-rigging: freshly created MetaHumanCharacter assets (e.g. a new
    # Ada 5.7 made by the user with no rig yet) need request_auto_rigging
    # before textures/build can happen. It's a cloud call that uses the
    # editor's active Epic account session. Skip if already rigged.
    # Skip the cloud rig request only if the character already has the
    # full ARKit blendshape rig (we read this off the SkeletalMesh's
    # morph_targets count - a joints-only rig has 0, a joints+blendshapes
    # rig has ~178 raw DNA shapes that include the 52 ARKit ones).
    already_blendshape_rig = False
    try:
        # The face mesh follows the convention SKM_<Char>_FaceMesh, baked
        # under /Game/<Char>/Face/.
        face_path = f"/Game/{STATE['output_name']}/Face/SKM_{STATE['output_name']}_FaceMesh"
        face_skm = unreal.EditorAssetLibrary.load_asset(face_path)
        if face_skm:
            morphs = face_skm.get_editor_property("morph_targets") or []
            already_blendshape_rig = len(morphs) > 0
            log(f"existing face mesh morph_targets={len(morphs)}")
    except Exception as e:
        log(f"  rig-state probe failed (assuming not rigged): {e}")
    if not already_blendshape_rig:
        # Ask the cloud rig service for joints + blendshapes, NOT just
        # joints (default). Without this, the resulting SkeletalMesh
        # has no morph targets and downstream stages have nothing to
        # carry through to the GLB - the head won't be drivable from
        # ARKit / LiveLink Face / MediaPipe.
        try:
            rig_params = unreal.MetaHumanCharacterAutoRiggingRequestParams()
            rig_params.set_editor_property(
                "rig_type", unreal.MetaHumanRigType.JOINTS_AND_BLEND_SHAPES)
            rig_params.set_editor_property("report_progress", True)
            rig_params.set_editor_property("blocking", False)
            log("firing request_auto_rigging (rig_type=JOINTS_AND_BLEND_SHAPES) ...")
            subsystem.request_auto_rigging(character, rig_params)
        except Exception as e:
            # Older bindings may take positional-only args; try the
            # legacy single-arg form as a fallback (still joints-only).
            log(f"  request_auto_rigging(params) failed: {e}; trying bare call")
            try:
                subsystem.request_auto_rigging(character)
            except Exception as e2:
                log(f"  request_auto_rigging bare failed: {e2}")
                write_status("FAILED", error=f"auto_rigging: {e2}")
                return 1
        write_status("RIGGING")
        STATE["awaiting_rig"] = True
        STATE["rig_started"] = time.time()
    else:
        log("face mesh already has morph targets; skipping request_auto_rigging")
        STATE["awaiting_rig"] = False

    # Fire texture sources request — but only if we're NOT awaiting rig.
    # When awaiting_rig is True, the tick callback fires this after
    # is_auto_rigged flips (Epic cloud's auto_rig deposits an initial set
    # of source textures and we don't want to race the request).
    if not STATE.get("awaiting_rig"):
        log("firing request_texture_sources ...")
        try:
            try:
                params = unreal.MetaHumanCharacterTextureRequestParams()
                subsystem.request_texture_sources(character, params)
            except Exception as e1:
                log(f"  request_texture_sources(params) failed: {e1}, trying bare call")
                subsystem.request_texture_sources(character)
        except Exception as e:
            write_status("FAILED", error=f"request_texture_sources: {e}")
            return 1
        STATE["texture_request_fired"] = True
        write_status("REQUESTED")

    # Register tick callback and return. Editor keeps ticking; callback
    # drives the remainder of the state machine.
    STATE["tick_handle"] = unreal.register_slate_post_tick_callback(on_tick)
    log("tick callback registered; main thread returning")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as _e:
        # Log ANY uncaught error to a crash file and to UE log. Prior runs
        # failed silently (no status.json) because main() threw before the
        # first write_status call.
        import traceback as _tb
        _tb_s = _tb.format_exc()
        try:
            log(f"FATAL: {_e}\n{_tb_s}")
        except Exception:
            pass
        try:
            from pathlib import Path as _P
            _P("C:/tmp/mh/crash.txt").write_text(
                f"FATAL: {_e}\n\n{_tb_s}", encoding="utf-8")
        except Exception:
            pass
        raise
