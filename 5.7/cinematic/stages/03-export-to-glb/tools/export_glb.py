"""Stage 03 — headless GLB export.

Invoked as:
    blender --background <char>.blend --python export_glb.py -- \
        --char <id> --workspace <abs>

Inputs:  characters/<id>/02-blend/<id>.blend (opened by blender before this runs)
         _config/pipeline.yaml (glb_constraints)
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


# ---------------------------------------------------------------------------
# Helpers

def _parse_args() -> argparse.Namespace:
    # Blender eats its own argv before "--"; scripts see argv after it.
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    return p.parse_args(argv)


def _read_glb_constraints(workspace: Path) -> dict:
    """Tiny flat YAML reader for the `glb_constraints:` block — no yaml dep."""
    cfg = workspace / "_config" / "pipeline.yaml"
    out = {
        "max_texture_px": 2048,
        "draco_compression": True,
        "target_tri_budget": 60000,
        "skip_groom": True,
    }
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


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Scene prep

def _delete_hidden_meshes() -> list[str]:
    """Remove meshes hidden from viewport/render (non-LOD0) so they don't export."""
    removed: list[str] = []
    for obj in list(bpy.data.objects):
        if obj.type != "MESH":
            continue
        if obj.hide_viewport or obj.hide_render:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    # Purge orphan data blocks so the glTF exporter doesn't pack their images.
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    return removed


# MetaHuman normal maps are authored in DirectX convention (+Y down). The glTF
# 2.0 spec mandates OpenGL convention (+Y up) for normal maps, so every glTF-
# compliant renderer (three.js, Babylon, Bevy, Godot, model-viewer, Unity's
# glTF importer) reads the G channel as +Y up. Without this flip, surface
# detail renders inverted — pores read as bumps, wrinkles as ridges.
#
# Match on filename because that's what stage 02 also keys on when classifying
# textures into the "normal" / "detail_normal" slots. Keeping the list in sync
# with stage 02's `_classify_texture` by hand is a stage-boundary cost we
# accept (no cross-stage imports).
_NORMAL_FILENAME_HINTS = (
    "_n.tga", "_n.png",
    "_normal.tga", "_normal.png",
    "_normal_map",
    "normal_main",
)
# Placeholder/utility normal-ish textures referenced by MH materials but not
# actually wired as normals in the exported materials — skip so we don't touch
# data we don't need to.
_NORMAL_FILENAME_SKIP = (
    "t_flatnormal.tga",
    "t_skinmicronormal.tga",
)


def _is_normal_image(img) -> bool:
    name = (img.name or "").lower()
    if any(s in name for s in _NORMAL_FILENAME_SKIP):
        return False
    return any(h in name for h in _NORMAL_FILENAME_HINTS)


def _flip_g_inplace(img) -> bool:
    """Flip the G channel of an image in place (DX -> OpenGL normal convention).
    Uses foreach_get/foreach_set with numpy — direct pixel list access is orders
    of magnitude slower on 2K+ textures."""
    import numpy as np
    if img.size[0] == 0 or img.size[1] == 0:
        return False
    w, h = img.size[0], img.size[1]
    ch = 4  # Blender images expose 4 float channels regardless of source
    buf = np.empty(w * h * ch, dtype=np.float32)
    try:
        img.pixels.foreach_get(buf)
    except Exception as exc:  # noqa: BLE001
        print(f"[stage03][normal] foreach_get failed for {img.name}: {exc}", flush=True)
        return False
    buf[1::4] = 1.0 - buf[1::4]
    img.pixels.foreach_set(buf)
    img.update()
    return True


def _flip_normal_maps_g() -> int:
    """Flip G on every normal-map image in the scene (UE is DX, glTF is GL)."""
    touched = 0
    for img in bpy.data.images:
        if not _is_normal_image(img):
            continue
        if _flip_g_inplace(img):
            print(f"[stage03] normal G-flip: {img.name} ({img.size[0]}x{img.size[1]})", flush=True)
            touched += 1
    return touched


def _per_image_cap(name: str, default_cap: int) -> int:
    """Per-texture cap override. Teeth atlas covers ~2% of screen and includes
    the tongue pack — 256px is plenty there."""
    n = name.lower()
    if n.startswith("t_teeth"):
        return 256
    return default_cap


def _downsample_images(max_px: int) -> int:
    """Scale any image texture whose max side > its cap down to the cap (preserving aspect)."""
    touched = 0
    for img in bpy.data.images:
        if img.size[0] == 0 or img.size[1] == 0:
            continue  # unloaded / generated
        w, h = img.size[0], img.size[1]
        biggest = max(w, h)
        cap = _per_image_cap(img.name, max_px)
        if biggest <= cap:
            continue
        factor = cap / biggest
        new_w = max(1, int(w * factor))
        new_h = max(1, int(h * factor))
        print(f"[stage03] downsample {img.name}: {w}x{h} -> {new_w}x{new_h} (cap={cap})", flush=True)
        img.scale(new_w, new_h)
        touched += 1
    return touched




def _emit_sidecar_textures_and_mapping(char_dir: Path, out_dir: Path) -> None:
    """Copy stage-02's material mapping next to the GLB and save every referenced
    texture as a PNG into `textures/`. Rewrites texture refs in the mapping to
    point at the sidecar path (relative to the GLB), so the web viewer can just
    `new THREE.TextureLoader().load(spec.textures.mask)` etc."""
    src_mapping = char_dir / "02-blend" / "mh_materials.json"
    if not src_mapping.exists():
        print(f"[stage03][tex] no {src_mapping} — stage 02 pre-mapping; skipping", flush=True)
        return

    mapping = json.loads(src_mapping.read_text(encoding="utf-8"))
    stems: set[str] = set()
    for m in mapping.get("materials", []):
        for _, stem in (m.get("textures") or {}).items():
            if stem:
                stems.add(stem)

    tex_dir = out_dir / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    url_by_stem: dict[str, str] = {}

    for stem in sorted(stems):
        # Blender image names come from the source filename as-imported, usually
        # including the extension. Try a few likely spellings.
        candidates = [stem, f"{stem}.tga", f"{stem}.png", f"{stem}.jpg", f"{stem}.TGA"]
        img = next((bpy.data.images.get(c) for c in candidates if bpy.data.images.get(c)), None)
        if img is None:
            print(f"[stage03][tex] WARN: no image datablock for '{stem}'", flush=True)
            continue
        out_path = tex_dir / f"{stem}.png"
        # Stash and restore format/filepath so we don't leak state back into the scene.
        prev_fmt = img.file_format
        prev_fp = img.filepath_raw
        try:
            img.file_format = "PNG"
            img.save(filepath=str(out_path))
        except Exception as exc:  # noqa: BLE001
            print(f"[stage03][tex] FAIL saving {stem}: {exc}", flush=True)
            continue
        finally:
            img.file_format = prev_fmt
            img.filepath_raw = prev_fp
        url_by_stem[stem] = f"textures/{stem}.png"
        print(f"[stage03][tex] {stem} -> {out_path.name} ({img.size[0]}x{img.size[1]})", flush=True)

    # Rewrite mapping to use sidecar URLs.
    for m in mapping.get("materials", []):
        tex = m.get("textures") or {}
        for role, stem in list(tex.items()):
            if stem in url_by_stem:
                tex[role] = url_by_stem[stem]
            # else leave as-is — web viewer will skip unknown refs gracefully.
    out_map = out_dir / "mh_materials.json"
    out_map.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"[stage03][tex] wrote {out_map} ({len(url_by_stem)} textures sidecar'd)", flush=True)


def _count_tris() -> int:
    total = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        me = obj.data
        # Use loop_triangles which reflects actual triangulation.
        me.calc_loop_triangles()
        total += len(me.loop_triangles)
    return total


def _current_max_image_px() -> int:
    m = 0
    for img in bpy.data.images:
        if img.size[0] == 0:
            continue
        m = max(m, img.size[0], img.size[1])
    return m


# ---------------------------------------------------------------------------
# Manifest I/O

def _update_char_manifest(char_dir: Path, status: str, errors: list[str]) -> None:
    path = char_dir / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stage = data.setdefault("stages", {}).setdefault("03_glb_export", {})
    now = _iso_now()
    if status == "done":
        if not stage.get("started_at"):
            stage["started_at"] = now
        stage["completed_at"] = now
    else:
        stage["started_at"] = stage.get("started_at") or now
        stage["completed_at"] = None
    stage["status"] = status
    stage["errors"] = errors
    stage.setdefault("output_dir", "03-glb/")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    args = _parse_args()
    workspace = Path(args.workspace)
    char_dir = workspace / "characters" / args.char
    out_dir = char_dir / "03-glb"
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / f"{args.char}.glb"
    manifest_path = out_dir / "glb_manifest.json"

    constraints = _read_glb_constraints(workspace)
    max_px = int(constraints.get("max_texture_px", 2048))
    draco = bool(constraints.get("draco_compression", True))
    tri_budget = int(constraints.get("target_tri_budget", 60000))

    try:
        removed = _delete_hidden_meshes()
        print(f"[stage03] removed {len(removed)} hidden meshes", flush=True)

        # Flip DX -> GL on normal maps BEFORE downsample so both the embedded
        # GLB textures and the sidecar PNGs end up spec-compliant in one pass.
        flipped = _flip_normal_maps_g()
        print(f"[stage03] flipped G on {flipped} normal maps (DX -> GL)", flush=True)

        downsampled = _downsample_images(max_px)
        print(f"[stage03] downsampled {downsampled} textures (max {max_px}px)", flush=True)

        kept_meshes = [o.name for o in bpy.data.objects if o.type == "MESH"]
        materials = [m.name for m in bpy.data.materials if m.users > 0]
        images = [i.name for i in bpy.data.images if i.users > 0 and i.size[0] > 0]

        # Select everything we want exported; exporter uses use_visible too but
        # setting selection is belt-and-suspenders.
        bpy.ops.object.select_all(action="DESELECT")
        for o in bpy.data.objects:
            if o.type in ("MESH", "ARMATURE"):
                o.select_set(True)

        print(f"[stage03] exporting {glb_path} (draco={draco})", flush=True)
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
            # Morph targets enabled so the face carries its 52 ARKit-named
            # shape keys (transplanted in stage 02 by apply_arkit52.py from a
            # frozen reference). The raw ~822 DNA action units would balloon
            # the GLB to ~350 MiB per character; 52 ARKit keys keep it at
            # ~20 MiB and are directly drivable from LiveLink-Face JSON.
            export_morph=True,
            export_morph_normal=True,
            export_morph_tangent=False,  # tangents blow up size with no visual gain on MH faces
            export_cameras=False,
            export_lights=False,
        )

        # Emit sidecar textures + rewritten mapping for the three.js viewer.
        # glTF embeds only the textures the exporter understands (direct Principled
        # BSDF inputs); MH cloth mask/detail/macro/AO live inside Mix chains so we
        # write them out as plain PNGs alongside the GLB and have the web viewer
        # load them explicitly based on the mapping.
        _emit_sidecar_textures_and_mapping(char_dir, out_dir)

        tri_count = _count_tris()
        max_img_px = _current_max_image_px()
        file_size = glb_path.stat().st_size

        manifest = {
            "character_id": args.char,
            "glb_path": f"03-glb/{args.char}.glb",
            "file_size_bytes": file_size,
            "tri_count": tri_count,
            "mesh_count": len(kept_meshes),
            "material_count": len(materials),
            "image_count": len(images),
            "max_texture_px_used": max_img_px,
            "normal_maps_g_flipped": flipped,
            "draco": draco,
            "tri_budget": tri_budget,
            "over_budget": tri_count > tri_budget,
            "exported_meshes": kept_meshes,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[stage03] wrote {glb_path} ({file_size/1_048_576:.1f} MiB, {tri_count} tris)", flush=True)
        print(f"[stage03] wrote {manifest_path}", flush=True)

        _update_char_manifest(char_dir, "done", [])
        print("[stage03] char manifest updated: status=done", flush=True)
        return 0

    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(f"[stage03] FAILED: {exc}\n{tb}", flush=True)
        try:
            _update_char_manifest(char_dir, "failed", [str(exc)])
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
