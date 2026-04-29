"""Stage 03 / 5.7 native-glb - headless GLB export.

Invoked as:
    blender --background <char>.blend --python export_glb.py -- \
        --char <id> --workspace <abs>

Pipeline-specific notes vs 5.6/cinematic:
  * Stage 02 already wired hair / eyebrow / lash materials manually
    (sidecar atlases + synthesized hair color + alpha clip), so we
    don't need an mh_materials.json sidecar to flow through to a
    three.js viewer. The viewer in stage 04 is Bevy/WASM and just
    consumes the .glb directly.
  * Hair-card materials carry a Multiply node feeding Base Color
    (synth color * root-darkening ramp from Attribute.B). Blender's
    glTF exporter only writes a constant Base Color factor for
    materials whose BC graph isn't a direct Image Texture, so the
    ramp gets silently dropped. We pre-bake those small Multiply
    chains to a single PNG per material and rewire BC to the baked
    image before export.

Inputs:  characters/<id>/02-blend/<id>.blend (opened by Blender)
         _config/pipeline.yaml -> glb_constraints.{max_texture_px, draco_compression}
Outputs: characters/<id>/03-glb/<id>.glb
         characters/<id>/03-glb/glb_manifest.json
         Updates characters/<id>/manifest.json (stages.03_glb_export)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

import bpy


def _log(msg):
    print(f"[stage03-glb] {msg}", flush=True)


def _parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    return p.parse_args(argv)


def _iso_now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_glb_constraints(workspace):
    """Tiny flat YAML reader for the `glb_constraints:` block."""
    cfg = workspace / "_config" / "pipeline.yaml"
    out = {"max_texture_px": 1024, "draco_compression": True,
           "target_tri_budget": 60000}
    if not cfg.exists():
        return out
    in_block = False
    for raw in cfg.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_block = line.strip().startswith("glb_constraints:")
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"')
        if val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        else:
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out


def _delete_hidden_meshes():
    """Drop meshes hidden from viewport/render so they don't get exported."""
    removed = []
    for obj in list(bpy.data.objects):
        if obj.type != "MESH":
            continue
        if obj.hide_viewport or obj.hide_render:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    return removed


# MetaHuman normal maps need DX -> GL conversion (+Y down -> +Y up) for
# glTF spec compliance, but only some of them. Empirically:
#
#   * MI_Face_Skin / MI_Body_Baked / MI_Eye*_Baked normals come through
#     UE's GLTFExporter ALREADY in OpenGL convention (the MH plugin's
#     bake handles the flip during export). Re-flipping here would
#     invert pores and wrinkles in browsers.
#   * Outfit normals (MID_M_DG_*) keep DirectX convention, so they DO
#     need flipping for spec-compliant rendering.
#
# Hair Attribute / Coverage atlases are NOT normal maps even though
# their names contain words like "Tangent" - skip them.
_NORMAL_HINTS = ("_n.png", "_n.tga", "_normal.png", "_normal.tga",
                 "_normal_map", "_normal_")
_NORMAL_SKIP = ("attribute", "coverage", "rootuvseed",
                "cardsatlas_tangent",  # tangent direction map, not normal
                "t_flatnormal", "t_skinmicronormal",
                # MH-baked skin/eye/body normals are already +Y up
                "mi_face_skin", "mi_body_baked",
                "mi_eyel_baked", "mi_eyer_baked")


def _is_normal_image(img):
    name = (img.name or "").lower()
    if any(s in name for s in _NORMAL_SKIP):
        return False
    if any(h in name for h in _NORMAL_HINTS):
        return True
    return name.endswith("_normal") or "_normal_" in name


def _flip_g_inplace(img):
    import numpy as np
    if img.size[0] == 0 or img.size[1] == 0:
        return False
    w, h = img.size[0], img.size[1]
    buf = np.empty(w * h * 4, dtype=np.float32)
    try:
        img.pixels.foreach_get(buf)
    except Exception as e:
        _log(f"  G-flip foreach_get failed for {img.name}: {e}")
        return False
    buf[1::4] = 1.0 - buf[1::4]
    img.pixels.foreach_set(buf)
    img.update()
    return True


def _flip_normal_maps_g():
    touched = 0
    for img in bpy.data.images:
        if not _is_normal_image(img):
            continue
        if _flip_g_inplace(img):
            _log(f"  normal G-flip: {img.name} ({img.size[0]}x{img.size[1]})")
            touched += 1
    return touched


def _per_image_cap(name, default_cap):
    n = name.lower()
    # Teeth fits ~2% of screen; 256 is plenty.
    if n.startswith("t_teeth"):
        return 256
    return default_cap


def _downsample_images(max_px):
    """Scale every image > its cap down (preserving aspect). After
    scaling, repack the image so the glTF exporter uses the in-memory
    pixel buffer rather than re-reading the un-downsampled PNG from
    `filepath_raw`."""
    touched = 0
    for img in bpy.data.images:
        if img.size[0] == 0 or img.size[1] == 0:
            continue
        w, h = img.size[0], img.size[1]
        biggest = max(w, h)
        cap = _per_image_cap(img.name, max_px)
        if biggest <= cap:
            continue
        factor = cap / biggest
        new_w = max(1, int(w * factor))
        new_h = max(1, int(h * factor))
        _log(f"  downsample {img.name}: {w}x{h} -> {new_w}x{new_h} (cap={cap})")
        img.scale(new_w, new_h)
        # Detach from disk source so the glTF exporter can't fall back
        # to the original-size file. Pack the (now smaller) pixel
        # buffer into the .blend so it becomes the source of truth.
        try:
            img.filepath_raw = ""
            img.filepath = ""
            img.pack()
        except Exception as e:
            _log(f"    pack {img.name} failed: {e}")
        touched += 1
    return touched


def _flatten_card_basecolor(out_dir, max_px):
    """Pre-bake Multiply-node chains feeding Base Color into a single
    PNG per hair-card / eyebrow material so the glTF exporter ships
    the actual color we set up in stage 02 (synth tone with root
    darkening) instead of dropping back to a constant.

    For each *_CardMat material whose Base Color isn't a direct Image
    Texture node, we:
      1. Create a new sRGB image at the per-image cap size.
      2. Set Cycles bake target to that image.
      3. Bake the diffuse-color pass which evaluates the BC graph.
      4. Rewire BC <- new baked image. The Alpha link from the
         Attribute texture stays untouched.

    Returns the list of materials that got flattened."""
    flattened = []

    scene = bpy.context.scene
    prev_engine = scene.render.engine
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.use_pass_color = True
    scene.render.bake.margin = 4

    bake_dir = out_dir / "_baked"
    bake_dir.mkdir(parents=True, exist_ok=True)

    try:
        for mat in list(bpy.data.materials):
            if not mat.use_nodes or mat.users <= 0:
                continue
            nm = mat.name.lower()
            if not nm.endswith("_cardmat"):
                continue
            nt = mat.node_tree
            bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                continue
            bc_input = bsdf.inputs.get("Base Color")
            if not bc_input or not bc_input.is_linked:
                continue
            src_node = bc_input.links[0].from_node
            if src_node.type == "TEX_IMAGE":
                continue  # already a direct texture; glTF handles it

            host_obj = None
            for obj in bpy.data.objects:
                if obj.type != "MESH":
                    continue
                if any(slot.material is mat for slot in obj.material_slots):
                    host_obj = obj
                    break
            if host_obj is None:
                _log(f"  flatten {mat.name}: no host mesh found, skipping")
                continue

            cap = _per_image_cap(mat.name, max_px)
            img_name = f"{mat.name}_BC_baked"
            img = bpy.data.images.new(img_name, width=cap, height=cap, alpha=False)
            img.colorspace_settings.name = "sRGB"

            target_node = nt.nodes.new("ShaderNodeTexImage")
            target_node.image = img
            target_node.location = (1000, -300)
            for n in nt.nodes:
                n.select = False
            target_node.select = True
            nt.nodes.active = target_node

            bpy.ops.object.select_all(action="DESELECT")
            host_obj.select_set(True)
            bpy.context.view_layer.objects.active = host_obj

            try:
                bpy.ops.object.bake(type="DIFFUSE", use_clear=True, margin=4)
            except Exception as e:
                _log(f"  flatten bake {mat.name} failed: {e}")
                nt.nodes.remove(target_node)
                continue

            try:
                img.filepath_raw = str(bake_dir / f"{img_name}.png")
                img.file_format = "PNG"
                img.save()
                img.pack()
            except Exception as e:
                _log(f"  flatten save {img_name} failed: {e}")

            for link in list(bc_input.links):
                nt.links.remove(link)
            nt.links.new(target_node.outputs["Color"], bc_input)

            _log(f"  flattened {mat.name} BC -> {img_name} ({cap}x{cap})")
            flattened.append(mat.name)
    finally:
        scene.render.engine = prev_engine

    return flattened


def _count_tris():
    total = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        me = obj.data
        me.calc_loop_triangles()
        total += len(me.loop_triangles)
    return total


def _current_max_image_px():
    m = 0
    for img in bpy.data.images:
        if img.size[0] == 0:
            continue
        m = max(m, img.size[0], img.size[1])
    return m


def _emit_sidecar_for_viewer(char_dir, out_dir):
    """Forward stage 02's mh_materials.json + the alpha textures it
    references from 01-glb/textures/ into 03-glb/. Stage 04's
    build_site.py copies whatever's in 03-glb/{mh_materials.json,
    textures/*} into docs/characters/<id>/, where viewer.js fetches
    them to drive the hair / lash shader injection."""
    import shutil
    src_map = char_dir / "02-blend" / "mh_materials.json"
    if not src_map.exists():
        _log("no 02-blend/mh_materials.json - skipping viewer sidecar")
        return
    dst_map = out_dir / "mh_materials.json"
    shutil.copy2(src_map, dst_map)

    # Walk the mapping, collect every alpha_stem + textures.alpha ref,
    # copy each from 01-glb/textures/ into 03-glb/textures/.
    mapping = json.loads(src_map.read_text(encoding="utf-8"))
    src_tex_dir = char_dir / "01-glb" / "textures"
    dst_tex_dir = out_dir / "textures"
    dst_tex_dir.mkdir(parents=True, exist_ok=True)
    needed = set()
    for m in mapping.get("materials", []):
        p = m.get("params") or {}
        t = m.get("textures") or {}
        if p.get("alpha_stem"):
            needed.add(p["alpha_stem"])
        for ref in t.values():
            if isinstance(ref, str) and "/" in ref:
                # "textures/foo.png" -> "foo"
                stem = os.path.splitext(os.path.basename(ref))[0]
                needed.add(stem)
            elif isinstance(ref, str):
                stem = os.path.splitext(ref)[0]
                needed.add(stem)
    copied = 0
    for stem in sorted(needed):
        src = src_tex_dir / f"{stem}.png"
        if not src.exists():
            _log(f"  sidecar miss: {src}")
            continue
        dst = dst_tex_dir / f"{stem}.png"
        shutil.copy2(src, dst)
        copied += 1
    _log(f"sidecar: {dst_map.name} + {copied} texture(s) -> {out_dir}")


def _propagate_mesh_target_names_to_primitives(glb_path):
    """Blender's glTF exporter writes morph target names at the mesh
    level (`mesh.extras.targetNames`), but three.js's GLTFLoader only
    reads them at the primitive level (`primitive.extras.targetNames`).
    Without this propagation, every imported mesh comes in with an
    empty `morphTargetDictionary` even though the morph data is
    correctly serialized - the blendshape panel ends up with zero
    sliders because there are no names to key off of.

    Rewrite the GLB chunk in place: parse JSON header, copy
    `mesh.extras.targetNames` into each `primitive.extras.targetNames`
    on that mesh, repack."""
    import io, struct
    with open(glb_path, "rb") as f:
        magic, version, length = struct.unpack("<III", f.read(12))
        json_len, json_type = struct.unpack("<II", f.read(8))
        json_bytes = f.read(json_len)
        bin_chunk = f.read()  # everything after the JSON chunk

    gltf = json.loads(json_bytes)
    propagated = 0
    for mesh in gltf.get("meshes", []):
        names = mesh.get("extras", {}).get("targetNames")
        if not names:
            continue
        for prim in mesh.get("primitives", []):
            if "targets" not in prim:
                continue
            extras = prim.setdefault("extras", {})
            if extras.get("targetNames") == names:
                continue
            extras["targetNames"] = names
            propagated += 1

    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    # JSON chunk must be 4-byte aligned, padded with spaces.
    pad = (4 - len(new_json) % 4) % 4
    new_json += b" " * pad
    new_bin = bin_chunk  # bin chunk header is included; its 4-byte
                         # alignment is already correct from Blender.
    new_length = 12 + 8 + len(new_json) + len(new_bin)
    with open(glb_path, "wb") as f:
        f.write(struct.pack("<III", magic, version, new_length))
        f.write(struct.pack("<II", len(new_json), json_type))
        f.write(new_json)
        f.write(new_bin)
    _log(f"propagated targetNames onto {propagated} primitive(s)")


def _update_char_manifest(char_dir, status, errors):
    path = char_dir / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stage = data.setdefault("stages", {}).setdefault("03_glb_export", {})
    now = _iso_now()
    if status == "done":
        stage["started_at"] = stage.get("started_at") or now
        stage["completed_at"] = now
    else:
        stage["started_at"] = stage.get("started_at") or now
        stage["completed_at"] = None
    stage["status"] = status
    stage["errors"] = errors
    stage.setdefault("output_dir", "03-glb/")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main():
    args = _parse_args()
    workspace = Path(args.workspace)
    char_dir = workspace / "characters" / args.char
    out_dir = char_dir / "03-glb"
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / f"{args.char}.glb"
    manifest_path = out_dir / "glb_manifest.json"

    constraints = _read_glb_constraints(workspace)
    max_px = int(constraints.get("max_texture_px", 1024))
    draco = bool(constraints.get("draco_compression", True))

    try:
        removed = _delete_hidden_meshes()
        _log(f"removed {len(removed)} hidden meshes")

        flipped = _flip_normal_maps_g()
        _log(f"flipped G on {flipped} normal maps (DX -> GL)")

        downsampled = _downsample_images(max_px)
        _log(f"downsampled {downsampled} textures (max {max_px}px)")

        flattened = _flatten_card_basecolor(out_dir, max_px)
        _log(f"flattened {len(flattened)} card-material BC chains")

        kept_meshes = [o.name for o in bpy.data.objects if o.type == "MESH"]
        materials = [m.name for m in bpy.data.materials if m.users > 0]
        images = [i.name for i in bpy.data.images
                  if i.users > 0 and i.size[0] > 0]

        bpy.ops.object.select_all(action="DESELECT")
        for o in bpy.data.objects:
            if o.type in ("MESH", "ARMATURE"):
                o.select_set(True)

        _log(f"exporting {glb_path} (draco={draco})")
        bpy.ops.export_scene.gltf(
            filepath=str(glb_path),
            export_format="GLB",
            use_visible=True,
            export_apply=True,
            export_yup=True,
            export_image_format="AUTO",
            export_draco_mesh_compression_enable=draco,
            export_draco_mesh_compression_level=6,
            export_skins=True,
            export_morph=True,
            export_morph_normal=True,
            export_morph_tangent=False,
            export_cameras=False,
            export_lights=False,
        )

        # Forward the viewer sidecar (mh_materials.json + alpha textures)
        # from 02-blend/ + 01-glb/textures/ into 03-glb/. Stage 04's
        # build_site.py copies these into docs/ so viewer.js can load
        # them as alphaMap targets and run the hair-card / lash shader.
        _emit_sidecar_for_viewer(char_dir, out_dir)

        # Patch the GLB so three.js's GLTFLoader can find morph target
        # names (it looks per-primitive; Blender writes per-mesh).
        _propagate_mesh_target_names_to_primitives(glb_path)

        tri_count = _count_tris()
        max_img_px = _current_max_image_px()
        file_size = glb_path.stat().st_size

        manifest = {
            "character_id": args.char,
            "pipeline": "native-glb",
            "saved_at": _iso_now(),
            "glb_path": f"03-glb/{args.char}.glb",
            "file_size_bytes": file_size,
            "tri_count": tri_count,
            "mesh_count": len(kept_meshes),
            "material_count": len(materials),
            "image_count": len(images),
            "max_texture_px_used": max_img_px,
            "normal_maps_g_flipped": flipped,
            "downsampled_images": downsampled,
            "flattened_card_materials": flattened,
            "draco": draco,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _log(f"wrote {manifest_path}")
        _log(f"glb {file_size/1_000_000:.1f} MB, "
             f"{tri_count:,} tris, max {max_img_px}px, "
             f"{len(materials)} mats, {len(images)} imgs")

        _update_char_manifest(char_dir, "done", [])
    except Exception as exc:
        import traceback
        _log(f"FAILED: {exc}\n{traceback.format_exc()}")
        _update_char_manifest(char_dir, "failed", [str(exc)])
        raise


if __name__ == "__main__":
    main()
