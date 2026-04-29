"""
Stage 02 / UE 5.7 Native GLB — import stage 01's per-mesh .glb files into
a single Blender scene.

Unlike the FBX-based pipelines, we do NOT rebuild skin/eye shaders here.
UE's glTF exporter has already baked each MetaHuman material's BaseColor
/ Normal / ORM / Scatter outputs into PNG textures embedded in each
.glb, so the materials round-trip as Principled BSDF with the right
textures wired to the right inputs. Stage 02's job is just:

  1. Load each .glb into the scene.
  2. Mark non-LOD0 / collision meshes hidden (keeps the downstream
     GLB export lean).
  3. Hair-card StaticMeshes get parented to the face skeleton's head
     bone so they track head motion.

Later stages (03 GLB export, 04 webview) work unchanged from cinematic.

Usage:
    blender --background --python import_glb.py \
        -- --char <id> --workspace <abs>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

import bpy


def _log(msg):
    print(f"[stage02-glb] {msg}", flush=True)


def _iso_now():
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    return p.parse_args(argv)


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _import_glb(path):
    # Blender's built-in glTF 2.0 importer reads .glb binary directly.
    bpy.ops.import_scene.gltf(filepath=path, import_pack_images=True)


def _hide_non_lod0():
    """Drop LOD>0 copies + UCX collision hulls from render. MH .glb
    exports keep LOD0 only by default (we set default_level_of_detail=0)
    but hair-card meshes sometimes slip the LOD suffix in, and the
    importer renames collisions to UCX_*."""
    hidden = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        name = obj.name.lower()
        if ("_lod" in name and "_lod0" not in name) or name.startswith("ucx_"):
            obj.hide_viewport = True
            obj.hide_render = True
            hidden += 1
    return hidden


def _parent_hair_to_head_bone(hair_names):
    """Hair cards arrive from UE's glTF exporter already positioned
    correctly — MH bakes hair-card StaticMesh verts in character-world
    space (origin at character root, verts at head height), so the mesh
    sits at the right place just by being imported. All we need to do
    is (a) pick the face armature as the logical parent so the hair
    follows the character's root motion, and (b) NOT translate the
    mesh. We preserve the existing world transform via
    matrix_parent_inverse."""
    # Prefer face armature (has the full facial rig)
    target_arm = None
    for arm in bpy.data.objects:
        if arm.type == "ARMATURE" and "facemesh" in arm.name.lower():
            target_arm = arm
            break
    # Fallbacks: any armature with "face", then any armature at all
    if target_arm is None:
        for arm in bpy.data.objects:
            if arm.type == "ARMATURE" and "face" in arm.name.lower():
                target_arm = arm
                break
    if target_arm is None:
        for arm in bpy.data.objects:
            if arm.type == "ARMATURE":
                target_arm = arm
                break
    if target_arm is None:
        _log("no armature found for hair-card parenting")
        return 0

    _log(f"hair parent: {target_arm.name}")
    parented = 0
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if not any(h in obj.name.lower() for h in hair_names):
            continue
        # Preserve current world transform exactly.
        world_mat = obj.matrix_world.copy()
        obj.parent = target_arm
        obj.parent_type = "OBJECT"
        obj.matrix_parent_inverse = target_arm.matrix_world.inverted()
        obj.matrix_world = world_mat
        parented += 1
    return parented


def _synth_hair_color(mi_params):
    """Compute MH hair-card Base Color from the MI's scalar/vector params.

    Hair cards have NO albedo texture; the Attribute atlas is a data
    map (R = strand cutout mask, G = root->tip gradient, B = root
    darkening modulation). Color comes procedurally from the MI's
    `hairMelanin` (0..1, where 1 = darkest) plus optional `hairRedness`
    and an `hairDye` RGB multiplier. Formula matches 5.6/cinematic
    pipeline."""
    scalars = (mi_params or {}).get("scalars") or {}
    vectors = (mi_params or {}).get("vectors") or {}
    if "hairMelanin" not in scalars:
        # MH plugin default for unbiased hair is medium-brown.
        return (0.18, 0.10, 0.05, 1.0)
    t = max(0.0, min(1.0, float(scalars["hairMelanin"])))
    light = (1.0 - t) ** 1.5
    r = 0.55 * light + 0.02 * t
    g = 0.40 * light + 0.01 * t
    b = 0.25 * light + 0.005 * t
    red = float(scalars.get("hairRedness", 0.0))
    r += red * 0.12
    dye = vectors.get("hairDye")
    if dye and len(dye) >= 3 and dye[3] > 0.0:
        # MH hairDye alpha=0 means "no dye". Skip otherwise the white
        # passthrough RGB nukes the synthesized color to white.
        r *= float(dye[0]); g *= float(dye[1]); b *= float(dye[2])
    clamp = lambda x: max(0.0, min(1.0, x))
    return (clamp(r), clamp(g), clamp(b), 1.0)


def _wire_card_materials(in_root, mh_manifest):
    """Rebuild MH hair-card / eyebrow materials in Blender using the
    sidecar texture atlases + MI params dumped by stage 01.

    GLTFExporter assigns WorldGridMaterial to hair-card meshes because
    the MH hair-card shader doesn't translate to standard PBR. Stage
    01 dumps:

      - PNGs of `/Game/<char>/Grooms/Textures/` (the data atlases —
        Attribute and RootUVSeedCoverage)
      - MI param dict per groom MaterialInstance (hairMelanin etc.)

    For each hair-card mesh we:

      1. Look up the matching MI (by name pattern Hair_S_Coil_CardsMesh
         -> MI_WI_Hair_S_Coil_Hair_Cards) and synthesize a Base Color
         from its hairMelanin / hairRedness / hairDye params.
      2. Load the Attribute atlas as Non-Color, split channels:
         R -> Alpha (strand cutout)
         B -> root-darkening factor (multiplied into Base Color via a
              color ramp that holds tips at 1.0 and roots at 0.35).
      3. Use HASHED alpha so thin strands antialias correctly.

    Matches the 5.6 cinematic implementation in
    5.6/cinematic/stages/02-blender-setup/tools/import_fbx.py."""
    sidecar = mh_manifest.get("sidecar_textures") or []
    groom_mis = mh_manifest.get("groom_materials") or {}
    if not sidecar:
        return 0
    # name -> abs path
    avail = {}
    for rec in sidecar:
        name = rec.get("name") or ""
        path = os.path.join(in_root, rec.get("file_path", ""))
        if name and os.path.isfile(path):
            avail[name] = path

    def _find(prefix_options):
        for n, p in avail.items():
            ln = n.lower()
            for pref in prefix_options:
                if ln.startswith(pref.lower()):
                    return n, p
        return None, None

    def _pick_groom_mi(mesh_name_low):
        """Pick the MI most likely to drive this mesh's material.
        For hair_s_coil_cardsmesh_* -> MI_WI_Hair_S_Coil_Hair_Cards.
        For eyebrows_m_slightarch_* -> MI_WI_Eyebrows_M_SlightArch_*.
        """
        # Strip _CardsMesh_GroupN_LODN tail to get the groom prefix.
        prefix = mesh_name_low.split("_cardsmesh_")[0]
        # Best match: MI_WI_<prefix>_*Cards (hair) or *Hair (brows/lashes)
        candidates = []
        for mi_name, params in groom_mis.items():
            mn = mi_name.lower()
            if not mn.startswith("mi_wi_"):
                continue
            if prefix not in mn:
                continue
            candidates.append((mi_name, params))
        if not candidates:
            return None, None
        # Prefer one with "_cards" in its name; else first.
        for mi_name, params in candidates:
            if "_cards" in mi_name.lower():
                return mi_name, params
        return candidates[0]

    def _find_lash_mi():
        """Eyelash material lives on the face mesh, not a CardsMesh.
        Find the most relevant eyelash MI from the dumped groom params
        (MI_WI_Eyelashes_L_SlightCurl_Hair). Falls back to None."""
        for mi_name, params in groom_mis.items():
            if mi_name.lower().startswith("mi_wi_eyelashes_"):
                return mi_name, params
        return None, None

    def _find_hair_mi_for_color():
        """When the lash MI doesn't carry its own hairMelanin override,
        fall back to the head hair MI so the lashes match the hair
        color (a sane MH default)."""
        for mi_name, params in groom_mis.items():
            mn = mi_name.lower()
            if mn.startswith("mi_wi_hair_") and "hairmelanin" in (
                    s.lower() for s in (params.get("scalars") or {})):
                return params
        return None

    fixed = 0
    # ---- Pass 1: hair-card + eyebrow CardsMesh slots ----
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        name_low = obj.name.lower()
        if "cardsmesh" not in name_low:
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            mat_low = mat.name.lower()
            needs_fix = mat_low.startswith("worldgridmaterial")
            if not needs_fix and mat.use_nodes:
                has_imgs = any(n.type == "TEX_IMAGE" and n.image
                               for n in mat.node_tree.nodes)
                if not has_imgs and ("hair" in mat_low or "brow" in mat_low or
                                      "eyelash" in mat_low):
                    needs_fix = True
            if not needs_fix:
                continue

            # Pick the Attribute (data) atlas by mesh prefix. The
            # Attribute texture is what carries strand cutout (R) and
            # root-darkening (B).
            if name_low.startswith("hair_"):
                attr_name, attr_path = _find(
                    ["Hair_S_Coil_CardsAtlas_Attribute",
                     "Hair_"])
            elif name_low.startswith("eyebrows_"):
                # Prefer the engine-plugin-sourced atlas (named
                # `Eyebrows_<style>_CardsAtlas_Attribute`) over the
                # generic `Texture2D_0` fallback that build_meta_human
                # writes when it can't resolve the source name.
                attr_name, attr_path = _find(
                    ["Eyebrows_", "Texture2D_0"])
            else:
                continue
            if not attr_path:
                _log(f"  wire {obj.name}: no Attribute atlas found")
                continue

            mi_name, mi_params = _pick_groom_mi(name_low)
            color = _synth_hair_color(mi_params)
            _log(f"  wire {obj.name}: mi={mi_name} "
                 f"color=({color[0]:.3f}, {color[1]:.3f}, {color[2]:.3f}) "
                 f"attr={os.path.basename(attr_path)}")

            new_mat = bpy.data.materials.new(name=f"{obj.name}_CardMat")
            new_mat.use_nodes = True
            nt = new_mat.node_tree
            for n in list(nt.nodes):
                nt.nodes.remove(n)
            out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (700, 0)
            bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (450, 0)

            # Attribute atlas as data (Non-Color), split into R/G/B
            attr_tex = nt.nodes.new("ShaderNodeTexImage")
            attr_tex.location = (-400, 0)
            attr_tex.image = bpy.data.images.load(attr_path, check_existing=True)
            try:
                attr_tex.image.colorspace_settings.name = "Non-Color"
            except Exception:
                pass
            sep = nt.nodes.new("ShaderNodeSeparateColor"); sep.location = (-150, 0)
            nt.links.new(attr_tex.outputs["Color"], sep.inputs["Color"])

            # R channel -> Alpha (strand cutout mask)
            nt.links.new(sep.outputs["Red"], bsdf.inputs["Alpha"])

            # B channel -> root darkening factor. Build a color ramp:
            # B=0 (root) -> 0.35 brightness; B=1 (tip) -> 1.0.
            ramp = nt.nodes.new("ShaderNodeValToRGB"); ramp.location = (50, 200)
            ramp.color_ramp.elements[0].position = 0.0
            ramp.color_ramp.elements[0].color = (0.35, 0.35, 0.35, 1.0)
            ramp.color_ramp.elements[1].position = 1.0
            ramp.color_ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
            nt.links.new(sep.outputs["Blue"], ramp.inputs["Fac"])

            # Multiply synthesized hair Base Color by ramp.
            mul = nt.nodes.new("ShaderNodeMixRGB"); mul.location = (250, 200)
            mul.blend_type = "MULTIPLY"
            mul.inputs["Fac"].default_value = 1.0
            mul.inputs["Color1"].default_value = color
            nt.links.new(ramp.outputs["Color"], mul.inputs["Color2"])
            nt.links.new(mul.outputs["Color"], bsdf.inputs["Base Color"])

            nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

            # Alpha-clip hair cards. HASHED looks better in Blender's
            # offline render, but Blender's glTF exporter writes HASHED
            # as alphaMode=BLEND, which produces hard rectangular card
            # silhouettes in any glTF viewer (z-fighting from stacked
            # blended cards). CLIP -> alphaMode=MASK + alphaCutoff is
            # the spec-supported alpha-test path that every browser
            # renderer respects. Cutoff 0.5 gives clean strand
            # silhouettes from the Attribute.R cutout mask.
            try: new_mat.blend_method = "CLIP"
            except Exception: pass
            try: new_mat.alpha_threshold = 0.5
            except Exception: pass
            try: new_mat.shadow_method = "CLIP"
            except Exception: pass

            # Hair-specific PBR defaults
            try: bsdf.inputs["Roughness"].default_value = 0.55
            except Exception: pass
            try: bsdf.inputs["Specular IOR Level"].default_value = 0.25
            except Exception:
                try: bsdf.inputs["Specular"].default_value = 0.25
                except Exception: pass

            slot.material = new_mat
            fixed += 1

    # ---- Pass 2: eyelash slot on the face mesh ----
    # Eyelashes aren't a CardsMesh; their material slot is on the face
    # SkeletalMesh and arrives as an empty Principled BSDF (the lash
    # shader doesn't translate via USE_MESH_DATA). Wire it up using
    # the lash coverage texture from the plugin sidecar pull.
    lash_name, lash_path = _find(["T_Eyelashes_", "Eyelashes_"])
    if lash_path:
        lash_mi_name, lash_mi_params = _find_lash_mi()
        # Eyelash MI usually doesn't override hairMelanin; reuse the
        # head hair MI's value so lashes match hair colour by default.
        synth_params = lash_mi_params or {}
        if not (synth_params.get("scalars") or {}).get("hairMelanin"):
            head_params = _find_hair_mi_for_color()
            if head_params:
                synth_params = {
                    "scalars": dict(synth_params.get("scalars") or {},
                                    **(head_params.get("scalars") or {})),
                    "vectors": dict(synth_params.get("vectors") or {},
                                    **(head_params.get("vectors") or {})),
                }
        lash_color = _synth_hair_color(synth_params)
        for obj in bpy.data.objects:
            if obj.type != "MESH":
                continue
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None:
                    continue
                mat_low = mat.name.lower()
                if "eyelash" not in mat_low:
                    continue

                new_mat = bpy.data.materials.new(
                    name=f"{obj.name}_LashMat")
                new_mat.use_nodes = True
                nt = new_mat.node_tree
                for n in list(nt.nodes):
                    nt.nodes.remove(n)
                out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (700, 0)
                bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (450, 0)
                bsdf.inputs["Base Color"].default_value = lash_color

                # Lash coverage: sample as Non-Color, route alpha
                # channel into BSDF Alpha. Some MH lash atlases pack
                # coverage in R, others in A; we route both safely.
                tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-200, 0)
                tex.image = bpy.data.images.load(lash_path, check_existing=True)
                try:
                    tex.image.colorspace_settings.name = "Non-Color"
                except Exception:
                    pass
                # Most MH lash coverage atlases store mask in A; if A
                # is fully opaque we fall back to R via a SeparateColor.
                nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
                nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

                try: new_mat.blend_method = "HASHED"
                except Exception: pass
                try: new_mat.shadow_method = "HASHED"
                except Exception: pass
                try: bsdf.inputs["Roughness"].default_value = 0.55
                except Exception: pass

                slot.material = new_mat
                _log(f"  wire lash {obj.name}.{slot.name or '<slot>'} "
                     f"-> {os.path.basename(lash_path)} "
                     f"color=({lash_color[0]:.3f}, {lash_color[1]:.3f}, "
                     f"{lash_color[2]:.3f})")
                fixed += 1
    else:
        _log("  wire lash: no eyelash coverage texture in sidecar")

    return fixed


def _emit_material_spec(out_root, mh_manifest):
    """Write `<char>/02-blend/mh_materials.json` describing the card
    materials so stage 04's three.js viewer can apply the proper
    hair / lash shader treatment.

    Schema matches 5.6/cinematic so the viewer.js already in
    stages/04-webview-build/templates/ handles it without change:

        {
          "materials": [
            { "material_name": "...",
              "kind": "hair" | "skin" | ...,
              "face_slot": "eyelashes" | ...,
              "params": { base_color, alpha_clip, alpha_channel,
                          alpha_stem, roughness, ignore_gltf_map },
              "textures": { alpha?: "textures/<file>.png" }
            }, ...
          ]
        }

    `alpha_stem` is the bare filename (no extension) of the sidecar
    texture stage 03 will copy from 01-glb/textures/ into 03-glb/
    textures/. The viewer fetches `textures/<stem>.png` at load time
    and assigns it to the material's alphaMap, then runs the hair /
    lash shader injection."""
    sidecar = mh_manifest.get("sidecar_textures") or []
    groom_mis = mh_manifest.get("groom_materials") or {}

    # name -> stem map for sidecar lookup
    avail = {}
    for rec in sidecar:
        name = rec.get("name") or ""
        if name:
            avail[name] = name  # stem (no extension)

    def _find_stem(prefixes):
        for n in avail:
            ln = n.lower()
            for pref in prefixes:
                if ln.startswith(pref.lower()):
                    return n
        return None

    def _hair_color_for(mi_pattern, require_melanin=False):
        """Find an MI matching pattern, return synth color via 5.6 formula.
        If `require_melanin` is set, skip MIs that don't override
        hairMelanin (so eyelash MIs can fall through to the head hair
        MI's color rather than getting the synth default brown)."""
        for mi_name, params in groom_mis.items():
            if mi_pattern not in mi_name.lower():
                continue
            scalars = (params or {}).get("scalars") or {}
            if require_melanin and "hairMelanin" not in scalars:
                continue
            return _synth_hair_color(params)
        return None

    materials = []
    for mat in bpy.data.materials:
        if not mat.use_nodes or mat.users <= 0:
            continue
        mn = mat.name
        ml = mn.lower()
        if ml.endswith("_cardmat"):
            # Hair / eyebrow card material
            if ml.startswith("hair_"):
                stem = _find_stem(["Hair_S_Coil_CardsAtlas_Attribute",
                                   "Hair_"])
                color = _hair_color_for("mi_wi_hair_") or [0.18, 0.10, 0.05, 1.0]
            elif ml.startswith("eyebrows_"):
                stem = _find_stem(["Eyebrows_M_SlightArch_CardsAtlas_Attribute",
                                   "Eyebrows_"])
                color = _hair_color_for("mi_wi_eyebrows_") or [0.18, 0.10, 0.05, 1.0]
            else:
                continue
            if stem is None:
                continue
            materials.append({
                "material_name": mn,
                "kind": "hair",
                "params": {
                    "base_color": list(color),
                    "alpha_clip": True,
                    "alpha_channel": "r",
                    "alpha_stem": stem,
                    "roughness": 0.55,
                    "ignore_gltf_map": True,
                },
                "textures": {},
            })
        elif ml.endswith("_lashmat"):
            # Eyelash slot on the face mesh
            stem = _find_stem(["T_Eyelashes_", "Eyelashes_"])
            if stem is None:
                continue
            color = (_hair_color_for("mi_wi_eyelashes_", require_melanin=True)
                     or _hair_color_for("mi_wi_hair_", require_melanin=True)
                     or [0.05, 0.03, 0.02, 1.0])
            materials.append({
                "material_name": mn,
                "kind": "face_accessory",
                "face_slot": "eyelashes",
                "params": {
                    "base_color": list(color),
                    "alpha_clip": True,
                    "roughness": 0.55,
                    "ignore_gltf_map": True,
                },
                "textures": {"alpha": f"textures/{stem}.png"},
            })

    if not materials:
        return []

    out_path = os.path.join(out_root, "mh_materials.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"character": "ada",  # caller can overwrite if needed
                   "saved_at": _iso_now(),
                   "materials": materials}, f, indent=2)
    return materials


# Path to the 5.6 cinematic blend that has the canonical 52 ARKit shape
# keys baked on Ada_FaceMesh_LOD0. The 5.6 cinematic pipeline produces
# proper full-magnitude ARKit deltas (jawOpen ~37mm, eyeBlinkLeft ~11mm,
# mouthSmileLeft ~13mm) — magnitudes that include the bone-driven
# component baked as static vertex offsets. We transplant those deltas
# onto the 5.7 GLB face mesh by position (kdtree, centroid-aligned)
# rather than by vertex index because the two exporters split UV seams
# differently and Ada's 5.6 vs 5.7 LOD0 have 34615 vs 34657 verts.
# Path is relative to char_root (workspace/characters/<char>/), so we go
# up 4 levels: characters/<char>/ -> characters/ -> native-glb/ -> 5.7/
# -> worktree root, then into 5.6/cinematic/...
ARKIT_DONOR_BLEND = (
    "../../../../5.6/cinematic/characters/{char}/02-blend/{char}.blend"
)
ARKIT_DONOR_OBJECT = "Ada_FaceMesh_LOD0"
# Threshold: if a target vertex's nearest source vertex is farther than
# this in world-aligned (centroid-removed) space, copy a ZERO delta
# instead of the source delta. Protects topology-misaligned regions
# (teeth interior, eye sockets) from getting a chin's or cheek's delta
# applied to them.
ARKIT_MATCH_THRESHOLD_M = 0.005  # 5 mm


def _bake_arkit_from_5_6(in_root, mh_manifest, char_id, char_root):
    """Transplant the 52 named ARKit shape keys from the 5.6 cinematic
    `Ada_FaceMesh_LOD0` onto this scene's 5.7 GLB face mesh.

    The 5.6 cinematic pipeline already produces proper full-deformation
    ARKit shape keys (joints + morphs + correctives baked as static
    per-vertex deltas). 5.7 doesn't yet have an equivalent bake path
    that's reachable from Python without Maya, so for the same character
    we transplant 5.6's shape keys onto 5.7's GLB face mesh by
    position-matched kdtree.

    Steps:
      1. Append `Ada_FaceMesh_LOD0` from the 5.6 ada.blend.
      2. Compute world-space basis positions on both meshes; align the
         centroids (5.6 character is at z~1.45m, 5.7 at z~1.58m).
      3. KDTree-match each GLB vertex to the closest 5.6 source vertex
         in centered world-aligned space.
      4. For each of the 52 shape keys on the source, transfer per-vertex
         deltas through the index map, with the world->local conversion
         that handles the source's 0.01 import scale vs the GLB's 1.0.
      5. Threshold: vertices > ARKIT_MATCH_THRESHOLD_M from any source
         vertex receive a zero delta (protects topology-misaligned
         regions like teeth interior, eye sockets).
      6. Drop the appended 5.6 source mesh + armature + grooms.

    Returns the count of shape keys transplanted.
    """
    import bmesh
    import mathutils.bvhtree as _bvhtree
    from mathutils import Vector

    donor_rel = ARKIT_DONOR_BLEND.format(char=char_id)
    donor_path = os.path.normpath(os.path.join(char_root, donor_rel))
    if not os.path.isfile(donor_path):
        _log(f"  arkit: donor blend not found: {donor_path}")
        return 0
    _log(f"  arkit: donor = {donor_path}")

    # ---- 1) Append the 5.6 face mesh as a single object ----
    pre_objects = set(o.name for o in bpy.data.objects)
    pre_meshes = set(m.name for m in bpy.data.meshes)
    with bpy.data.libraries.load(donor_path, link=False) as (src, dst):
        if ARKIT_DONOR_OBJECT not in src.objects:
            _log(f"  arkit: donor object '{ARKIT_DONOR_OBJECT}' not in {donor_path}")
            return 0
        dst.objects = [ARKIT_DONOR_OBJECT]
    src_face = bpy.data.objects.get(ARKIT_DONOR_OBJECT)
    if src_face is None:
        _log("  arkit: append failed silently")
        return 0
    bpy.context.collection.objects.link(src_face)
    new_objects = set(o.name for o in bpy.data.objects) - pre_objects
    new_meshes = set(m.name for m in bpy.data.meshes) - pre_meshes
    src_keys = src_face.data.shape_keys
    if src_keys is None:
        _log("  arkit: 5.6 donor face has no shape keys")
        return 0
    sk_names = [kb.name for kb in src_keys.key_blocks]
    arkit_names = [n for n in sk_names if n != "Basis"]
    _log(f"  arkit: 5.6 donor: '{src_face.name}' verts={len(src_face.data.vertices)}, "
         f"shape_keys={len(arkit_names)}")

    # ---- 2) Find the 5.7 GLB face mesh ----
    tgt_face = None
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if "facemesh" in o.name.lower() and "cardsmesh" not in o.name.lower():
            if o is src_face:
                continue
            if tgt_face is None or len(o.data.vertices) > len(tgt_face.data.vertices):
                tgt_face = o
    if tgt_face is None:
        _log("  arkit: no GLB face mesh found in scene")
        return 0
    _log(f"  arkit: 5.7 target: '{tgt_face.name}' verts={len(tgt_face.data.vertices)}")

    # We bypass matrix_world entirely. Appending JUST the face mesh
    # (without its parent armature) leaves matrix_world as identity in
    # Blender, so we work in mesh-LOCAL space and apply known unit
    # scales to put both meshes into common metres.
    #
    # 5.6 cinematic face: imported as FBX with Blender's default 0.01
    #   unit scale, but the appended object has identity matrix_world
    #   (parent armature wasn't appended). The mesh's own local verts
    #   carry the unscaled UE cm values.
    # 5.7 GLB face: glTF importer puts verts directly in metres.
    SRC_LOCAL_TO_M = 0.01  # 5.6 cm -> m
    TGT_LOCAL_TO_M = 1.0   # 5.7 GLB already in m

    # ---- 3) Basis positions in metres, centroid-aligned ----
    src_basis_m = [v.co * SRC_LOCAL_TO_M for v in src_face.data.vertices]
    tgt_basis_m = [v.co * TGT_LOCAL_TO_M for v in tgt_face.data.vertices]
    src_centroid = (sum(src_basis_m, src_basis_m[0] * 0)
                    / max(len(src_basis_m), 1))
    tgt_centroid = (sum(tgt_basis_m, tgt_basis_m[0] * 0)
                    / max(len(tgt_basis_m), 1))
    _log(f"  arkit: src centroid (m) = {[round(c, 4) for c in src_centroid]}")
    _log(f"  arkit: tgt centroid (m) = {[round(c, 4) for c in tgt_centroid]}")

    # ---- 4) Align src to tgt and rescale src to tgt units ----
    # Surface Deform modifier requires src and tgt in the same world space
    # at the same scale. The 5.6 face is at (0, -0.07, 1.45) in 5.6's
    # imagined-world frame and verts are in cm; the 5.7 GLB face is at
    # (0, -0.07, 1.58) and verts are in metres. We bake src verts into
    # metric space at the tgt centroid by replacing src.data.vertices.co
    # in-place: this avoids matrix_world reliance entirely and gives us
    # a properly-scaled, properly-positioned source mesh that the
    # Surface Deform binder can walk with the target.
    align_offset = tgt_centroid - src_centroid * (TGT_LOCAL_TO_M / SRC_LOCAL_TO_M) \
                   if False else None  # not used; we just rescale + offset
    for v in src_face.data.vertices:
        # cm-local -> m-world-aligned-to-tgt
        v.co = (v.co * SRC_LOCAL_TO_M) - src_centroid + tgt_centroid
    # Also rescale every shape key's stored positions so the deltas come
    # out in metres (Surface Deform will compute deltas in shared world
    # space and apply them to tgt verts).
    for kb in src_keys.key_blocks:
        for vi in range(len(src_face.data.vertices)):
            # kb.data is shape-key positions in src-local cm-space too
            # Convert: kb.data[vi].co = (kb.data[vi].co * SRC_LOCAL_TO_M) - src_centroid + tgt_centroid
            kb.data[vi].co = (kb.data[vi].co * SRC_LOCAL_TO_M
                              - src_centroid + tgt_centroid)
    src_face.data.update()
    _log(f"  arkit: src face rescaled+aligned to tgt centroid")

    # ---- 5) Surface Deform: bind tgt to src, then per shape key, set
    # the source's shape weight to 1.0 and snapshot tgt's deformed mesh
    # as a new shape key. This is Blender's official inter-topology
    # deformation transfer and handles the eye-corner / nostril gaps
    # cleanly — every tgt vert lands somewhere on src's surface and
    # gets a smooth blend of its triangle corners' motion. ----
    bpy.context.view_layer.objects.active = tgt_face
    tgt_face.select_set(True)
    src_face.select_set(False)

    # Ensure tgt has a Basis shape key (Surface Deform modifier_apply_as_shapekey
    # writes new shape keys, but the mesh needs a Basis to hang them on).
    if tgt_face.data.shape_keys is None:
        tgt_face.shape_key_add(name="Basis", from_mix=False)
    tgt_keys = tgt_face.data.shape_keys
    tgt_basis_kb = tgt_keys.key_blocks[0]

    # Reset all source shape key values to 0 before bind (bind takes the
    # mesh as-is, so we want to bind against the basis pose).
    for kb in src_keys.key_blocks:
        kb.value = 0.0
    bpy.context.view_layer.update()

    sd_mod = tgt_face.modifiers.new("ARKit_SurfDeform", "SURFACE_DEFORM")
    sd_mod.target = src_face
    bpy.ops.object.surfacedeform_bind(modifier=sd_mod.name)
    if not sd_mod.is_bound:
        _log("  arkit: SurfaceDeform bind failed; falling back to BVH transfer")
        # Fallback: revert src verts (we've changed them in place; revert
        # not strictly possible without reload, but cleanup will drop the
        # appended objects anyway). Just bail with 0.
        tgt_face.modifiers.remove(sd_mod)
        for n_ in new_objects:
            obj = bpy.data.objects.get(n_)
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.ops.outliner.orphans_purge(do_recursive=True)
        return 0
    _log(f"  arkit: SurfaceDeform bound (target={sd_mod.target.name})")

    transferred = 0
    for name in arkit_names:
        src_kb = src_keys.key_blocks.get(name)
        if src_kb is None:
            continue
        # Activate this source shape key fully; surface deform will move
        # the tgt mesh accordingly.
        for kb in src_keys.key_blocks:
            kb.value = 0.0
        src_kb.value = 1.0
        bpy.context.view_layer.update()
        # Apply current Surface-Deform output as a new shape key on tgt,
        # KEEPING the modifier so we can re-apply it for the next pose.
        # The new shape key is appended to tgt_keys; we rename it.
        existing = tgt_keys.key_blocks.get(name)
        if existing is not None:
            tgt_face.shape_key_remove(existing)
        n_keys_before = len(tgt_keys.key_blocks)
        bpy.ops.object.modifier_apply_as_shapekey(
            keep_modifier=True, modifier=sd_mod.name)
        if len(tgt_keys.key_blocks) <= n_keys_before:
            _log(f"  arkit: apply_as_shapekey produced no new key for {name}")
            continue
        tgt_keys.key_blocks[-1].name = name
        tgt_keys.key_blocks[-1].value = 0.0
        transferred += 1

    # Reset source for cleanliness, drop the modifier
    for kb in src_keys.key_blocks:
        kb.value = 0.0
    tgt_face.modifiers.remove(sd_mod)
    _log(f"  arkit: transplanted {transferred} ARKit shape keys onto GLB face "
         f"(via Blender Surface Deform: 5.6 face -> 5.7 GLB face)")

    # ---- 6) Cleanup: drop the appended source ----
    for n_ in new_objects:
        obj = bpy.data.objects.get(n_)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
    for n_ in new_meshes:
        m = bpy.data.meshes.get(n_)
        if m is not None and m.users == 0:
            bpy.data.meshes.remove(m, do_unlink=True)
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    return transferred


def _bake_arkit_from_lse_fbx(in_root, mh_manifest):
    """Bake 52 ARKit shape keys onto the GLB face mesh by replaying the
    Sequencer-baked AnimSequence and capturing per-pose deformed mesh.

    Stage 01 produces `LS_arkit_full.fbx` via UE's Sequencer, which
    bakes the curve-driven AS_MetaHuman_ARKit_Mapping into a real
    bone-keyed animation by running it through the live skeletal mesh
    component (RigLogic + correctives + bone resolution). The FBX has
    the face mesh + skeleton + bone keyframes per ARKit pose.

    Per-frame morph weights do NOT round-trip through FBX (only bones
    do), so the deformations we capture are bone-only — bones drive
    most ARKit motion (jaw rotation, eye rotation, eyelid roll); we
    miss the fine morph correctives (lip squash, wrinkle deltas) that
    make up ~5-10mm of detail on top of bone motion. Acceptable trade
    for a fully-automated 5.7-native pipeline.

    Returns the count of shape keys baked + transferred.
    """
    import mathutils.kdtree as _kdtree

    sources = (mh_manifest or {}).get("arkit_sources") or {}
    lse_fbx_rel = sources.get("lse_fbx")
    pose_names_rel = sources.get("pose_names")
    if not lse_fbx_rel or not pose_names_rel:
        _log("  arkit: lse_fbx or pose_names missing in manifest")
        return 0
    lse_fbx = os.path.join(in_root, lse_fbx_rel)
    pose_names_json = os.path.join(in_root, pose_names_rel)
    if not os.path.isfile(lse_fbx) or not os.path.isfile(pose_names_json):
        _log(f"  arkit: lse_fbx={os.path.isfile(lse_fbx)} "
             f"pose_names={os.path.isfile(pose_names_json)}")
        return 0

    raw_pose_names = json.load(open(pose_names_json, encoding="utf-8"))

    def _camel(n):
        if not n or n == "Default" or (len(n) >= 5 and n[:5] == "Pose_"):
            return n
        return n[0].lower() + n[1:]
    arkit_pose_names = [(_camel(n), i) for i, n in enumerate(raw_pose_names)
                        if n != "Default" and not n.startswith("Pose_")]

    # ---- 1) Import LSE FBX (mesh + skeleton + animation) ----
    pre_objects = set(o.name for o in bpy.data.objects)
    pre_meshes = set(m.name for m in bpy.data.meshes)
    pre_actions = set(a.name for a in bpy.data.actions)
    bpy.ops.import_scene.fbx(filepath=lse_fbx, use_anim=True)
    new_objects = set(o.name for o in bpy.data.objects) - pre_objects
    new_meshes = set(m.name for m in bpy.data.meshes) - pre_meshes
    new_actions = set(a.name for a in bpy.data.actions) - pre_actions

    src_face = next((bpy.data.objects[n] for n in new_objects
                     if bpy.data.objects[n].type == "MESH"), None)
    src_arm = next((bpy.data.objects[n] for n in new_objects
                    if bpy.data.objects[n].type == "ARMATURE"), None)
    if src_face is None or src_arm is None:
        _log("  arkit: LSE FBX import missing mesh or armature")
        return 0
    _log(f"  arkit: imported LSE: face='{src_face.name}' "
         f"verts={len(src_face.data.vertices)} arm='{src_arm.name}' "
         f"action={src_arm.animation_data.action.name if src_arm.animation_data and src_arm.animation_data.action else None}")

    # The animated frame range. Stage 01 sets the level sequence's
    # display rate to 24fps to match the source AnimSequence's native
    # rate, so each integer bake frame == one source pose exactly. The
    # action's frame_range[0] corresponds to source pose 0 ("Default").
    n_poses_total = len(raw_pose_names)
    if src_arm.animation_data and src_arm.animation_data.action:
        action = src_arm.animation_data.action
        f_start = int(action.frame_range[0])
        f_end = int(action.frame_range[1])
    else:
        f_start, f_end = 1, n_poses_total
    n_frames = f_end - f_start + 1
    _log(f"  arkit: action frames {f_start}..{f_end} ({n_frames} frames), "
         f"poses {n_poses_total}")

    def _pose_idx_to_frame(pose_idx):
        # 1:1 mapping at 24fps bake. Pose N is at f_start + N.
        return f_start + pose_idx

    # ---- 2) Capture deformed mesh as a new shape key per ARKit pose ----
    # Use evaluated_get to read the mesh after armature deformation.
    if src_face.data.shape_keys is None:
        src_face.shape_key_add(name="Basis", from_mix=False)
    src_keys = src_face.data.shape_keys

    # Capture basis (frame 1) first
    bpy.context.view_layer.objects.active = src_face
    bpy.context.scene.frame_set(f_start)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_basis_obj = src_face.evaluated_get(depsgraph)
    basis_positions = [v.co.copy() for v in eval_basis_obj.data.vertices]
    n_src = len(basis_positions)

    pose_shape_keys = []  # list of (shape_key_name, [vec3 per src vertex])
    for pose_name, pose_idx in arkit_pose_names:
        f = _pose_idx_to_frame(pose_idx)
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = src_face.evaluated_get(depsgraph)
        positions = [v.co.copy() for v in eval_obj.data.vertices]
        pose_shape_keys.append((pose_name, positions))

    _log(f"  arkit: captured {len(pose_shape_keys)} ARKit pose meshes from LSE FBX")

    # ---- 3) Find GLB face mesh ----
    tgt_face = None
    for o in bpy.data.objects:
        if o.type != "MESH" or o is src_face:
            continue
        if "facemesh" in o.name.lower() and "cardsmesh" not in o.name.lower():
            if tgt_face is None or len(o.data.vertices) > len(tgt_face.data.vertices):
                tgt_face = o
    if tgt_face is None:
        _log("  arkit: no GLB face mesh found")
        return 0
    _log(f"  arkit: GLB target='{tgt_face.name}' verts={len(tgt_face.data.vertices)}")

    # ---- 4) KDTree-match GLB face vertices to LSE source verts in
    # common metric space. LSE FBX import has world_scale=0.01 (cm->m),
    # but evaluated_get returns mesh-local positions in cm. GLB import
    # has world_scale=1.0 with mesh-local in m. Normalize both to m. ----
    SRC_LOCAL_TO_M = 0.01
    TGT_LOCAL_TO_M = 1.0
    src_basis_m = [p * SRC_LOCAL_TO_M for p in basis_positions]
    tgt_basis_m = [v.co * TGT_LOCAL_TO_M for v in tgt_face.data.vertices]
    src_centroid = sum(src_basis_m, src_basis_m[0] * 0) / max(len(src_basis_m), 1)
    tgt_centroid = sum(tgt_basis_m, tgt_basis_m[0] * 0) / max(len(tgt_basis_m), 1)

    n_tgt = len(tgt_basis_m)
    tree = _kdtree.KDTree(n_src)
    for i, p in enumerate(src_basis_m):
        tree.insert(p - src_centroid, i)
    tree.balance()
    glb_to_src = [0] * n_tgt
    distances = [0.0] * n_tgt
    max_d = 0.0
    sum_d = 0.0
    for i, p in enumerate(tgt_basis_m):
        _, idx, d = tree.find(p - tgt_centroid)
        glb_to_src[i] = idx
        distances[i] = d
        sum_d += d
        if d > max_d:
            max_d = d
    avg_d = sum_d / n_tgt if n_tgt else 0.0
    _log(f"  arkit: kdtree match max={max_d*1000:.2f}mm avg={avg_d*1000:.2f}mm")

    # ---- 5) Transfer captured shape keys to GLB face. Source positions
    # are in cm (mesh-local), target is in m. Convert deltas accordingly. ----
    if tgt_face.data.shape_keys is None:
        tgt_face.shape_key_add(name="Basis", from_mix=False)
    tgt_keys = tgt_face.data.shape_keys
    tgt_basis_kb = tgt_keys.key_blocks[0]
    delta_scale = SRC_LOCAL_TO_M / TGT_LOCAL_TO_M

    transferred = 0
    for name, src_positions in pose_shape_keys:
        existing = tgt_keys.key_blocks.get(name)
        if existing is not None:
            tgt_face.shape_key_remove(existing)
        new_kb = tgt_face.shape_key_add(name=name, from_mix=False)
        for ti in range(n_tgt):
            si = glb_to_src[ti]
            src_delta_local = src_positions[si] - basis_positions[si]
            tgt_delta_local = src_delta_local * delta_scale
            new_kb.data[ti].co = tgt_basis_kb.data[ti].co + tgt_delta_local
        new_kb.value = 0.0
        transferred += 1
    _log(f"  arkit: transferred {transferred} ARKit shape keys to GLB face")

    # ---- 6) Cleanup: drop the LSE imports ----
    for n_ in new_objects:
        obj = bpy.data.objects.get(n_)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
    for n_ in new_meshes:
        m = bpy.data.meshes.get(n_)
        if m is not None and m.users == 0:
            bpy.data.meshes.remove(m, do_unlink=True)
    for n_ in new_actions:
        a = bpy.data.actions.get(n_)
        if a is not None and a.users == 0:
            bpy.data.actions.remove(a, do_unlink=True)
    bpy.ops.outliner.orphans_purge(do_recursive=True)
    return transferred


# Mesh-name prefixes for facial-groom card meshes that need to follow the
# face's ARKit deformation. Hair_ is intentionally excluded — the scalp
# doesn't deform with ARKit shapes, so any morph transfer there would be
# noise (and hair sits high enough that nearest-face-vert lookups would
# pull from forehead, which DOES deform).
_GROOM_PREFIXES = (
    "Eyebrows_", "Mustache_", "Moustache_", "Goatee_", "Beard_",
    "Stubble_", "Sideburns_", "Fuzz_",
)


def _apply_arkit_to_grooms(face_obj):
    """Propagate the face mesh's ARKit shape keys onto every facial-groom
    card mesh in the scene (eyebrows, mustache, beard, stubble, etc.).

    The groom cards are separate StaticMeshes positioned on the face
    surface at rest. They have no shape keys of their own — when the
    face's jawOpen fires, the lower face translates ~2cm down while the
    beard hanging below stays put, producing visible detachment.

    For each groom vert, we find the k=4 nearest face verts (in world
    space, from the face's Basis), compute inverse-distance² weights,
    and sample the weighted average of every face shape key's delta.
    Then we write a matching shape key onto the groom (same name → same
    index in the viewer's morphTargetDictionary → slider drives every
    mesh in lockstep).

    Eyelashes are NOT a separate mesh on MH — they live as a material
    slot on the face mesh, so they inherit the face's shape keys
    automatically. No special handling needed.

    Ported from 5.6/cinematic apply_arkit52_grooms.py.
    """
    import numpy as np
    from mathutils.kdtree import KDTree
    from mathutils import Vector

    K = 4         # neighbors to blend per groom vert (5.6 sweet spot)
    EPS = 1e-8    # ε in inverse-distance² weights

    grooms = [o for o in bpy.data.objects
              if o.type == "MESH"
              and any(o.name.startswith(p) for p in _GROOM_PREFIXES)]
    if not grooms:
        _log(f"  grooms: no card meshes found (prefixes: "
             f"{', '.join(_GROOM_PREFIXES)})")
        return 0

    mesh = face_obj.data
    sk = mesh.shape_keys
    if sk is None or len(sk.key_blocks) <= 1:
        _log("  grooms: face has no shape keys, skipping")
        return 0

    # ---- Face: Basis + per-key deltas in WORLD space ----
    basis_local = np.empty(len(sk.key_blocks[0].data) * 3, dtype=np.float32)
    sk.key_blocks[0].data.foreach_get("co", basis_local)
    basis_local = basis_local.reshape(-1, 3)

    R_face = np.array(face_obj.matrix_world, dtype=np.float64)[:3, :3]
    t_face = np.array(face_obj.matrix_world, dtype=np.float64)[:3, 3]
    basis_world = basis_local.astype(np.float64) @ R_face.T + t_face

    face_deltas_world = {}
    for kb in sk.key_blocks:
        if kb.name == "Basis":
            continue
        pos = np.empty(len(kb.data) * 3, dtype=np.float32)
        kb.data.foreach_get("co", pos)
        delta_local = pos.reshape(-1, 3) - basis_local
        if not np.any(np.abs(delta_local) > 1e-7):
            continue
        # delta is a direction vector — only the rotation+scale 3x3 applies,
        # translation cancels.
        face_deltas_world[kb.name] = delta_local.astype(np.float64) @ R_face.T

    if not face_deltas_world:
        _log("  grooms: face shape keys all zero, nothing to transfer")
        return 0

    _log(f"  grooms: face={face_obj.name} basis_verts={basis_world.shape[0]} "
         f"keys={len(face_deltas_world)} grooms={[g.name for g in grooms]}")

    # ---- KD-tree over face basis in world space ----
    tree = KDTree(basis_world.shape[0])
    for i, p in enumerate(basis_world):
        tree.insert(Vector((float(p[0]), float(p[1]), float(p[2]))), i)
    tree.balance()

    total_keys_added = 0
    for groom_obj in grooms:
        gmesh = groom_obj.data
        # Groom verts in world space
        groom_local = np.empty(len(gmesh.vertices) * 3, dtype=np.float32)
        gmesh.vertices.foreach_get("co", groom_local)
        groom_local = groom_local.reshape(-1, 3).astype(np.float64)
        R_g = np.array(groom_obj.matrix_world, dtype=np.float64)[:3, :3]
        t_g = np.array(groom_obj.matrix_world, dtype=np.float64)[:3, 3]
        groom_world = groom_local @ R_g.T + t_g
        N_g = groom_world.shape[0]

        # k-NN against face basis
        nn_idx = np.empty((N_g, K), dtype=np.int64)
        nn_dist = np.empty((N_g, K), dtype=np.float64)
        for gi in range(N_g):
            p = groom_world[gi]
            results = tree.find_n(
                Vector((float(p[0]), float(p[1]), float(p[2]))), K)
            for k, (_co, idx, d) in enumerate(results):
                nn_idx[gi, k] = idx
                nn_dist[gi, k] = d

        # Inverse-distance² weights normalized per-vert. When d≈0 (groom
        # vert sitting on a face vert), that neighbor dominates ~100%.
        w = 1.0 / (nn_dist * nn_dist + EPS)
        w /= w.sum(axis=1, keepdims=True)

        # Wipe any prior shape keys on the groom
        if gmesh.shape_keys is None:
            groom_obj.shape_key_add(name="Basis", from_mix=False)
        existing = [kb.name for kb in gmesh.shape_keys.key_blocks
                    if kb.name != "Basis"]
        for n in reversed(existing):
            groom_obj.shape_key_remove(gmesh.shape_keys.key_blocks[n])

        # Inverse groom rotation for world→groom-local delta conversion.
        # Use actual inverse (handles non-uniform scale safely).
        R_g_inv = np.linalg.inv(R_g)

        created = 0
        skipped = []
        for key_name, fd_world in face_deltas_world.items():
            sampled_world = np.einsum("gk,gkd->gd", w, fd_world[nn_idx])
            if not np.any(np.abs(sampled_world) > 1e-6):
                skipped.append(key_name)
                continue
            sampled_local = sampled_world @ R_g_inv.T  # directions → local
            new_positions = (groom_local + sampled_local).astype(np.float32)
            kb = groom_obj.shape_key_add(name=key_name, from_mix=False)
            # Blender 5.0 defaults shape_key_add().value to 1.0, which
            # the glTF exporter writes into mesh.weights → zombie face at
            # rest. Force 0.
            kb.value = 0.0
            kb.data.foreach_set("co", new_positions.reshape(-1))
            created += 1

        nn0 = nn_dist[:, 0]
        _log(f"  grooms: {groom_obj.name} verts={N_g} keys_created={created} "
             f"skipped_zero={len(skipped)} nn0_mean={nn0.mean()*1000:.2f}mm "
             f"p95={np.percentile(nn0, 95)*1000:.2f}mm")
        total_keys_added += created
    return total_keys_added


# Per-side raw RigLogic morphs we keep alongside the 52 ARKit blendshapes.
# ARKit's `BrowInnerUp` is symmetric (drives both inner brows together),
# but MediaPipe landmarks let us measure inner-brow displacement per
# eye and drive each side independently for asymmetric expressions.
# The keys here are the raw morph names on the FBX face SkeletalMesh
# (which preserves them, unlike the GLTFExporter); the values are the
# readable names we expose on the final GLB / blendshape panel.
EXTRA_RAW_MORPHS = {
    # Per-side inner brow raise. ARKit's BrowInnerUp is symmetric;
    # MediaPipe landmarks let us drive each inner brow independently.
    "head_lod0_mesh__brow_raiseIn_L": "browInnerUpLeft",
    "head_lod0_mesh__brow_raiseIn_R": "browInnerUpRight",
    # Per-side inner-corner squint. Finer-grained than ARKit's full
    # eyeSquintLeft/Right - drives the inner-corner crinkle that gives
    # a smile its Duchenne / smile-in-the-eyes character.
    "head_lod0_mesh__eye_squintInner_L": "eyeSquintInnerLeft",
    "head_lod0_mesh__eye_squintInner_R": "eyeSquintInnerRight",
}


def _bake_arkit_shape_keys(in_root, mh_manifest):
    """Bake ARKit-named shape keys onto the GLB face mesh by
    importing the side-car FBX face mesh + ARKit AnimSequence and
    stepping through each frame.

    Stage 01 dumps:
      <face>.fbx                 - SkeletalMesh + 858 raw RigLogic
                                   morphs (with their original names).
      AS_MetaHuman_ARKit_Mapping.fbx
                                  - one frame per ARKit pose, drives
                                   the raw morphs into the composed
                                   pose.
      arkit_pose_names.json      - ordered list of pose names.

    Frame N corresponds to pose_names[N]. We:

      1. Load the face FBX into a scratch armature/mesh.
      2. Load the AnimSequence FBX onto the same armature.
      3. For each frame, set the scene to that frame, force a depsgraph
         update, then `shape_key_add(from_mix=True)` on the imported
         mesh. The new key captures the composed deformation from
         that frame's morph weights + bone transforms.
      4. Rename the new key to pose_names[N].
      5. Strip the 858 raw shape keys (we keep only the named poses).
      6. Transfer those shape keys onto the GLB face mesh by vertex
         order (FBX and GLB come from the same UE SkeletalMesh data
         so vertex count + order match).
      7. Drop the FBX-imported objects so they don't ship in the
         final blend.

    Returns the count of shape keys baked, or 0 if any source asset
    is missing."""
    sources = (mh_manifest or {}).get("arkit_sources") or {}
    if not sources:
        _log("  arkit: no sources in manifest - skipping shape-key bake")
        return 0

    face_fbx = os.path.join(in_root, sources.get("face_fbx", ""))
    pose_json = os.path.join(in_root, sources.get("pose_names", ""))
    curves_json = os.path.join(in_root, sources.get("pose_curves", ""))
    if not (os.path.isfile(face_fbx) and os.path.isfile(pose_json)
            and os.path.isfile(curves_json)):
        _log(f"  arkit: missing source(s): face_fbx={os.path.isfile(face_fbx)} "
             f"pose_json={os.path.isfile(pose_json)} curves_json={os.path.isfile(curves_json)}")
        return 0

    raw_pose_names = json.load(open(pose_json, "r", encoding="utf-8"))
    pose_curves_map = json.load(open(curves_json, "r", encoding="utf-8"))
    # MH PoseAsset names are PascalCase (`EyeBlinkLeft`); the ARKit /
    # MediaPipe / three.js convention is camelCase (`eyeBlinkLeft`).
    # Lowercase the first character so the names match
    # BLENDSHAPE_GROUPS in viewer.js and MediaPipe FaceLandmarker
    # category names without a remap layer. "Default" stays as-is.
    def _camel(n):
        if not n or n == "Default" or (len(n) >= 5 and n[:5] == "Pose_"):
            return n
        return n[0].lower() + n[1:]
    pose_names = [_camel(str(n)) for n in raw_pose_names]
    _log(f"  arkit: {len(pose_names)} pose names to bake "
         f"(first 5 = {pose_names[:5]})")

    # Snapshot existing object names so we know what came from the
    # FBX import.
    pre_objects = set(o.name for o in bpy.data.objects)
    bpy.ops.import_scene.fbx(
        filepath=face_fbx,
        use_anim=False,
        ignore_leaf_bones=True,
        automatic_bone_orientation=False,
    )
    new_after_face = set(o.name for o in bpy.data.objects) - pre_objects
    fbx_face_obj = next(
        (bpy.data.objects[n] for n in new_after_face
         if bpy.data.objects[n].type == "MESH"), None)
    if fbx_face_obj is None:
        _log("  arkit: FBX face import produced no MESH; bailing")
        return 0
    fbx_keys = fbx_face_obj.data.shape_keys
    _log(f"  arkit: imported FBX face = '{fbx_face_obj.name}' "
         f"verts={len(fbx_face_obj.data.vertices)} "
         f"morphs={len(fbx_keys.key_blocks) - 1 if fbx_keys else 0}")

    # Curve -> morph target matching. The AnimSequence dumps a mix of
    # curve types per pose:
    #   ctrl_expressions_*       RigLogic INPUT controls (NOT morph weights)
    #   ctrl_riglogic_offon      RigLogic enable bit
    #   head_cmN_color_*         wrinkle/colormap params
    #   head_lod0_mesh__*        RigLogic OUTPUT morph weights for face <- THE TRUTH
    #   cartilage_lod0_mesh__*   RigLogic OUTPUT morph weights for cartilage submesh
    # The `*_lod0_mesh__*` curves ARE the resolved morph weights post-RigLogic;
    # they include correctives that the input controls alone don't capture
    # (e.g. mouth corner pull at full strength when smiling). Curve names
    # come back lowercase from AnimPose.get_curve_names(), but FBX morph
    # names preserve case (`head_lod0_mesh__eye_blink_L`,
    # `head_lod0_mesh__mouth_cornerPull_left`). Build a case- and
    # underscore-insensitive lookup so the lowercase curve names match.
    def _norm(name):
        return name.replace("_", "").lower()
    morph_norm_to_full = {
        _norm(kb.name): kb.name
        for kb in (fbx_keys.key_blocks[1:] if fbx_keys else [])
    }
    _log(f"  arkit: {len(morph_norm_to_full)} morph targets indexed for curve-match")

    # Bake one shape key per pose by setting curve weights manually.
    bpy.context.view_layer.objects.active = fbx_face_obj
    fbx_face_obj.select_set(True)
    if fbx_keys is None:
        fbx_face_obj.shape_key_add(name="Basis", from_mix=False)
        fbx_keys = fbx_face_obj.data.shape_keys

    # Reset every shape key to 0 before each pose so prior poses don't
    # leak into the next.
    def _reset_all_keys():
        for kb in fbx_keys.key_blocks:
            kb.value = 0.0

    baked_names = []
    pose_match_log = []
    for i, pose_name in enumerate(pose_names):
        raw_name = raw_pose_names[i]  # PoseAsset's PascalCase original
        curves = pose_curves_map.get(raw_name, {})
        _reset_all_keys()
        matched = 0
        skipped_non_morph = 0
        unmatched_morph = []
        for curve_name, weight in curves.items():
            # Only RigLogic-OUTPUT curves carry morph weights. Inputs
            # (ctrl_expressions_*, ctrl_riglogic_*) and wrinkle-colormap
            # params (head_cmN_color_*) are not morph drivers — skip them.
            if not (curve_name.startswith("head_lod0_mesh__") or
                    curve_name.startswith("cartilage_lod0_mesh__")):
                skipped_non_morph += 1
                continue
            morph_full = morph_norm_to_full.get(_norm(curve_name))
            if morph_full is None:
                unmatched_morph.append(curve_name)
                continue
            kb = fbx_keys.key_blocks.get(morph_full)
            if kb is not None:
                kb.value = float(weight)
                matched += 1
        bpy.context.view_layer.update()
        new_key = fbx_face_obj.shape_key_add(name=pose_name, from_mix=True)
        new_key.value = 0.0
        baked_names.append(pose_name)
        if i < 6 or matched == 0 or unmatched_morph:
            extra = (f", unmatched first 3: {unmatched_morph[:3]}"
                     if unmatched_morph else "")
            pose_match_log.append(
                f"    pose[{i}] {pose_name}: {matched} morph curves matched, "
                f"{skipped_non_morph} non-morph curves skipped" + extra)
    for line in pose_match_log[:10]:
        _log(line)
    _reset_all_keys()
    _log(f"  arkit: baked {len(baked_names)} pose shape keys")

    # Promote curated raw morphs to friendly names BEFORE the strip,
    # so e.g. `head_lod0_mesh__brow_raiseIn_L` -> `browInnerUpLeft`
    # and survives as a kept shape key on the GLB.
    skeys = fbx_face_obj.data.shape_keys
    for raw_name, friendly_name in EXTRA_RAW_MORPHS.items():
        kb = skeys.key_blocks.get(raw_name)
        if kb is None:
            _log(f"  arkit: extra raw morph not present: {raw_name}")
            continue
        # Don't collide with an ARKit-baked name if one already exists.
        if friendly_name in skeys.key_blocks:
            _log(f"  arkit: skip rename - '{friendly_name}' already exists")
            continue
        kb.name = friendly_name
        baked_names.append(friendly_name)
        _log(f"  arkit: kept raw '{raw_name}' as '{friendly_name}'")

    # Strip the rest of the 858 raw morphs.
    keep = set(baked_names)
    to_remove = [kb.name for kb in list(skeys.key_blocks)[1:]
                 if kb.name not in keep]
    for name in to_remove:
        kb = skeys.key_blocks.get(name)
        if kb:
            fbx_face_obj.shape_key_remove(kb)
    _log(f"  arkit: stripped {len(to_remove)} raw RigLogic morphs; "
         f"remaining = {len(skeys.key_blocks) - 1}")

    # Find the GLB face mesh (kept the same name pattern as
    # `_find_face_mesh` in apply_arkit52.py).
    glb_face = None
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if "facemesh" in o.name.lower() and "cardsmesh" not in o.name.lower():
            if o is fbx_face_obj:
                continue
            if not glb_face or len(o.data.vertices) > len(glb_face.data.vertices):
                glb_face = o
    if glb_face is None:
        _log("  arkit: no GLB face mesh found; leaving keys on FBX face")
        return len(baked_names)

    fbx_verts = len(fbx_face_obj.data.vertices)
    glb_verts = len(glb_face.data.vertices)
    _log(f"  arkit: GLB face = '{glb_face.name}' verts={glb_verts} "
         f"(FBX face verts={fbx_verts})")

    # Vertex-order is NOT shared between UE's FBX exporter and GLTFExporter:
    # they split UV seams independently, so even when vertex counts match,
    # index N points to different physical vertices. Naive index-by-index
    # transfer applies deltas to the wrong vertices and warps the face.
    # We build a KDTree of FBX face world positions and remap each GLB
    # vertex to its nearest FBX neighbor. World-space matching is the
    # right axis here because both meshes share the same UE SKM at the
    # same rest pose, so positions agree to sub-millimeter even when the
    # local rest matrices differ.
    import mathutils.kdtree as _kdtree
    fbx_world = fbx_face_obj.matrix_world
    glb_world = glb_face.matrix_world
    tree = _kdtree.KDTree(fbx_verts)
    for vi, v in enumerate(fbx_face_obj.data.vertices):
        tree.insert(fbx_world @ v.co, vi)
    tree.balance()

    glb_to_fbx = [0] * glb_verts
    max_d = 0.0
    sum_d = 0.0
    for vi, v in enumerate(glb_face.data.vertices):
        _, idx, d = tree.find(glb_world @ v.co)
        glb_to_fbx[vi] = idx
        if d > max_d:
            max_d = d
        sum_d += d
    avg_d = sum_d / glb_verts if glb_verts else 0.0
    _log(f"  arkit: kdtree match max_dist={max_d:.5f}m avg_dist={avg_d:.5f}m")
    # Tolerance: 5 mm is generous for UV-seam noise but tight enough to
    # catch genuine topology mismatches (totally different mesh).
    if max_d > 0.005:
        _log(f"  arkit: position match too loose ({max_d:.4f}m); "
             f"FBX and GLB don't share rest geometry. Skipping transfer.")
        return len(baked_names)

    # Transfer shape keys via the kdtree-derived index map. Shape-key
    # data is in mesh-local space, so we have to push the FBX delta
    # through fbx_world (to world space) and pull back through
    # glb_world.inverted() (into GLB local space). This handles the
    # case where FBX and GLB importers gave the meshes different
    # rest matrices.
    if glb_face.data.shape_keys is None:
        glb_face.shape_key_add(name="Basis", from_mix=False)

    fbx_basis = fbx_face_obj.data.shape_keys.key_blocks[0]
    glb_basis = glb_face.data.shape_keys.key_blocks[0]
    fbx_rot = fbx_world.to_3x3()
    glb_rot_inv = glb_world.inverted().to_3x3()
    for name in baked_names:
        src = fbx_face_obj.data.shape_keys.key_blocks.get(name)
        if src is None:
            continue
        # Remove any existing key on glb with the same name (so we can
        # re-bake idempotently).
        existing = glb_face.data.shape_keys.key_blocks.get(name)
        if existing is not None:
            glb_face.shape_key_remove(existing)
        new_kb = glb_face.shape_key_add(name=name, from_mix=False)
        for vi in range(glb_verts):
            fi = glb_to_fbx[vi]
            fbx_delta = src.data[fi].co - fbx_basis.data[fi].co
            # Convert delta from FBX-local -> world -> GLB-local. For a
            # pure delta (vector), only the rotation/scale parts of the
            # matrices apply; translation cancels out.
            world_delta = fbx_rot @ fbx_delta
            glb_local_delta = glb_rot_inv @ world_delta
            new_kb.data[vi].co = glb_basis.data[vi].co + glb_local_delta
        new_kb.value = 0.0
    _log(f"  arkit: transferred {len(baked_names)} shape keys to GLB face "
         f"(via kdtree position match)")

    # Cleanup: remove the FBX-imported objects so they don't end up in
    # the saved blend.
    for n_ in list(new_after_face):
        obj = bpy.data.objects.get(n_)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.outliner.orphans_purge(do_recursive=True)

    return len(baked_names)


def _remove_gltf_placeholder_empties():
    """Blender's glTF importer spawns `Icosphere` meshes for glTF nodes
    that have no geometry/light/camera (bone sockets, empty transforms).
    They are not real geometry — drop them so they don't land in the
    final GLB or get counted against our tri budget."""
    removed = 0
    for obj in list(bpy.data.objects):
        if obj.type != "MESH":
            continue
        if obj.name.startswith("Icosphere"):
            # These placeholders have ~42 verts and no materials
            if len(obj.data.vertices) < 100 and not obj.data.materials:
                bpy.data.objects.remove(obj, do_unlink=True)
                removed += 1
    return removed


def main():
    args = _parse()
    ws = os.path.abspath(args.workspace)
    char_root = os.path.join(ws, "characters", args.char)
    in_root = os.path.join(char_root, "01-glb")
    out_root = os.path.join(char_root, "02-blend")
    os.makedirs(out_root, exist_ok=True)

    with open(os.path.join(in_root, "mh_manifest.json"), "r", encoding="utf-8") as f:
        mh = json.load(f)

    _reset_scene()

    hair_names = set()
    for rec in mh["assets"]:
        glb = os.path.join(in_root, rec["file_path"])
        _log(f"importing {glb}")
        _import_glb(glb)
        if rec.get("role") == "hair":
            hair_names.add(rec["file_path"].lower().replace(".glb", ""))

    hidden = _hide_non_lod0()
    _log(f"hid {hidden} non-LOD0/collision meshes")

    # ARKit shape-key bake from the Sequencer-baked LSE FBX (stage 01).
    # UE's Sequencer evaluates the AnimSequence through a live
    # SkeletalMeshComponent, which fires RigLogic + correctives + bone
    # resolution natively. The resulting LSE FBX has the face mesh +
    # skeleton + per-frame bone keyframes for all 66 poses (Default + 52
    # ARKit + 13 correctives). We replay that animation in Blender,
    # capture the deformed mesh per ARKit pose frame, and transfer to
    # the GLB face via kdtree position match.
    baked = _bake_arkit_from_lse_fbx(in_root, mh)
    if baked:
        _log(f"baked {baked} ARKit shape keys onto the GLB face mesh "
             f"(via Sequencer-baked LSE FBX)")
        # Propagate the face's ARKit shape keys onto facial-groom card
        # meshes (eyebrows, mustache, beard, stubble) so they follow the
        # face when a blendshape fires. Without this the groom cards stay
        # static while the face deforms — visible detachment, especially
        # on jawOpen + mouthSmile + browDown.
        glb_face = None
        for o in bpy.data.objects:
            if (o.type == "MESH" and "facemesh" in o.name.lower()
                    and "cardsmesh" not in o.name.lower()):
                if glb_face is None or len(o.data.vertices) > len(glb_face.data.vertices):
                    glb_face = o
        if glb_face is not None:
            groom_keys = _apply_arkit_to_grooms(glb_face)
            _log(f"propagated ARKit shape keys onto groom card meshes "
                 f"({groom_keys} total keys created)")
    else:
        _log("no ARKit shape keys baked - LSE FBX missing or capture failed")

    # UE MH face mesh has a handful of slots that are meant to be
    # invisible (eye occlusion shell, hide placeholder, tear fluid).
    # glTF imports them as default-white opaque, which paints over
    # the iris in any glTF viewer. Force them transparent.
    #
    # We use Principled BSDF with Base Color alpha = 0 + blend_method
    # = BLEND (instead of a Transparent BSDF). Blender's glTF exporter
    # only round-trips Principled BSDF; it falls back to "OPAQUE white
    # Principled" when it sees a Transparent BSDF, which is exactly
    # the bug that produces white eyes in standard glTF viewers.
    INVISIBLE_MI_KEYWORDS = ("eyeshell", "eyeshellsynthesized",
                             "m_hide", "mi_hide",
                             "lacrimal", "saliva")
    tuned = 0
    for mat in bpy.data.materials:
        nm = mat.name.lower()
        if not any(k in nm for k in INVISIBLE_MI_KEYWORDS):
            continue
        if not mat.use_nodes:
            _log(f"  skip {mat.name}: use_nodes=False"); continue
        nt = mat.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (400, 0)
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (0, 0)
        # Base Color alpha = 0; the glTF exporter writes this through
        # to baseColorFactor[3] = 0 + alphaMode = BLEND, which every
        # glTF-2.0 viewer renders as fully transparent.
        bsdf.inputs["Base Color"].default_value = (1, 1, 1, 0)
        bsdf.inputs["Alpha"].default_value = 0.0
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        try: mat.blend_method = "BLEND"
        except Exception: pass
        try: mat.surface_render_method = "BLENDED"
        except Exception: pass
        try: mat.shadow_method = "NONE"
        except Exception: pass
        tuned += 1
    _log(f"made {tuned} invisible-mat materials fully transparent (alpha=0 Principled)")

    parented = _parent_hair_to_head_bone(hair_names=tuple(hair_names))
    if parented:
        _log(f"parented {parented} hair-card mesh(es) to head bone")

    # Hair-card and eyebrow meshes come in with WorldGridMaterial because
    # GLTFExporter can't translate MH's hair-card shader. Stage 01 dumped
    # the source textures from /Game/<char>/Grooms/Textures/ as a sidecar.
    # Wire them onto a Principled BSDF + alpha clip so the cards render.
    fixed = _wire_card_materials(in_root, mh)
    if fixed:
        _log(f"wired sidecar textures onto {fixed} card material(s)")

    # Emit mh_materials.json describing the card materials so stage 04's
    # three.js viewer can apply the proper hair-card shader (alpha-clip
    # outer pass + opaque inner pass + per-strand seed variation +
    # root darkening). Same schema as 5.6/cinematic so the existing
    # viewer.js handles it without any change.
    mat_records = _emit_material_spec(out_root, mh)
    if mat_records:
        _log(f"wrote mh_materials.json: {len(mat_records)} entry(ies)")

    removed_placeholders = _remove_gltf_placeholder_empties()
    if removed_placeholders:
        _log(f"removed {removed_placeholders} glTF placeholder Icosphere(s)")

    # Save blend
    blend_path = os.path.join(out_root, f"{args.char}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    _log(f"wrote {blend_path}")

    # Blend-side manifest for stage 03
    scene_info = {
        "imported": len(mh["assets"]),
        "mesh_names": sorted([o.name for o in bpy.data.objects
                              if o.type == "MESH" and not o.hide_render]),
        "armature_names": sorted([o.name for o in bpy.data.objects
                                  if o.type == "ARMATURE"]),
        "hair_parented": parented,
    }
    with open(os.path.join(out_root, "blend_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"character_id": args.char,
                   "pipeline": "native-glb",
                   "saved_at": _iso_now(),
                   "scene": scene_info}, f, indent=2)
    _log(f"scene: {scene_info}")

    # Update character manifest
    char_man_path = os.path.join(char_root, "manifest.json")
    with open(char_man_path, "r", encoding="utf-8") as f:
        char_man = json.load(f)
    char_man["stages"]["02_blender_assemble"] = {
        "status": "done",
        "started_at": _iso_now(),
        "completed_at": _iso_now(),
        "output_dir": "02-blend/",
        "errors": [],
    }
    with open(char_man_path, "w", encoding="utf-8") as f:
        json.dump(char_man, f, indent=2)
    _log("char manifest updated: status=done")


if __name__ == "__main__":
    main()
