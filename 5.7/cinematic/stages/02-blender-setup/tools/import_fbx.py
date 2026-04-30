"""
Stage 02 v0 — import stage 01 FBXs into a clean Blender scene and save a .blend.

Runs headless:
    blender --background --python import_fbx.py -- --char ada --workspace "<abs>"

No materials, no cleanup, no armature merge — this is the "does it load?" step.
Reads characters/<id>/01-fbx/mh_manifest.json, imports every mesh FBX, reports
what ended up in the scene, writes characters/<id>/02-blend/<id>.blend.
"""

import argparse
import datetime as _dt
import json
import os
import sys

import bpy

# Make sibling modules importable when Blender runs this as a -P script.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import apply_arkit52  # noqa: E402  — sibling module; see apply_arkit52.py
import apply_arkit52_grooms  # noqa: E402  — propagates face keys to beard/brows/etc.


def _iso_now():
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    return p.parse_args(argv)


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _import_fbx(fbx_abs):
    bpy.ops.import_scene.fbx(filepath=fbx_abs)


def _classify_texture(filename):
    """Return which PBR channel a texture file should feed: 'basecolor' | 'normal' |
    'roughness' | 'ao' | None. Heuristic by filename. Skips LOD variants (we only
    wire materials on LOD0 meshes) and wrinkle-map secondary normals."""
    n = filename.lower()
    # LOD variants target lower-LOD materials we're not wiring in v0
    if n.endswith("_lod.tga") or n.endswith("_lod1.tga"):
        return None
    # Wrinkle-map secondary normals — not the primary normal
    if "_wm1" in n or "_wm2" in n or "_wm3" in n:
        return None
    # UE tileable fabric overlays for cloth shaders — not the primary PBR
    # input for a slot, but they carry the small-scale weave pattern (denim
    # micro-weave, knit macro-heather, etc.) that differentiates garments.
    # Route them to dedicated roles so the cloth shader can tile + combine
    # them on top of the flat color1/color2 mask blend.
    if n.startswith("micro_") or n.startswith("macro_"):
        # memory_* are wrinkle overlays driven by a live rig — ignore for GLB.
        # pilling_* are a macro dust/lint texture; multiplying it in kills the
        # garment so skip for v1 (future: route to a subtle variation channel).
        if "pilling" in n:
            return None
        if n.endswith("_n.tga") or "_normal" in n:
            return "detail_normal"
        if n.endswith("_h.tga"):
            return None    # height-only micro; not used in v1
        if n.startswith("macro_"):
            return "macro_overlay"
        # micro_*_diffuse / other micro tile albedo → detail diffuse
        return "detail_diffuse"
    if n.startswith("memory_"):
        return None
    # Placeholder/utility textures referenced by MH materials but not meant to drive PBR
    if n in ("whitesquaretexture.tga", "black_masks.tga",
             "t_flatnormal.tga", "t_skinmicronormal.tga"):
        return None
    # MH hair-card data-packed atlases — NOT albedo.
    # _CardsAtlas_Tangent: RG=tangent XY in card UV space, used for
    # anisotropic hair specular. Route as 'tangent_atlas' so the web
    # viewer can sample it via KHR_materials_anisotropy.
    if "cardsatlas_tangent" in n:
        return "tangent_atlas"
    # _ColorXYDepthGroupID / _TangentStrandU — alternative tangent packings
    # we don't currently consume; skip for now.
    if "colorxydepthgroupid" in n or "tangentstrandu" in n:
        return None
    # MH 5.6 "compact" hair atlas — _CardsAtlas_Attribute packs coverage in the
    # R channel (vs the legacy _RootUVSeedCoverage which uses A). Eyebrows and
    # other 5.6 grooms that ship without a legacy coverage texture rely on this.
    if "cardsatlas_attribute" in n:
        return "alpha_r"
    # MH hair-card atlas (stored on the GroomAsset, exported in stage 01 as part
    # of the groom dep walk). Coverage packs silhouette opacity in the A channel.
    # Must precede the generic 'coverage' basecolor fallback below.
    if "rootuvseedcoverage" in n:
        return "alpha"
    # Eyelash coverage atlas — silhouette mask, not albedo. R channel holds the
    # mask shape; route as alpha so the eyelash card clips to strand outlines.
    if "eyelashes" in n and "coverage" in n:
        return "alpha"
    # Eye iris — explicit basecolor fallback before generic matchers
    if n.startswith("t_iris_"):
        return "basecolor"
    # Other 'coverage' textures (not handled above) default to basecolor
    if "coverage" in n:
        return "basecolor"
    if any(s in n for s in ("basecolor", "color_main", "color_map", "_diffuse", "diffuse")):
        return "basecolor"
    if (n.endswith("_n.tga") or "normal_main" in n
            or "_normal_map" in n or "_normal.tga" in n):
        return "normal"
    if "roughness" in n:
        return "roughness"
    # 5.7 Optimized packs Specular/Roughness/Metallic/Fuzz into SRMF(_VT) and
    # teeth ships SRM. G channel = roughness; route to the roughness role so
    # the skin shader's roughness input gets a meaningful map.
    if n.endswith("_srmf.tga") or n.endswith("_srm.tga"):
        return "roughness"
    if n.endswith("_ao.tga") or "cavity" in n:
        return "ao"
    # 5.7 Head/Body Scatter maps: subsurface depth / thickness modulation.
    # NOT albedo and NOT cavity. The skin shader multiplies BC × AO to get
    # pore/crease darkening; feeding the neutral-gray scatter texture there
    # washes basecolor out to gray. Leave scatter unclassified so the skin
    # shader's AO multiply stays off, letting the baked BC show full.
    # (Dedicated SSS input can come later if the viewer needs one.)
    if n.endswith("_scatter.tga"):
        return None
    # MH cloth masks: packed region selector between diffuse_color_1 and
    # diffuse_color_2 (R = primary blend factor). Named `<item>_Mask.tga`.
    if n.endswith("_mask.tga"):
        return "mask"
    return None


def _basecolor_score(filename):
    """Rank basecolor candidates when a material references multiple. MH bodies
    expose both `BodyBaseColor.tga` (canonical albedo) and
    `female_underwear_color_map.tga` (painted-on underwear overlay). Both get
    classified as basecolor; the canonical file wins via higher score.

    5.7 note: T_Face_Basecolor_Animated_CM{1,2,3} are per-expression
    multiplier layers that are ~white at the neutral (CM1) pose and
    would render the face as plain white if picked as the primary
    albedo. T_Head_LOD{N}_BaseColor is the real baked skin atlas.
    Rank LOD-baked HIGHER than CM animated."""
    n = filename.lower()
    # Demote 5.7 CM/WM animated multipliers — they are additive layers,
    # not albedo on their own.
    if "_animated_cm" in n or "_animated_wm" in n:
        return 20
    # Prefer the 5.6-style iris packed atlas (T_Iris_A_M, B_M etc.) over
    # the 5.7 per-char baked photo (T_EyeIris{L,R}_BC) so the eye
    # procedural shader gets the channels it expects.
    if n.startswith("t_iris_") and n.endswith("_m.tga"):
        return 130
    if "basecolor" in n:
        # 5.7 Cinematic ships the canonical MAIN skin atlas as
        # T_Head_BaseColor / T_Body_BaseColor (8192x8192 source, no LOD
        # suffix), plus lower-res per-LOD bakes. Prefer the MAIN over
        # any LOD-specific variant.
        is_lod = "_lod" in n
        is_head_or_body_main = (
            (n.startswith("t_head_") or n.startswith("t_body_"))
            and not is_lod
        )
        if is_head_or_body_main:
            return 125
        if "_lod1" in n:   return 115
        if "_lod3" in n:   return 110
        if "_lod5" in n or "_lod5to7" in n: return 105
        return 100
    if "color_main" in n:
        return 90
    if n.startswith("t_iris_") or "coverage" in n:
        return 80
    if "_diffuse" in n or n.endswith(".tga") and "diffuse" in n:
        return 70
    if "color_map" in n:
        return 10   # weak — avoid over-preferring overlays like underwear_color_map
    return 50


def _pick_mi_basecolor(params):
    """Derive an albedo RGBA from a MaterialInstance's parameter block. MH uses
    different parameter conventions per asset class:
      - Outfits: `diffuse_color_1` (primary), sometimes `_2` secondary; also C_color.
      - Hair (cards & facial): no albedo param — color is synthesized from
        `hairMelanin` + `hairRedness` + optional `hairDye` multiplier.
      - Generic/baked: BaseColor / Tint / Albedo.
    Returns (r,g,b,a) or None when no plausible source is present."""
    if not params:
        return None
    vecs = params.get("vectors") or {}
    scalars = params.get("scalars") or {}
    lower = {k.lower(): v for k, v in vecs.items()}

    # Explicit albedo/tint parameters (baked MIs, some generic materials)
    for key in ("basecolor", "base color", "color", "tint", "albedo",
                "diffuse_color_1", "c_color"):
        if key in lower:
            v = lower[key]
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                return (float(v[0]), float(v[1]), float(v[2]),
                        float(v[3]) if len(v) >= 4 else 1.0)

    # MH hair fallback: synthesize from melanin / redness / dye multiplier
    if "hairMelanin" in scalars:
        t = max(0.0, min(1.0, float(scalars["hairMelanin"])))
        light = (1.0 - t) ** 1.5
        r = 0.55 * light + 0.02 * (1.0 - light)
        g = 0.40 * light + 0.01 * (1.0 - light)
        b = 0.25 * light + 0.005 * (1.0 - light)
        red = float(scalars.get("hairRedness", 0.0))
        r += red * 0.12
        dye = vecs.get("hairDye") or lower.get("hairdye")
        if dye and len(dye) >= 3:
            r *= float(dye[0]); g *= float(dye[1]); b *= float(dye[2])
        clamp = lambda x: max(0.0, min(1.0, x))
        return (clamp(r), clamp(g), clamp(b), 1.0)

    return None


def _pick_mi_color(params, key):
    """Fetch a named vector-parameter from an MI param block as RGBA tuple.
    Used to read MH cloth secondary diffuse (`diffuse_color_2`) and similar
    named color params that don't fall through the albedo-key cascade in
    _pick_mi_basecolor. Returns None when absent or malformed."""
    if not params:
        return None
    vecs = params.get("vectors") or {}
    lower = {k.lower(): v for k, v in vecs.items()}
    v = lower.get(key.lower())
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return (float(v[0]), float(v[1]), float(v[2]),
                float(v[3]) if len(v) >= 4 else 1.0)
    return None


def _pick_mi_roughness(params, material_kind):
    """Return a 0..1 roughness scalar derived from a MaterialInstance's scalar
    params, or a sane per-kind default. MH default shading is PBR so Principled
    BSDF default 0.5 ends up looking too shiny for cloth + too matte for skin.
    Key scalars by kind:
      - cloth: 'C_roughness value' (shirt), else default high
      - hair:  'HairRoughness' (cards) or 'Roughness' (facial)
      - generic/body: no param → return None, let basecolor drive defaults."""
    if not params:
        return None
    scalars = params.get("scalars") or {}
    lower = {k.lower(): v for k, v in scalars.items()}
    for key in ("roughness", "hairroughness", "c_roughness value"):
        if key in lower:
            try:
                return max(0.0, min(1.0, float(lower[key])))
            except Exception:
                pass
    if material_kind == "cloth":
        return 0.75          # cloth should not be specular by default
    if material_kind == "hair":
        return 0.55
    return None


def _classify_material_kind(mat_path, comp):
    """Return 'body' | 'face' | 'cloth' | 'hair' | 'generic' — used to pick per-kind
    defaults (specular / roughness) when MI params don't spell them out."""
    c = (comp or "").lower()
    m = (mat_path or "").lower()
    if "hair" in c or "hair" in m or "cardsmesh" in c:
        return "hair"
    if any(k in c for k in ("shirt", "top", "slacks", "btm", "bottom", "flats", "shoe", "gloves")):
        return "cloth"
    if "body" in c or "body" in m:
        return "body"
    if "face" in c or "head" in m:
        return "face"
    return "generic"


def _classify_face_slot(slot_name, mat_path):
    """Fine-grained face accessory slot kind. Face meshes expose ~15 material
    slots (head/teeth/saliva/eyeLeft/eyeRight/eyeshell/eyelashes/eyeEdge/
    cartilage + LOD duplicates). Only 'head_skin' uses the full skin shader;
    the rest need tiny flat-default materials because UE's MIs for these slots
    ship no textures — color is baked into MI scalar/vector params.
    Returns one of the slot-kind strings below, or None."""
    s = (slot_name or "").lower()
    m = (mat_path or "").lower()
    if ("head_shader" in s or "headsynthesized" in m
            or "head_lod" in s):
        return "head_skin"
    if "teeth" in s or "teeth" in m:
        return "teeth"
    if "eyeleft" in s or "eyeright" in s or "eyerefractive" in m:
        return "eye_refractive"
    if "eyeshell" in s or "eyeocclusion" in m:
        return "eye_occlusion"
    if "eyelash" in s or "eyelash" in m:
        return "eyelashes"
    if "saliva" in s or "eyeedge" in s or "lacrimal" in m:
        return "wet"
    if "cartilage" in s or "cartilage" in m:
        return "cartilage"
    return None


def _bone_forward_for_slot(mesh_obj, slot_index):
    """Return (origin, gaze_axis) for the eye bone owning this slot's
    geometry, both in world space.

    `origin` is `FACIAL_{L,R}_EyeParallel.head` (center of the eyeball).
    `gaze_axis` is `normalize(Pupil.head - EyeParallel.head)` —
    points OUT through the pupil, 1cm long.

    Returns ((ox,oy,oz), (ax,ay,az)) or None."""
    import mathutils as _mu
    me = mesh_obj.data
    # Collect dominant vertex group per vertex for polys in this slot.
    slot_verts = set()
    for p in me.polygons:
        if p.material_index == slot_index:
            slot_verts.update(p.vertices)
    if not slot_verts:
        return None
    # Find the most-used vertex group across slot_verts.
    vg_votes = {}
    for vi in slot_verts:
        v = me.vertices[vi]
        if not v.groups:
            continue
        best = max(v.groups, key=lambda g: g.weight)
        if best.weight <= 0.5:
            continue
        vg_votes[best.group] = vg_votes.get(best.group, 0) + 1
    if not vg_votes:
        return None
    winner_vg_idx = max(vg_votes, key=vg_votes.get)
    vg_name = mesh_obj.vertex_groups[winner_vg_idx].name
    # Find the armature that drives this mesh (parent OR Armature modifier).
    arm = None
    for m in mesh_obj.modifiers:
        if m.type == "ARMATURE" and m.object and m.object.type == "ARMATURE":
            arm = m.object; break
    if arm is None and mesh_obj.parent and mesh_obj.parent.type == "ARMATURE":
        arm = mesh_obj.parent
    if arm is None:
        return None
    eye_bone = arm.data.bones.get(vg_name) if hasattr(arm.data, "bones") else None
    if eye_bone is None:
        for b in arm.data.bones:
            if b.name == vg_name:
                eye_bone = b; break
    if eye_bone is None:
        return None
    pupil_name = None
    en = eye_bone.name
    if "_EyeParallel" in en:
        pupil_name = en.replace("_EyeParallel", "_Pupil")
    elif en.endswith("_Eye"):
        pupil_name = en[:-4] + "_Pupil"
    pupil = arm.data.bones.get(pupil_name) if pupil_name else None
    eye_head = arm.matrix_world @ eye_bone.head_local
    if pupil is not None:
        pup_head = arm.matrix_world @ pupil.head_local
        axis = pup_head - eye_head
        if axis.length > 1e-6:
            axis.normalize()
            return ((eye_head.x, eye_head.y, eye_head.z),
                    (axis.x, axis.y, axis.z))
    tail = arm.matrix_world @ eye_bone.tail_local
    axis = tail - eye_head
    if axis.length <= 1e-6:
        return None
    axis.normalize()
    return ((eye_head.x, eye_head.y, eye_head.z),
            (axis.x, axis.y, axis.z))


def _compute_eye_pole_uv(mesh_obj, slot_index):
    """Find the UV coordinate of the forward pole of an eyeball for the given
    material slot. MH eye meshes are a closed sphere wrapped by a planar UV
    projection: the forward pole (iris center in world space) maps to a
    specific UV point that differs per-eye (Ada: left ~(0.477, 0.512), right
    ~(0.525, 0.507)). The iris mask + radial pupil mask are computed from UV
    distance to this pole, so it has to be detected accurately at import time.

    Strategy: derive the forward direction from the eye bone the slot's
    vertices are weighted to, then weighted-average UVs of polys whose
    normals face that direction. Falls back to 6-axis heuristic if we
    can't resolve a bone."""
    me = mesh_obj.data
    if not me.uv_layers.active:
        return (0.5, 0.5)
    uv_layer = me.uv_layers.active.data

    slot_polys = [p for p in me.polygons if p.material_index == slot_index]
    if not slot_polys:
        print(f"[stage02] eye pole: no polys with material_index={slot_index} "
              f"on {mesh_obj.name}, using (0.5,0.5)", flush=True)
        return (0.5, 0.5)

    def weighted_uv(fx, fy, fz):
        u_sum = 0.0; v_sum = 0.0; w_sum = 0.0
        for p in slot_polys:
            d = p.normal[0] * fx + p.normal[1] * fy + p.normal[2] * fz
            if d <= 0.85:
                continue
            w = (d - 0.85) ** 3
            pu = 0.0; pv = 0.0; n = 0
            for li in p.loop_indices:
                uv = uv_layer[li].uv
                pu += uv[0]; pv += uv[1]; n += 1
            if n == 0:
                continue
            pu /= n; pv /= n
            u_sum += pu * w; v_sum += pv * w; w_sum += w
        if w_sum <= 0.0:
            return None
        return (u_sum / w_sum, v_sum / w_sum)

    # Build a 10x10 UV-density map from poly centroids. Used to both
    # sanity-check the forward-direction result and to provide a
    # fallback for scattered-UV meshes (5.7 Optimized slot 3).
    BUCKETS = 10
    bins = {}
    for p in slot_polys:
        pu_c = 0.0; pv_c = 0.0; n = 0
        for li in p.loop_indices:
            uv = uv_layer[li].uv
            pu_c += uv[0]; pv_c += uv[1]; n += 1
        if n == 0:
            continue
        pu_c /= n; pv_c /= n
        kx = max(0, min(BUCKETS - 1, int(pu_c * BUCKETS)))
        ky = max(0, min(BUCKETS - 1, int(pv_c * BUCKETS)))
        key = (kx, ky)
        rec = bins.setdefault(key, [0, 0.0, 0.0])
        rec[0] += 1; rec[1] += pu_c; rec[2] += pv_c

    def _bucket_count_near(pu, pv):
        kx = max(0, min(BUCKETS - 1, int(pu * BUCKETS)))
        ky = max(0, min(BUCKETS - 1, int(pv * BUCKETS)))
        total = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (kx + dx, ky + dy)
                if k in bins:
                    total += bins[k][0]
        return total

    # Primary: find the poly whose world-space centroid is CLOSEST to
    # the Pupil-axis tip (eyeball_center + gaze_axis * R). That poly is
    # physically at the iris center of the eyeball sphere, so its UV is
    # the iris pole regardless of how scattered the UV layout is.
    bone_info = _bone_forward_for_slot(mesh_obj, slot_index)
    if bone_info is not None:
        import mathutils as _mu
        (ox, oy, oz), (ax, ay, az) = bone_info
        origin = _mu.Vector((ox, oy, oz))
        axis = _mu.Vector((ax, ay, az))
        best_poly = None; best_dot = None
        for p in slot_polys:
            cw = mesh_obj.matrix_world @ p.center
            # project centroid onto gaze axis; maximum projection = frontmost poly.
            proj = (cw - origin).dot(axis)
            if best_dot is None or proj > best_dot:
                best_dot = proj; best_poly = p
        if best_poly is not None:
            pu_c = 0.0; pv_c = 0.0; n = 0
            for li in best_poly.loop_indices:
                uv = uv_layer[li].uv
                pu_c += uv[0]; pv_c += uv[1]; n += 1
            if n > 0:
                pu_c /= n; pv_c /= n
                print(f"[stage02] eye pole: {mesh_obj.name} slot {slot_index} → "
                      f"UV=({pu_c:.4f},{pv_c:.4f}) via frontmost-poly "
                      f"origin=({ox:.3f},{oy:.3f},{oz:.3f}) "
                      f"axis=({ax:.2f},{ay:.2f},{az:.2f}) proj={best_dot:.4f}",
                      flush=True)
                return (pu_c, pv_c)

    # Fallback: try all 6 axis-aligned directions (5.6 heuristic).
    candidates = []
    for fx, fy, fz in ((1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)):
        r = weighted_uv(fx, fy, fz)
        if r is None:
            continue
        pu_c, pv_c = r
        dist = ((pu_c - 0.5) ** 2 + (pv_c - 0.5) ** 2) ** 0.5
        candidates.append((dist, pu_c, pv_c, (fx, fy, fz)))
    candidates.sort(key=lambda x: x[0])
    for _, pu_c, pv_c, fw in candidates:
        # Accept this direction if the UV lands in a dense cluster of
        # this slot's polys (≥10% of total). Otherwise it's a
        # misdirected centroid (e.g. center of a scattered UV layout)
        # and the radial mask would miss the mesh entirely.
        near = _bucket_count_near(pu_c, pv_c)
        if near >= max(10, int(len(slot_polys) * 0.10)):
            print(f"[stage02] eye pole: {mesh_obj.name} slot {slot_index} → "
                  f"UV=({pu_c:.4f},{pv_c:.4f}) forward={fw} density_check={near} polys",
                  flush=True)
            return (pu_c, pv_c)
    # Fallback: densest UV bucket centroid (covers scattered-UV meshes).
    if bins:
        best_key = max(bins, key=lambda k: bins[k][0])
        cnt, su, sv = bins[best_key]
        pu = su / cnt; pv = sv / cnt
        print(f"[stage02] eye pole: {mesh_obj.name} slot {slot_index} → "
              f"UV=({pu:.4f},{pv:.4f}) from densest bucket {best_key} ({cnt} polys) "
              f"[forward directions all landed in sparse UV]",
              flush=True)
        return (pu, pv)
    print(f"[stage02] eye pole: no UV data on {mesh_obj.name} slot {slot_index}, "
          f"using (0.5,0.5)", flush=True)
    return (0.5, 0.5)


def _build_face_accessory_material(name, slot_kind, textures_root, assignments,
                                   mesh_obj=None, slot_index=-1, params=None):
    """Small flat-default materials for MH face accessory slots (teeth, saliva,
    cartilage, eye refractive/occlusion, eyelashes, eyeEdge). UE's MIs for these
    slots ship only color-tint/roughness scalars (no textures), so we recreate
    plausible defaults per slot type. The head skin slot is NOT handled here —
    see _build_skin_material."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        nt.nodes.remove(node)
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (400, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (0, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def _load(rel, noncolor=False):
        p = os.path.join(textures_root, rel)
        if not os.path.exists(p): return None
        try: img = bpy.data.images.load(p, check_existing=True)
        except Exception: return None
        if noncolor:
            try: img.colorspace_settings.name = "Non-Color"
            except Exception: pass
        return img

    def _set(k, v):
        if k in bsdf.inputs:
            try: bsdf.inputs[k].default_value = v
            except Exception: pass

    def _enable_alpha_clip():
        try: mat.surface_render_method = "DITHERED"
        except Exception: pass
        try: mat.blend_method = "HASHED"
        except Exception: pass
        try: mat.shadow_method = "HASHED"
        except Exception: pass

    def _enable_alpha_blend():
        try: mat.surface_render_method = "BLENDED"
        except Exception: pass
        try: mat.blend_method = "BLEND"
        except Exception: pass

    if slot_kind == "teeth":
        # MH's M_Teeth parent material is a procedural shader we can't
        # reconstruct cleanly, but the project ships a LOD-friendly simplified
        # bake under /Game/MetaHumans/Common/Face/Textures/Simplified/ that
        # packs teeth + gums + tongue into one atlas matching the teeth mesh
        # UVs. Stage 01's sweep exports them as .tga; we load them here as
        # image datablocks so stage 03's sidecar pass finds them, and record
        # them on the material spec so the web viewer can PBR-sample them.
        _set("Base Color", (0.85, 0.80, 0.72, 1.0))
        _set("Roughness", 0.35)
        _set("Subsurface Weight", 0.10)
        _set("Subsurface Radius", (1.0, 0.4, 0.2))
        _set("Subsurface Scale", 0.005)
        for k in ("Specular IOR Level", "Specular"):
            if k in bsdf.inputs: bsdf.inputs[k].default_value = 0.5; break
        # Preload the simplified bake textures into bpy.data.images. We don't
        # wire them into the Blender shader (the viewer does the sampling
        # directly), we just need them present so stage 03 sidecars them.
        # use_fake_user=True keeps them alive across .blend save/load even
        # though no node references them.
        for rel in ("T_Teeth_BaseColor_Baked.tga",
                    "T_Teeth_Normal_Baked.tga",
                    "T_Teeth_Specular_Baked.tga",
                    "T_Teeth_mouthOcc.tga"):
            img = _load(rel, noncolor=(rel != "T_Teeth_BaseColor_Baked.tga"))
            if img is not None:
                img.use_fake_user = True
        return mat

    if slot_kind == "eye_refractive":
        # MH eye is a fully data-driven UE shader we can't reproduce 1:1 (palette
        # sampling from IrisColorPicker + MI IrisColor1U/V params that aren't in
        # our texture export). Clean-room rebuild:
        #
        #   T_Iris_A_M.tga channels (measured empirically, EXTEND sampling):
        #     R — iris fibril detail (continuous ~0.25..0.75)
        #     G — hard inverted iris mask  (unused here)
        #     B — SOFT gradient out of iris edge → used for LIMBUS DARKENING
        #     A — sharp iris mask (unused here; we mask by radial UV instead)
        #
        #   Pole UV: iris center in UV space, detected per-eye from mesh
        #   geometry (forward vertex of the eyeball sphere). We UV-space-scale
        #   the texture 4× around the pole so the iris disc fills an area of
        #   ~0.13 UV radius, matching the separate radial iris/sclera mask.
        #
        #   Color ramps (user-tuned):
        #     • limbus B→ramp:  0.095 black → 1.0 white (how deep the iris
        #       darkens toward the outer iris edge)
        #     • radial iris/sclera: 0.131 black → 0.146 white (hard boundary)
        #     • radial pupil:       0.022 white  → 0.046 black (pupil disc)
        UV_SCALE  = 4.0
        IRIS      = (0.176, 0.077, 0.045, 1.0)
        IRIS_DARK = (0.036, 0.015, 0.000, 1.0)
        SCLERA    = (0.92,  0.90,  0.86,  1.0)
        PUPIL     = (0.005, 0.005, 0.005, 1.0)

        iris_img = None
        if assignments.get("basecolor"):
            iris_img = _load(assignments["basecolor"], noncolor=True)

        if iris_img is None or mesh_obj is None:
            # No iris texture available or we don't know which mesh this slot
            # belongs to — fall back to flat sclera (better than green eyes).
            _set("Base Color", SCLERA)
            _set("Roughness", 0.25)
            _set("IOR", 1.5)
            mat.blend_method = "OPAQUE"
            return mat

        pu, pv = _compute_eye_pole_uv(mesh_obj, slot_index)

        texc = nt.nodes.new("ShaderNodeTexCoord"); texc.location = (-1700, 0)

        # Scaled UV → iris texture lookup (pole pins to texture center 0.5,0.5).
        mapn = nt.nodes.new("ShaderNodeMapping"); mapn.location = (-1450, 300)
        mapn.inputs["Location"].default_value = (0.5 - UV_SCALE * pu,
                                                 0.5 - UV_SCALE * pv, 0.0)
        mapn.inputs["Scale"].default_value = (UV_SCALE, UV_SCALE, 1.0)
        nt.links.new(texc.outputs["UV"], mapn.inputs["Vector"])

        tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-1200, 300)
        tex.image = iris_img
        try: tex.extension = "EXTEND"
        except Exception: pass
        nt.links.new(mapn.outputs["Vector"], tex.inputs["Vector"])

        sep = nt.nodes.new("ShaderNodeSeparateColor"); sep.location = (-950, 300)
        nt.links.new(tex.outputs["Color"], sep.inputs["Color"])

        # Fibril detail from R channel: MapRange 0.25..0.75 → 0.65..1.15 clamped.
        mr = nt.nodes.new("ShaderNodeMapRange"); mr.location = (-720, 500)
        mr.inputs["From Min"].default_value = 0.25
        mr.inputs["From Max"].default_value = 0.75
        mr.inputs["To Min"].default_value   = 0.65
        mr.inputs["To Max"].default_value   = 1.15
        try: mr.clamp = True
        except Exception: pass
        nt.links.new(sep.outputs["Red"], mr.inputs["Value"])

        fibril_rgb = nt.nodes.new("ShaderNodeCombineColor"); fibril_rgb.location = (-500, 500)
        nt.links.new(mr.outputs["Result"], fibril_rgb.inputs["Red"])
        nt.links.new(mr.outputs["Result"], fibril_rgb.inputs["Green"])
        nt.links.new(mr.outputs["Result"], fibril_rgb.inputs["Blue"])

        iris_n = nt.nodes.new("ShaderNodeRGB"); iris_n.location = (-500, 250)
        iris_n.outputs[0].default_value = IRIS

        # iris_color × fibril_gray (MULTIPLY, full factor).
        iris_mul = nt.nodes.new("ShaderNodeMix"); iris_mul.data_type = "RGBA"
        iris_mul.blend_type = "MULTIPLY"; iris_mul.location = (-240, 400)
        iris_mul.inputs[0].default_value = 1.0
        nt.links.new(iris_n.outputs[0], iris_mul.inputs[6])
        nt.links.new(fibril_rgb.outputs["Color"], iris_mul.inputs[7])

        # Limbus darkening: B → ramp → mix iris_mul to iris_dark.
        brmp = nt.nodes.new("ShaderNodeValToRGB"); brmp.location = (-500, 50)
        brmp.color_ramp.elements[0].position = 0.095
        brmp.color_ramp.elements[0].color = (0, 0, 0, 1)
        brmp.color_ramp.elements[1].position = 1.0
        brmp.color_ramp.elements[1].color = (1, 1, 1, 1)
        nt.links.new(sep.outputs["Blue"], brmp.inputs["Fac"])

        idark_n = nt.nodes.new("ShaderNodeRGB"); idark_n.location = (-500, -150)
        idark_n.outputs[0].default_value = IRIS_DARK

        limbus = nt.nodes.new("ShaderNodeMix"); limbus.data_type = "RGBA"
        limbus.location = (0, 200)
        nt.links.new(brmp.outputs["Color"], limbus.inputs[0])
        nt.links.new(iris_mul.outputs[2], limbus.inputs[6])
        nt.links.new(idark_n.outputs[0], limbus.inputs[7])

        # Radial UV distance from pole — drives iris/sclera + pupil masks.
        sxyz = nt.nodes.new("ShaderNodeSeparateXYZ"); sxyz.location = (-1450, -400)
        nt.links.new(texc.outputs["UV"], sxyz.inputs["Vector"])
        du = nt.nodes.new("ShaderNodeMath"); du.operation = "SUBTRACT"; du.location = (-1200, -300)
        nt.links.new(sxyz.outputs["X"], du.inputs[0]); du.inputs[1].default_value = pu
        dv = nt.nodes.new("ShaderNodeMath"); dv.operation = "SUBTRACT"; dv.location = (-1200, -500)
        nt.links.new(sxyz.outputs["Y"], dv.inputs[0]); dv.inputs[1].default_value = pv
        du2 = nt.nodes.new("ShaderNodeMath"); du2.operation = "MULTIPLY"; du2.location = (-1000, -300)
        nt.links.new(du.outputs[0], du2.inputs[0]); nt.links.new(du.outputs[0], du2.inputs[1])
        dv2 = nt.nodes.new("ShaderNodeMath"); dv2.operation = "MULTIPLY"; dv2.location = (-1000, -500)
        nt.links.new(dv.outputs[0], dv2.inputs[0]); nt.links.new(dv.outputs[0], dv2.inputs[1])
        s2  = nt.nodes.new("ShaderNodeMath"); s2.operation  = "ADD";      s2.location  = (-820, -400)
        nt.links.new(du2.outputs[0], s2.inputs[0]); nt.links.new(dv2.outputs[0], s2.inputs[1])
        rd  = nt.nodes.new("ShaderNodeMath"); rd.operation  = "SQRT";     rd.location  = (-640, -400)
        nt.links.new(s2.outputs[0], rd.inputs[0])

        # Iris-vs-sclera hard ramp: radial 0.131 black → 0.146 white.
        isr = nt.nodes.new("ShaderNodeValToRGB"); isr.location = (-400, -200)
        isr.color_ramp.elements[0].position = 0.131
        isr.color_ramp.elements[0].color = (0, 0, 0, 1)
        isr.color_ramp.elements[1].position = 0.146
        isr.color_ramp.elements[1].color = (1, 1, 1, 1)
        nt.links.new(rd.outputs[0], isr.inputs["Fac"])

        scle_n = nt.nodes.new("ShaderNodeRGB"); scle_n.location = (-400, -400)
        scle_n.outputs[0].default_value = SCLERA

        # Procedural sclera veins — MH UE shader generates these from a vein
        # noise driven by VeinsPower / VeinsRotate scalars (NO bitmap in the
        # export since it's procedural). We reproduce the effect with a
        # Voronoi distance-to-edge (cell boundaries = thin branching web,
        # which reads as veins) in OBJECT coordinates so the pattern is
        # stable relative to each eyeball under skinning. Masked to the
        # sclera region (reuse iris/sclera ramp) and modulated by VeinsPower.
        VEIN_COLOR = (0.62, 0.18, 0.15, 1.0)
        veins_power = 0.5
        if params:
            try:
                veins_power = float((params.get("scalars") or {})
                                    .get("VeinsPower", veins_power))
            except Exception:
                pass
        vor = nt.nodes.new("ShaderNodeTexVoronoi"); vor.location = (-1100, -900)
        try: vor.feature = "DISTANCE_TO_EDGE"
        except Exception: pass
        vor.inputs["Scale"].default_value = 35.0
        nt.links.new(texc.outputs["Object"], vor.inputs["Vector"])
        vrmp = nt.nodes.new("ShaderNodeValToRGB"); vrmp.location = (-880, -900)
        vrmp.color_ramp.elements[0].position = 0.0
        vrmp.color_ramp.elements[0].color = (1, 1, 1, 1)
        vrmp.color_ramp.elements[1].position = 0.04
        vrmp.color_ramp.elements[1].color = (0, 0, 0, 1)
        nt.links.new(vor.outputs["Distance"], vrmp.inputs["Fac"])
        # Only in sclera: multiply vein mask by (iris/sclera fac).
        vm = nt.nodes.new("ShaderNodeMath"); vm.operation = "MULTIPLY"
        vm.location = (-650, -900)
        nt.links.new(vrmp.outputs["Color"], vm.inputs[0])
        nt.links.new(isr.outputs["Color"], vm.inputs[1])
        vm2 = nt.nodes.new("ShaderNodeMath"); vm2.operation = "MULTIPLY"
        vm2.location = (-450, -900)
        nt.links.new(vm.outputs[0], vm2.inputs[0])
        vm2.inputs[1].default_value = max(0.0, min(1.0, veins_power))
        vcn = nt.nodes.new("ShaderNodeRGB"); vcn.location = (-450, -1100)
        vcn.outputs[0].default_value = VEIN_COLOR
        # Blend sclera -> vein color by mask.
        scle_veined = nt.nodes.new("ShaderNodeMix"); scle_veined.data_type = "RGBA"
        scle_veined.location = (-150, -600)
        nt.links.new(vm2.outputs[0], scle_veined.inputs[0])
        nt.links.new(scle_n.outputs[0], scle_veined.inputs[6])
        nt.links.new(vcn.outputs[0], scle_veined.inputs[7])

        iors = nt.nodes.new("ShaderNodeMix"); iors.data_type = "RGBA"
        iors.location = (200, 0)
        nt.links.new(isr.outputs["Color"], iors.inputs[0])
        nt.links.new(limbus.outputs[2], iors.inputs[6])
        nt.links.new(scle_veined.outputs[2], iors.inputs[7])

        # Pupil radial ramp (inverted): 0.022 white → 0.046 black. fac=1 at
        # center (blends to pupil), fac=0 outside (keeps iris_or_sclera).
        pr = nt.nodes.new("ShaderNodeValToRGB"); pr.location = (-400, -620)
        pr.color_ramp.elements[0].position = 0.022
        pr.color_ramp.elements[0].color = (1, 1, 1, 1)
        pr.color_ramp.elements[1].position = 0.046
        pr.color_ramp.elements[1].color = (0, 0, 0, 1)
        nt.links.new(rd.outputs[0], pr.inputs["Fac"])

        pup_n = nt.nodes.new("ShaderNodeRGB"); pup_n.location = (200, -300)
        pup_n.outputs[0].default_value = PUPIL

        final = nt.nodes.new("ShaderNodeMix"); final.data_type = "RGBA"
        final.location = (450, 0)
        nt.links.new(pr.outputs["Color"], final.inputs[0])
        nt.links.new(iors.outputs[2], final.inputs[6])
        nt.links.new(pup_n.outputs[0], final.inputs[7])

        nt.links.new(final.outputs[2], bsdf.inputs["Base Color"])
        _set("Roughness", 0.25)
        _set("IOR", 1.5)
        for k in ("Specular IOR Level", "Specular"):
            if k in bsdf.inputs: bsdf.inputs[k].default_value = 0.6; break
        mat.blend_method = "OPAQUE"
        return mat

    if slot_kind == "eye_occlusion":
        # Fully invisible via a pure Transparent BSDF. Principled +
        # alpha=0 was still rendering a white sheet in Blender's
        # material-preview viewport because Eevee Next ignores HASHED
        # alpha below a threshold and BLEND keeps specular samples.
        # Transparent BSDF is guaranteed invisible in both engines.
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out2 = nt.nodes.new("ShaderNodeOutputMaterial"); out2.location = (400, 0)
        tr = nt.nodes.new("ShaderNodeBsdfTransparent"); tr.location = (0, 0)
        tr.inputs["Color"].default_value = (1, 1, 1, 1)
        nt.links.new(tr.outputs["BSDF"], out2.inputs["Surface"])
        try: mat.blend_method = "BLEND"
        except Exception: pass
        try: mat.surface_render_method = "BLENDED"
        except Exception: pass
        try: mat.shadow_method = "NONE"
        except Exception: pass
        return mat

    if slot_kind == "eyelashes":
        # Dark strands; alpha from the coverage texture's Alpha channel.
        _set("Base Color", (0.015, 0.010, 0.008, 1.0))
        _set("Roughness", 0.6)
        for k in ("Specular IOR Level", "Specular"):
            if k in bsdf.inputs: bsdf.inputs[k].default_value = 0.25; break
        if assignments.get("alpha"):
            img = _load(assignments["alpha"], noncolor=True)
            if img:
                tn = nt.nodes.new("ShaderNodeTexImage"); tn.image = img
                tn.location = (-400, 0)
                if "Alpha" in bsdf.inputs:
                    nt.links.new(tn.outputs["Alpha"], bsdf.inputs["Alpha"])
        _enable_alpha_clip()
        return mat

    if slot_kind == "wet":
        # Tear film / lacrimal fluid / waterline. The 0.25 alpha layer
        # sat directly over the iris in the full face render; combined
        # with the eye-occlusion shell's 0.35 layer the effect was a
        # milky film that washed out the iris colour. Make it fully
        # transparent for now.
        _set("Base Color", (0.9, 0.88, 0.85, 1.0))
        _set("Roughness", 0.05)
        _set("Alpha", 0.0)
        _set("IOR", 1.336)                                # tear fluid IOR
        for k in ("Specular IOR Level", "Specular"):
            if k in bsdf.inputs: bsdf.inputs[k].default_value = 0.6; break
        _enable_alpha_blend()
        return mat

    if slot_kind == "cartilage":
        # 5.6 ships a small tear-duct inner-tissue mesh (~200 polys). 5.7
        # Optimized gives this slot ~1200 polys of geometry that renders
        # as a big pink mass over the eye socket when opaque. Make it
        # fully transparent — the skin behind covers that region anyway.
        _set("Base Color", (0.45, 0.18, 0.18, 1.0))
        _set("Roughness", 0.45)
        _set("Alpha", 0.0)
        _enable_alpha_blend()
        return mat

    return mat


def _build_skin_material(name, textures_root, assignments, material_kind="face"):
    """Clean-room MH skin shader. Principled BSDF with:
      - Base Color = albedo × cavity (cavity map multiplies into pores/creases)
      - Tangent-space Normal from _Normal map
      - Roughness from _Roughness map (fallback 0.45 matte skin)
      - Subsurface scattering: red-biased radius, weight 0.15, IOR 1.4
    Ignores the UE rig-animated CM*/WM* color+wrinkle slots — those need a live
    facial rig to drive their mix factors; irrelevant for static GLB delivery.
    Ignores T_SkinMicroNormal tiling (second UV, non-portable to glTF in v0)."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        nt.nodes.remove(node)

    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (200, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def _load(rel, noncolor=False):
        p = os.path.join(textures_root, rel)
        if not os.path.exists(p):
            return None
        try:
            img = bpy.data.images.load(p, check_existing=True)
        except Exception:
            return None
        if noncolor:
            try: img.colorspace_settings.name = "Non-Color"
            except Exception: pass
        return img

    # --- base color × cavity multiply ------------------------------------- #
    bc_tex = None
    if assignments.get("basecolor"):
        img = _load(assignments["basecolor"])
        if img:
            bc_tex = nt.nodes.new("ShaderNodeTexImage")
            bc_tex.image = img; bc_tex.location = (-800, 300)

    ao_tex = None
    if assignments.get("ao"):
        img = _load(assignments["ao"], noncolor=True)
        if img:
            ao_tex = nt.nodes.new("ShaderNodeTexImage")
            ao_tex.image = img; ao_tex.location = (-800, 0)

    if bc_tex and ao_tex:
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs["Fac"].default_value = 0.85   # partial AO tint
        mix.location = (-400, 200)
        nt.links.new(bc_tex.outputs["Color"], mix.inputs["Color1"])
        nt.links.new(ao_tex.outputs["Color"], mix.inputs["Color2"])
        nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    elif bc_tex:
        nt.links.new(bc_tex.outputs["Color"], bsdf.inputs["Base Color"])

    # --- roughness -------------------------------------------------------- #
    if assignments.get("roughness"):
        img = _load(assignments["roughness"], noncolor=True)
        if img:
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img; tn.location = (-800, -300)
            nt.links.new(tn.outputs["Color"], bsdf.inputs["Roughness"])
    else:
        bsdf.inputs["Roughness"].default_value = 0.45

    # --- normal (base + micro-detail mixed) ------------------------------ #
    base_nm_out = None
    if assignments.get("normal"):
        img = _load(assignments["normal"], noncolor=True)
        if img:
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img; tn.location = (-800, -600)
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-400, -600); nm.inputs["Strength"].default_value = 1.0
            nt.links.new(tn.outputs["Color"], nm.inputs["Color"])
            base_nm_out = nm.outputs["Normal"]

    # Micro-detail normal (pore-scale high-frequency) — tileable texture that the
    # MH pipeline uses to break up the mid-scale normal. Not a channel in the
    # manifest's assignments dict; hard-coded to T_SkinMicroNormal.tga which stage
    # 01 always exports. Mapping tiles it so UVs in 0..1 repeat many times.
    micro_img = _load("T_SkinMicroNormal.tga", noncolor=True)
    if micro_img and base_nm_out is not None:
        texc = nt.nodes.new("ShaderNodeTexCoord"); texc.location = (-1400, -1000)
        mapn = nt.nodes.new("ShaderNodeMapping"); mapn.location = (-1200, -1000)
        tile = 20.0    # tiles per UV unit — body UVs use 0..1, so this gives
        mapn.inputs["Scale"].default_value = (tile, tile, tile)
        nt.links.new(texc.outputs["UV"], mapn.inputs["Vector"])
        mtn = nt.nodes.new("ShaderNodeTexImage")
        mtn.image = micro_img; mtn.location = (-900, -1000)
        nt.links.new(mapn.outputs["Vector"], mtn.inputs["Vector"])
        mnm = nt.nodes.new("ShaderNodeNormalMap"); mnm.location = (-500, -1000)
        mnm.inputs["Strength"].default_value = 0.35    # subtle
        nt.links.new(mtn.outputs["Color"], mnm.inputs["Color"])
        # Combine base+micro with a Mix (legacy) — the simplest portable blend.
        # A Normal Mix shader group would be slightly more correct but adds a
        # node-group dependency.
        combine = nt.nodes.new("ShaderNodeMixRGB"); combine.blend_type = "MIX"
        combine.inputs["Fac"].default_value = 0.5; combine.location = (-100, -800)
        nt.links.new(base_nm_out, combine.inputs["Color1"])
        nt.links.new(mnm.outputs["Normal"], combine.inputs["Color2"])
        nt.links.new(combine.outputs["Color"], bsdf.inputs["Normal"])
    elif base_nm_out is not None:
        nt.links.new(base_nm_out, bsdf.inputs["Normal"])

    # --- subsurface scattering (skin) ------------------------------------- #
    # Principled BSDF v3 inputs vary across Blender versions; guard each.
    def _set(k, v):
        if k in bsdf.inputs:
            try: bsdf.inputs[k].default_value = v
            except Exception: pass
    _set("Subsurface Weight", 0.15)
    _set("Subsurface Radius", (1.0, 0.2, 0.1))
    _set("Subsurface Scale", 0.01)          # meters — ~1cm SSS radius
    _set("Subsurface IOR", 1.4)
    _set("Subsurface Anisotropy", 0.0)
    # Specular: real skin is not default-dielectric shiny
    for k in ("Specular IOR Level", "Specular"):
        if k in bsdf.inputs:
            bsdf.inputs[k].default_value = 0.35 if material_kind == "body" else 0.4
            break
    _set("IOR", 1.4)

    return mat


def _build_pbr_material(name, textures_root, assignments, base_color_rgba=None,
                       material_kind="generic", params=None,
                       use_alpha_clip=False):
    """Create a Principled BSDF material with the given texture assignments.
    assignments is a dict like {'basecolor': 'BodyBaseColor.tga', 'normal': '...'}.
    base_color_rgba, if provided and there is no basecolor texture, is applied as a
    uniform RGB on the BSDF input (used for clothing MIs that only expose a Color
    parameter with no albedo texture).
    material_kind drives specular/roughness defaults: cloth is dialed down from
    default dielectric gloss; hair uses low-ish specular + alpha clip."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        nt.nodes.remove(node)

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def _load_img(rel):
        p = os.path.join(textures_root, rel)
        if not os.path.exists(p):
            return None
        try:
            return bpy.data.images.load(p, check_existing=True)
        except Exception:
            return None

    y = 300
    # Track the current "base color" output so we can splice darkening between
    # the source and the BSDF input (hair root darkening uses the same mechanism).
    bc_output = None
    if assignments.get("basecolor"):
        img = _load_img(assignments["basecolor"])
        if img:
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img
            tn.location = (-400, y); y -= 300
            bc_output = tn.outputs["Color"]
    elif base_color_rgba is not None:
        # MH cloth MIs ship NO basecolor texture — color is encoded as two
        # vector params (`diffuse_color_1`, `diffuse_color_2`) blended by
        # `<item>_Mask.tga` R channel (region selector: 0=color1, 1=color2).
        # Without this the garment renders as a flat near-black since only
        # color_1 (usually the deep shadow tone) makes it through.
        color2 = _pick_mi_color(params, "diffuse_color_2") if material_kind == "cloth" else None
        mask_rel = assignments.get("mask") if material_kind == "cloth" else None
        mask_img = _load_img(mask_rel) if mask_rel else None
        if mask_img and color2 is not None:
            try: mask_img.colorspace_settings.name = "Non-Color"
            except Exception: pass
            mn = nt.nodes.new("ShaderNodeTexImage")
            mn.image = mask_img; mn.location = (-900, y)
            sep = nt.nodes.new("ShaderNodeSeparateColor"); sep.location = (-650, y)
            nt.links.new(mn.outputs["Color"], sep.inputs["Color"])
            c1n = nt.nodes.new("ShaderNodeRGB"); c1n.location = (-650, y - 200)
            c1n.outputs[0].default_value = base_color_rgba
            c2n = nt.nodes.new("ShaderNodeRGB"); c2n.location = (-650, y - 400)
            c2n.outputs[0].default_value = color2
            mx = nt.nodes.new("ShaderNodeMix"); mx.data_type = "RGBA"
            mx.location = (-400, y - 100)
            nt.links.new(sep.outputs["Red"], mx.inputs[0])
            nt.links.new(c1n.outputs[0], mx.inputs[6])
            nt.links.new(c2n.outputs[0], mx.inputs[7])
            bc_output = mx.outputs[2]
            y -= 500
        else:
            r, g, b, a = base_color_rgba
            bsdf.inputs["Base Color"].default_value = (r, g, b, a)

    # Cloth detail diffuse — tileable micro weave pattern (denim, oxford, knit).
    # Multiplies into basecolor at DetailTex_UVTtiling tiling, VariationStrength
    # as mix factor. This is what differentiates "flat dark gray shirt" from
    # "denim weave shirt" visually.
    if (material_kind == "cloth" and assignments.get("detail_diffuse")
            and bc_output is not None):
        img = _load_img(assignments["detail_diffuse"])
        if img:
            tile = 40.0; strength = 0.7
            if params:
                s = params.get("scalars") or {}
                tile = float(s.get("DetailTex_UVTtiling", tile))
                strength = float(s.get("DetailTex_VariationStrength", strength))
            # clamp tiling to a sane range — MI values up to 220 tile per UV unit
            tile = max(1.0, min(400.0, tile))
            texc = nt.nodes.new("ShaderNodeTexCoord"); texc.location = (-1500, y)
            mapn = nt.nodes.new("ShaderNodeMapping"); mapn.location = (-1300, y)
            mapn.inputs["Scale"].default_value = (tile, tile, tile)
            nt.links.new(texc.outputs["UV"], mapn.inputs["Vector"])
            dtn = nt.nodes.new("ShaderNodeTexImage"); dtn.image = img
            dtn.location = (-1000, y)
            nt.links.new(mapn.outputs["Vector"], dtn.inputs["Vector"])
            dmx = nt.nodes.new("ShaderNodeMix"); dmx.data_type = "RGBA"
            dmx.blend_type = "MULTIPLY"
            dmx.inputs[0].default_value = max(0.0, min(1.0, strength))
            dmx.location = (-700, y); y -= 300
            nt.links.new(bc_output, dmx.inputs[6])
            nt.links.new(dtn.outputs["Color"], dmx.inputs[7])
            bc_output = dmx.outputs[2]

    # Cloth macro overlay — large-scale variation (heather stripes, pilling,
    # canvas). Tiled at MacroTex_UvTiling (few tiles per UV), overlaid with
    # MacroTexStrength. OVERLAY blend preserves midtones, so a subtle macro
    # mask lifts/darkens specific regions without overpowering the basecolor.
    if (material_kind == "cloth" and assignments.get("macro_overlay")
            and bc_output is not None):
        img = _load_img(assignments["macro_overlay"])
        if img:
            tile = 3.0; strength = 0.3
            if params:
                s = params.get("scalars") or {}
                tile = float(s.get("MacroTex_UvTiling", tile))
                strength = float(s.get("MacroTexStrength", strength))
            tile = max(0.5, min(50.0, tile))
            texc = nt.nodes.new("ShaderNodeTexCoord"); texc.location = (-1500, y)
            mapn = nt.nodes.new("ShaderNodeMapping"); mapn.location = (-1300, y)
            mapn.inputs["Scale"].default_value = (tile, tile, tile)
            nt.links.new(texc.outputs["UV"], mapn.inputs["Vector"])
            mtn = nt.nodes.new("ShaderNodeTexImage"); mtn.image = img
            mtn.location = (-1000, y)
            nt.links.new(mapn.outputs["Vector"], mtn.inputs["Vector"])
            mmx = nt.nodes.new("ShaderNodeMix"); mmx.data_type = "RGBA"
            mmx.blend_type = "OVERLAY"
            mmx.inputs[0].default_value = max(0.0, min(1.0, strength))
            mmx.location = (-700, y); y -= 300
            nt.links.new(bc_output, mmx.inputs[6])
            nt.links.new(mtn.outputs["Color"], mmx.inputs[7])
            bc_output = mmx.outputs[2]

    # Cloth AO multiply — MH garment MIs expose `C_AOMultAmount` (default 0.5)
    # controlling how strongly the AO map darkens the diffuse. Only applied
    # when we already have a node-graph basecolor (flat default stays flat).
    if (material_kind == "cloth" and assignments.get("ao")
            and bc_output is not None):
        ao_img = _load_img(assignments["ao"])
        if ao_img:
            try: ao_img.colorspace_settings.name = "Non-Color"
            except Exception: pass
            ao_tn = nt.nodes.new("ShaderNodeTexImage")
            ao_tn.image = ao_img; ao_tn.location = (-400, y)
            ao_amt = 0.75
            if params:
                try:
                    ao_amt = float((params.get("scalars") or {})
                                   .get("C_AOMultAmount", ao_amt))
                except Exception:
                    pass
            ao_mx = nt.nodes.new("ShaderNodeMix"); ao_mx.data_type = "RGBA"
            ao_mx.blend_type = "MULTIPLY"
            ao_mx.inputs[0].default_value = max(0.0, min(1.0, ao_amt))
            ao_mx.location = (-150, y); y -= 300
            nt.links.new(bc_output, ao_mx.inputs[6])
            nt.links.new(ao_tn.outputs["Color"], ao_mx.inputs[7])
            bc_output = ao_mx.outputs[2]

    if assignments.get("roughness"):
        img = _load_img(assignments["roughness"])
        if img:
            try:
                img.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img
            tn.location = (-400, y); y -= 300
            nt.links.new(tn.outputs["Color"], bsdf.inputs["Roughness"])
    if assignments.get("normal"):
        img = _load_img(assignments["normal"])
        if img:
            try:
                img.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img
            tn.location = (-600, y)
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-300, y); y -= 300
            nt.links.new(tn.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

    # Alpha mask (hair cards). Two MH conventions:
    #   - legacy:  _RootUVSeedCoverage packs coverage in the A channel
    #   - compact (5.6 default): _CardsAtlas_Attribute packs R=strand cutout mask,
    #     G=root→tip gradient, B=root-darkness modulation, A=unused.
    # Prefer the compact atlas when both are present: its R channel gives true
    # per-strand silhouettes; the legacy A channel is a coarser card-level mask
    # that, when stacked across overlapping cards, reads as opaque cardboard.
    alpha_rel  = assignments.get("alpha_r") or assignments.get("alpha")
    alpha_chan = "R" if assignments.get("alpha_r") else "A"
    if alpha_rel:
        img = _load_img(alpha_rel)
        if img:
            try:
                img.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = img
            tn.location = (-400, y); y -= 300
            sep = None
            if "Alpha" in bsdf.inputs:
                if alpha_chan == "A":
                    nt.links.new(tn.outputs["Alpha"], bsdf.inputs["Alpha"])
                else:
                    # Compact-atlas strand mask lives in the R channel — split
                    # and feed raw R into Alpha. HASHED dithering handles the
                    # thin-strand anti-aliasing correctly in Eevee/Cycles.
                    sep = nt.nodes.new("ShaderNodeSeparateColor")
                    sep.location = (-150, y + 150)
                    nt.links.new(tn.outputs["Color"], sep.inputs["Color"])
                    nt.links.new(sep.outputs["Red"], bsdf.inputs["Alpha"])

            # Root darkening from the compact atlas B channel. MH hair cards
            # pack a root-darkness gradient in B (dark at strand roots, bright
            # at tips). Splice a Multiply between the current base-color source
            # and the BSDF Base Color input, with B driving the mix factor via
            # a color ramp that keeps tips at 1.0 and roots at ~0.35.
            if material_kind == "hair" and alpha_chan == "R":
                if sep is None:
                    sep = nt.nodes.new("ShaderNodeSeparateColor")
                    sep.location = (-150, y + 150)
                    nt.links.new(tn.outputs["Color"], sep.inputs["Color"])
                ramp = nt.nodes.new("ShaderNodeValToRGB")
                ramp.location = (100, y + 150)
                # 0.0 → 0.35 (root darken); 1.0 → 1.0 (tip untouched).
                ramp.color_ramp.elements[0].position = 0.0
                ramp.color_ramp.elements[0].color = (0.35, 0.35, 0.35, 1.0)
                ramp.color_ramp.elements[1].position = 1.0
                ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
                nt.links.new(sep.outputs["Blue"], ramp.inputs["Fac"])
                mul = nt.nodes.new("ShaderNodeMixRGB")
                mul.blend_type = "MULTIPLY"
                mul.inputs["Fac"].default_value = 1.0
                mul.location = (350, y + 150)
                if bc_output is not None:
                    nt.links.new(bc_output, mul.inputs["Color1"])
                else:
                    # No basecolor texture — use whatever the BSDF default was
                    # (MI-synthesized RGBA from hairMelanin, or Blender default).
                    bc_default = bsdf.inputs["Base Color"].default_value
                    mul.inputs["Color1"].default_value = (
                        bc_default[0], bc_default[1], bc_default[2], bc_default[3])
                nt.links.new(ramp.outputs["Color"], mul.inputs["Color2"])
                bc_output = mul.outputs["Color"]

    # MH anisotropic hair tangent (_CardsAtlas_Tangent) — not used by the Blender
    # shader (Principled BSDF anisotropy direction comes from tangent geometry,
    # not a texture), but must be loaded as a datablock so stage 03's sidecar
    # emitter picks it up and copies the PNG into textures/ for the web viewer
    # (which wires it into MeshPhysicalMaterial.anisotropyMap).
    if material_kind == "hair" and assignments.get("tangent_atlas"):
        tang_img = _load_img(assignments["tangent_atlas"])
        if tang_img:
            try: tang_img.colorspace_settings.name = "Non-Color"
            except Exception: pass
            # Orphan user on the image datablock so it survives save — the
            # image isn't wired into any shader input, so without this Blender
            # purges it on the next save and stage 03 can't sidecar it.
            tang_img.use_fake_user = True
            # Belt-and-braces: attach to a disconnected TexImage node in the
            # material tree so it's part of the blend regardless.
            tn = nt.nodes.new("ShaderNodeTexImage")
            tn.image = tang_img
            tn.location = (-1400, -300)
            tn.label = "CardsAtlas_Tangent (for web)"
            tn.mute = True

    # Commit base-color wiring (after any hair root-darkening splice).
    if bc_output is not None:
        nt.links.new(bc_output, bsdf.inputs["Base Color"])

    # Roughness scalar fallback — when no texture, drive from MI params or kind default
    if not assignments.get("roughness"):
        rough = _pick_mi_roughness(params, material_kind)
        if rough is not None and "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = rough

    # Specular defaults per kind — cloth/hair should not be as dielectric-shiny as
    # Blender's default. Principled BSDF exposes "Specular IOR Level" (0..1) in
    # Blender 4+ / 5; older versions used "Specular".
    spec_defaults = {"cloth": 0.15, "hair": 0.25, "body": 0.35, "face": 0.4}
    if material_kind in spec_defaults:
        for spec_key in ("Specular IOR Level", "Specular"):
            if spec_key in bsdf.inputs:
                bsdf.inputs[spec_key].default_value = spec_defaults[material_kind]
                break

    # Alpha handling for hair cards — always HASHED. MH hair card silhouettes
    # (scalp/sides/eyebrows/lashes/beard) all rely on stochastic alpha dither
    # to produce strand-shaped edges; BLENDED would require draw-order sorting
    # and breaks in Eevee on thin strands.
    if use_alpha_clip:
        try: mat.surface_render_method = "DITHERED"   # Blender 4.2+/5.x
        except Exception: pass
        try: mat.blend_method = "HASHED"
        except Exception: pass
        try: mat.shadow_method = "HASHED"
        except Exception: pass
        try: mat.alpha_threshold = 0.5
        except Exception: pass

    return mat


def _wire_materials(mh, in_root):
    """For every LOD0 mesh, rebuild its material slots from mh_manifest's
    material→texture map. Returns a list of applied assignments for the blend manifest."""
    textures_root = os.path.join(in_root, "textures")
    textures_by_mat = {}
    for t in mh.get("textures", []):
        key = t.get("material")
        if not key:
            continue
        textures_by_mat.setdefault(key, []).append(t)

    applied = []
    for mesh_rec in mh.get("meshes", []):
        comp = mesh_rec["component"].lower()
        # Hair-card StaticMeshes are already named "..._LOD0" in UE, so their
        # component string includes the suffix. SkeletalMesh components don't —
        # Blender appends _LOD0 during FBX import. Normalize by stripping a
        # trailing "_lod0" from the component before matching.
        comp_stem = comp[:-len("_lod0")] if comp.endswith("_lod0") else comp
        target = None
        for obj in bpy.data.objects:
            if obj.type != "MESH" or not obj.name.endswith("_LOD0"):
                continue
            stem = obj.name[:-len("_LOD0")].lower()
            if stem == comp_stem:
                target = obj; break
        if target is None:
            print(f"[stage02] WARN: no LOD0 object matching component '{comp}'", flush=True)
            continue

        for i, mat_rec in enumerate(mesh_rec.get("materials", [])):
            mat_path = mat_rec["material"]
            slot = mat_rec.get("slot", f"slot{i}")
            # Candidate texture files per PBR channel, with scores — highest score
            # wins. Body has both BodyBaseColor.tga (wanted) and
            # female_underwear_color_map.tga (overlay) on the same material; scoring
            # picks the canonical base color file first.
            candidates = {}
            for t in textures_by_mat.get(mat_path, []):
                # Per-groom atlas textures carry a `component_hint`: skip any
                # whose hint doesn't match this mesh's component. Without this,
                # multiple facial grooms that all share MI_Facial_Hair (male
                # MHs: eyebrows + goatee + mustache) would pool their coverage
                # atlases together and Goatee could pick Mustache's mask.
                hint = t.get("component_hint")
                if hint and hint.lower() != comp:
                    continue
                fn = os.path.basename(t["file_path"])
                kind = _classify_texture(fn)
                if not kind:
                    continue
                # Non-LOD T_Head_* / T_Body_* textures are the 8K LOD0
                # source atlases in 5.7 Cinematic; LOD{N} variants are
                # lower-res bakes. Prefer LOD0 for all PBR channels, not
                # just basecolor.
                ln = fn.lower()
                score = _basecolor_score(fn) if kind == "basecolor" else 0
                if kind != "basecolor":
                    is_lod = "_lod" in ln
                    if (ln.startswith("t_head_") or ln.startswith("t_body_")) and not is_lod:
                        score = 125
                    elif "_lod1" in ln:   score = 115
                    elif "_lod3" in ln:   score = 110
                    elif "_lod5" in ln:   score = 105
                    else:                 score = 100
                prev = candidates.get(kind)
                if prev is None or score > prev[1]:
                    candidates[kind] = (fn, score)
            assignments = {k: v[0] for k, v in candidates.items()}
            base_rgba = _pick_mi_basecolor(mat_rec.get("params"))
            mat_name = f"{comp}_{slot}"
            kind = _classify_material_kind(mat_path, comp)
            face_slot = _classify_face_slot(slot, mat_path)
            # 5.7 slot-name quirk: the bone-skinning pass earlier stashes
            # slot indices that contain eyeball geometry (dominantly
            # weighted to an eye bone) regardless of Epic's labels. Honor
            # that override so a slot named "eyeshell_shader_shader" with
            # eyeball polys gets the eye_refractive shader.
            overrides_raw = target.get("mh_eye_refractive_slot_overrides", None)
            try:
                overrides_list = [int(x) for x in (overrides_raw or [])]
            except Exception as _e:
                overrides_list = []
                print(f"[stage02] WARN override decode failed on {target.name}: {_e}",
                      flush=True)
            if int(i) in overrides_list:
                print(f"[stage02] override: {target.name} slot[{i}] forced "
                      f"from face_slot={face_slot!r} to 'eye_refractive'",
                      flush=True)
                face_slot = "eye_refractive"
                # The override slot's MI may not be associated with the
                # iris atlas (e.g. MI_Face_EyeShell). Inject the stage-01
                # shared iris atlas so the 5.6 iris shader has a texture.
                if "basecolor" not in assignments:
                    import os as _os
                    iris_path = _os.path.join(textures_root, "T_Iris_A_M.tga")
                    if _os.path.exists(iris_path):
                        assignments["basecolor"] = "T_Iris_A_M.tga"
                        print(f"[stage02]          ...and injected "
                              f"T_Iris_A_M.tga into slot[{i}] assignments",
                              flush=True)
            # Face-mesh slots: teeth/eye/saliva/cartilage/eyelashes get dedicated
            # flat-default materials (MH ships no textures for these MIs). The
            # head_skin slot falls through to the skin shader.
            if face_slot and face_slot != "head_skin":
                new_mat = _build_face_accessory_material(
                    mat_name, face_slot, textures_root, assignments,
                    mesh_obj=target, slot_index=i,
                    params=mat_rec.get("params"))
            elif (kind in ("face", "body")
                  and assignments.get("basecolor")
                  and assignments.get("normal")):
                new_mat = _build_skin_material(mat_name, textures_root,
                                               assignments, material_kind=kind)
            else:
                new_mat = _build_pbr_material(mat_name, textures_root, assignments,
                                              base_color_rgba=base_rgba,
                                              material_kind=kind,
                                              params=mat_rec.get("params"),
                                              use_alpha_clip=(kind == "hair"))
            # Each eye gets its own material. The eye-refractive shader
            # bakes the eye-pole UV into color-ramp positions and
            # Mapping-node offsets — those differ per eye, so sharing
            # one material would center the iris mask on only one eye
            # and leave the other rendering as pure sclera.
            if i < len(target.data.materials):
                target.data.materials[i] = new_mat
            else:
                target.data.materials.append(new_mat)
            rec_textures = dict(assignments)
            # Teeth: MH ships no textures on the MI itself, but we preload the
            # LOD-friendly Simplified bake set in _build_face_accessory_material.
            # Surface those on the material spec so stage 03 sidecars them and
            # the viewer can PBR-sample them (teeth + gums + tongue atlas).
            if face_slot == "teeth":
                rec_textures = {
                    "basecolor":       "T_Teeth_BaseColor_Baked.tga",
                    "normal":          "T_Teeth_Normal_Baked.tga",
                    "specular":        "T_Teeth_Specular_Baked.tga",
                    "mouth_occlusion": "T_Teeth_mouthOcc.tga",
                }
            rec = {
                "mesh": target.name,
                "slot_index": i,
                "slot": slot,
                "material_name": mat_name,
                "blender_material_name": new_mat.name,  # post-Blender name collision suffix
                "material_source": mat_path,
                "kind": kind,
                "face_slot": face_slot,
                "mi_params": mat_rec.get("params") or {},
                "textures": rec_textures,
            }
            # For eye_refractive: capture the pole UV so the web viewer can run
            # the same radial iris/sclera/pupil math the Blender shader does.
            if face_slot == "eye_refractive":
                try:
                    pu, pv = _compute_eye_pole_uv(target, i)
                    rec["eye_pole_uv"] = [float(pu), float(pv)]
                except Exception as exc:  # noqa: BLE001
                    print(f"[stage02] WARN: eye_pole_uv failed for {mat_name}: {exc}", flush=True)
            applied.append(rec)
    return applied


def _hide_non_lod0():
    """Hide every mesh whose name doesn't end in _LOD0 (viewport + render).
    MH FBXs with level_of_detail=True import all LODs as sibling meshes at the same
    origin, which stacks them and causes z-fighting. Only LOD0 should be visible by
    default; later stages can unhide as needed."""
    hidden = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if not o.name.endswith("_LOD0"):
            o.hide_set(True)
            o.hide_render = True
            hidden.append(o.name)
    return hidden


def _scene_summary():
    meshes   = [o for o in bpy.data.objects if o.type == "MESH"]
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    return {
        "object_count": len(bpy.data.objects),
        "mesh_count":   len(meshes),
        "armature_count": len(armatures),
        "mesh_names":   sorted(o.name for o in meshes),
        "armature_names": sorted(o.name for o in armatures),
    }


# ---------------------------------------------------------------------------
# Web-renderer material mapping
#
# Blender's material graph (complex mix chains, ColorRamps, Voronoi, etc.) does
# not survive the glTF export — the exporter only understands simple
# Principled-BSDF-with-direct-image-texture graphs. Rather than bake the complex
# reconstruction to flat textures (lossy, high-cost), we emit a declarative
# mapping that the web viewer can use to patch materials at runtime from
# straightforward MI param/texture data.
#
# Scope per kind:
#   skin  — base_color/normal/roughness/cavity texture names (glTF carries these direct anyway)
#   cloth — diffuse_color_1/_2, mask + channel, detail_diffuse tiling/strength,
#           macro_overlay tiling/strength, AO amount
#   eye   — iris/sclera/pupil colors, pole UV, veins power
#   hair  — synthesized base color + optional alpha atlas
#   face_accessory — slot kind + simple color/roughness defaults
# The web viewer only needs to know (material_name, kind, params, textures).

# MH texture image names in the GLB are filenames without extension (Blender
# strips the .tga when building the image datablock). Normalize here so the
# web viewer can look up textures by glTF image name directly.
def _tex_stem(fn):
    if not fn:
        return None
    base = os.path.basename(fn)
    stem, _, _ = base.rpartition(".")
    return stem or base


def _scalar(params, key, default=None):
    if not params:
        return default
    s = params.get("scalars") or {}
    low = {k.lower(): v for k, v in s.items()}
    try:
        return float(low[key.lower()])
    except Exception:
        return default


def _vector(params, key):
    if not params:
        return None
    v = params.get("vectors") or {}
    low = {k.lower(): val for k, val in v.items()}
    hit = low.get(key.lower())
    if isinstance(hit, (list, tuple)) and len(hit) >= 3:
        return [float(hit[0]), float(hit[1]), float(hit[2]),
                float(hit[3]) if len(hit) >= 4 else 1.0]
    return None


def _build_material_mapping(applied):
    """Produce a per-material spec the web viewer uses to reconstruct the look."""
    out = []
    for a in applied:
        params = a.get("mi_params") or {}
        kind = a.get("kind") or "generic"
        face_slot = a.get("face_slot")
        tex = a.get("textures") or {}

        spec = {
            "material_name": a.get("blender_material_name") or a["material_name"],
            "mesh": a["mesh"],
            "slot_index": a["slot_index"],
            "material_source": a.get("material_source"),
            "kind": kind,
            "face_slot": face_slot,
            "params": {},
            "textures": {k: _tex_stem(v) for k, v in tex.items() if v},
        }

        if face_slot and face_slot != "head_skin":
            spec["kind"] = "face_accessory"
            if face_slot == "eye_refractive":
                # Eye procedural — mirror the Blender shader constants one-for-one so
                # the web viewer can port the same math via onBeforeCompile.
                spec["params"].update({
                    "eye_pole_uv":       a.get("eye_pole_uv") or [0.5, 0.5],
                    "uv_scale":          4.0,
                    "iris_color":        [0.176, 0.077, 0.045, 1.0],
                    "iris_dark":         [0.036, 0.015, 0.000, 1.0],
                    "sclera_color":      [0.92,  0.90,  0.86,  1.0],
                    "pupil_color":       [0.005, 0.005, 0.005, 1.0],
                    "vein_color":        [0.62,  0.18,  0.15,  1.0],
                    "veins_power":       _scalar(params, "VeinsPower", 0.5),
                    "limbus_start":      0.095,
                    "iris_radius_in":    0.131,
                    "iris_radius_out":   0.146,
                    "pupil_radius_in":   0.022,
                    "pupil_radius_out":  0.046,
                    "fibril_from_min":   0.25,
                    "fibril_from_max":   0.75,
                    "fibril_to_min":     0.65,
                    "fibril_to_max":     1.15,
                })
            elif face_slot in ("teeth", "saliva"):
                spec["params"]["base_color"] = [0.85, 0.78, 0.70, 1.0] if face_slot == "teeth" else [0.8, 0.9, 1.0, 0.4]
                spec["params"]["roughness"] = 0.35 if face_slot == "teeth" else 0.05
            elif face_slot == "eyelashes":
                spec["params"]["base_color"] = [0.05, 0.04, 0.03, 1.0]
                spec["params"]["roughness"] = 0.7
                spec["params"]["alpha_clip"] = True
            else:
                # eyeshell / eyeEdge / cartilage / head_LODN dupes — leave neutral defaults.
                spec["params"]["base_color"] = [0.9, 0.85, 0.80, 1.0]
                spec["params"]["roughness"] = 0.5
        elif kind == "cloth":
            col1 = _vector(params, "diffuse_color_1")
            col2 = _vector(params, "diffuse_color_2") or col1
            spec["params"].update({
                "diffuse_color_1": col1,
                "diffuse_color_2": col2,
                "roughness":       _scalar(params, "C_roughness value", 0.75),
                "metallic":        _scalar(params, "c_metalness value", 0.0),
                "ao_amount":       _scalar(params, "C_AOMultAmount", 0.85),
                "detail_tiling":   _scalar(params, "DetailTex_UVTtiling", 80.0),
                "detail_strength": _scalar(params, "DetailTex_VariationStrength", 0.6),
                "macro_tiling":    _scalar(params, "MacroTex_UvTiling", 3.0),
                "macro_strength":  _scalar(params, "MacroTexStrength", 0.5),
                "mask_channel":    "r",  # v1 hardcode; TODO auto-detect
            })
        elif kind == "hair":
            # MH hair atlases are data-packed (R = strand cutout mask on the new
            # compact atlas; A = cutout on the legacy atlas). The COLOR comes from
            # hairMelanin/hairRedness/hairDye MI scalars — never from the atlas RGB.
            tex_stems = spec["textures"]
            alpha_channel = "r" if "alpha_r" in tex_stems else ("a" if "alpha" in tex_stems else None)
            alpha_stem = tex_stems.get("alpha_r") or tex_stems.get("alpha")
            tangent_stem = tex_stems.get("tangent_atlas")
            synth_color = _pick_mi_basecolor(params) or [0.3, 0.22, 0.15, 1.0]
            # Eyebrow MIs in MH ship with hairRedness weighted high, so the synth
            # produces a distracting auburn. Force near-black for eyebrows — they
            # should read as "shape under the brow ridge", not as colored hair.
            mh_name = (a.get("material_name") or "").lower()
            if "eyebrow" in mh_name or "eyebrow" in (a.get("mesh") or "").lower():
                synth_color = [0.02, 0.015, 0.01, 1.0]
            spec["params"].update({
                "base_color":    synth_color,
                "roughness":     _pick_mi_roughness(params, "hair") or 0.55,
                "alpha_clip":    True,
                "alpha_channel": alpha_channel,    # which channel of the atlas is the cutout mask
                "alpha_stem":    alpha_stem,       # sidecar stem the viewer can load directly
                "tangent_stem":  tangent_stem,     # MH _CardsAtlas_Tangent stem for anisotropic spec
                "ignore_gltf_map": True,           # glTF may have carried the atlas as map — the
                                                   # viewer MUST discard it (it's not albedo).
            })
        elif kind in ("body", "face"):
            spec["kind"] = "skin"
            spec["params"]["roughness_bias"] = 0.0
        # else: generic — renderer just uses glTF as-is.
        out.append(spec)
    return out


def main():
    args = _parse_args()
    ws = os.path.abspath(args.workspace)
    char_root = os.path.join(ws, "characters", args.char)
    in_root   = os.path.join(char_root, "01-fbx")
    out_root  = os.path.join(char_root, "02-blend")
    os.makedirs(out_root, exist_ok=True)

    mh_manifest_path = os.path.join(in_root, "mh_manifest.json")
    with open(mh_manifest_path, "r", encoding="utf-8") as f:
        mh = json.load(f)

    _reset_scene()

    imported = []
    for m in mh.get("meshes", []):
        fbx_abs = os.path.join(in_root, m["fbx_path"])
        print(f"[stage02] importing {fbx_abs}", flush=True)
        _import_fbx(fbx_abs)
        imported.append({"component": m["component"], "fbx": m["fbx_path"],
                         "lod_count_declared": m.get("lod_count")})

    hidden = _hide_non_lod0()
    print(f"[stage02] hid {len(hidden)} non-LOD0 meshes", flush=True)

    # 5.7 Optimized face mesh quirk: UE slot names don't reliably
    # identify eyeballs (slot "eyeshell_shader_shader" holds an eye
    # sphere in 5.7, not a thin occlusion ring). Classify by skinning
    # instead — MH rigs an eyeball's verts exclusively to an eye bone
    # (FACIAL_{L,R}_Eye). Any face-mesh slot whose polys are dominantly
    # weighted to an eye bone is an eyeball regardless of Epic's name.
    # We stash overrides on the object so _wire_materials picks them up.
    EYE_SLOT_OVERRIDES_ATTR = "mh_eye_refractive_slot_overrides"
    for obj in bpy.data.objects:
        if obj.type != "MESH" or not obj.name.endswith("_FaceMesh_LOD0"):
            continue
        eye_vgs = set()
        for vg in obj.vertex_groups:
            n = vg.name.lower()
            if ("eye" in n and "lash" not in n and "shell" not in n
                    and "brow" not in n and "lid" not in n):
                eye_vgs.add(vg.index)
        if not eye_vgs:
            continue
        slot_stats = {}
        for p in obj.data.polygons:
            rec = slot_stats.setdefault(p.material_index, [0, 0])
            all_eye = True
            for vi in p.vertices:
                v = obj.data.vertices[vi]
                if not v.groups:
                    all_eye = False; break
                best = max(v.groups, key=lambda g: g.weight)
                if best.group not in eye_vgs or best.weight <= 0.5:
                    all_eye = False; break
            rec[0] += 1 if all_eye else 0
            rec[1] += 1
        overrides = []
        for si, (eye_p, total) in slot_stats.items():
            if total == 0: continue
            if eye_p / total >= 0.9:
                overrides.append(si)
        # Attach overrides as a custom property; _wire_materials reads it
        # and forces face_slot = "eye_refractive" for these slot indices.
        obj[EYE_SLOT_OVERRIDES_ATTR] = overrides
        for si in overrides:
            m = obj.material_slots[si].material
            nm = m.name if m else "-"
            print(f"[stage02] 5.7 eyeball-by-skinning: slot[{si}] ({nm}) "
                  f"bone-weighted to eye — forcing eye_refractive classification.",
                  flush=True)

    # 5.7 Optimized FBX quirk: on the face mesh LOD0, UE writes the head
    # skin polygons onto the material slot whose MI is MI_Teeth_Baked, and
    # the small head_LOD1_shader_shader slot ends up with the teeth /
    # mouth-interior polygons. Swap the materials between those two slots
    # before material wiring so the skin shader builds for the head polys.
    # Detection: face LOD0's slot[0] (teeth_shader_shader) having a
    # polygon count an order of magnitude larger than its head_LOD1 slot
    # is the reliable signal (10k+ vs <1k on Kleb).
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not obj.name.endswith("_FaceMesh_LOD0"):
            continue
        counts = {}
        for p in obj.data.polygons:
            counts[p.material_index] = counts.get(p.material_index, 0) + 1
        teeth_idx = None
        head_lod_idx = None
        for i, sl in enumerate(obj.material_slots):
            if sl.material is None:
                continue
            # Match original FBX-imported material names (MI leaf).
            nm = sl.material.name.lower()
            if "teeth_baked" in nm or nm == "mi_teeth_baked":
                teeth_idx = i
            if "face_skin_baked_lod1" in nm or "face_skin_baked_lod" in nm:
                if head_lod_idx is None:
                    head_lod_idx = i
        if teeth_idx is not None and head_lod_idx is not None:
            tc = counts.get(teeth_idx, 0)
            hc = counts.get(head_lod_idx, 0)
            if tc > 5000 and hc < 1500:
                print(f"[stage02] 5.7 FBX quirk: face slot[{teeth_idx}] "
                      f"(MI_Teeth) has {tc} polys; slot[{head_lod_idx}] "
                      f"(MI_Face_Skin) has {hc}. Swapping polygon "
                      f"material_indices so head polys land on the "
                      f"head_LOD slot (where stage 02 will build a skin "
                      f"shader), and teeth polys land on the teeth slot.",
                      flush=True)
                for p in obj.data.polygons:
                    if p.material_index == teeth_idx:
                        p.material_index = head_lod_idx
                    elif p.material_index == head_lod_idx:
                        p.material_index = teeth_idx

    # ARKit 52 transplant must run BEFORE _wire_materials. Stage 02's material
    # wiring replaces the FBX-imported materials with pipeline-built ones, so
    # the original UE names (MI_HeadSynthesized_Baked / MI_EyeRefractive_*)
    # that we classify regions by only exist at this point. Result: replaces
    # the ~822 raw DNA morph targets with 52 ARKit-named shape keys.
    arkit_npz = os.path.join(ws, "skills", "reference", "arkit52_deltas.npz")
    try:
        arkit_summary = apply_arkit52.apply(arkit_npz, char_id=args.char)
    except Exception as exc:
        arkit_summary = {"error": str(exc)}
        print(f"[stage02] ERROR apply_arkit52: {exc}", flush=True)

    # Propagate the face's freshly-stamped ARKit shape keys onto every
    # facial-groom mesh (brows, beard, mustache, goatee, stubble,…) via
    # k-NN-weighted per-vertex sampling. Scalp hair (Hair_*) is skipped;
    # eyelashes live on the face mesh so they inherit morphs implicitly.
    try:
        grooms_summary = apply_arkit52_grooms.apply(char_id=args.char)
    except Exception as exc:
        grooms_summary = {"error": str(exc)}
        print(f"[stage02] ERROR apply_arkit52_grooms: {exc}", flush=True)

    applied = _wire_materials(mh, in_root)
    with_tex = sum(1 for a in applied if a["textures"])
    print(f"[stage02] wired {len(applied)} material slots "
          f"({with_tex} with at least one texture)", flush=True)

    summary = _scene_summary()
    print(f"[stage02] scene: {summary}", flush=True)

    blend_path = os.path.join(out_root, f"{args.char}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    print(f"[stage02] wrote {blend_path}", flush=True)

    blend_manifest = {
        "character_id": args.char,
        "blend_path":   os.path.relpath(blend_path, char_root).replace(os.sep, "/"),
        "imported":     imported,
        "scene":        summary,
        "hidden_non_lod0": hidden,
        "materials_applied": applied,
        "arkit52":      arkit_summary,
        "arkit52_grooms": grooms_summary,
    }
    bm_path = os.path.join(out_root, "blend_manifest.json")
    with open(bm_path, "w", encoding="utf-8") as f:
        json.dump(blend_manifest, f, indent=2)
    print(f"[stage02] wrote {bm_path}", flush=True)

    # Web-renderer material mapping — stage 03 copies this next to the GLB so the
    # three.js viewer can reconstruct MH-style materials client-side without bake.
    mapping = {
        "character_id": args.char,
        "version": 1,
        "materials": _build_material_mapping(applied),
    }
    map_path = os.path.join(out_root, "mh_materials.json")
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    print(f"[stage02] wrote {map_path}", flush=True)

    # --- update character manifest --------------------------------------- #
    char_manifest_path = os.path.join(char_root, "manifest.json")
    with open(char_manifest_path, "r", encoding="utf-8") as f:
        cm = json.load(f)
    stage = cm["stages"]["02_blender_setup"]
    now = _iso_now()
    stage["started_at"] = stage.get("started_at") or now
    expected_count = len(mh.get("meshes", []))
    imported_count = len(imported)
    if imported_count != expected_count:
        stage["status"] = "failed"
        stage["errors"] = [
            f"imported {imported_count} meshes but mh_manifest declared {expected_count}"
        ]
    else:
        stage["status"] = "done"
        stage["completed_at"] = now
        stage["errors"] = []
    with open(char_manifest_path, "w", encoding="utf-8") as f:
        json.dump(cm, f, indent=2)
    print(f"[stage02] char manifest updated: status={stage['status']}", flush=True)


if __name__ == "__main__":
    main()
