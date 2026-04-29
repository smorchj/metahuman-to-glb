"""
Headless FBX + TGA exporter for a 5.7 MetaHuman that's already been
assembled (i.e. `build_meta_human` was run and the resulting
/Game/<Name>/ package is saved to disk).

Runs as a commandlet — no Slate ticks, no texture cloud fetch — so it's
fast and can be scripted from the pipeline launcher the same way 5.6
does. The heavy `build_meta_human` assemble step happens in a separate
GUI-startup invocation; this script only reads what's already on disk.

Usage:
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/export_textures.py -- --folder=/Game/Kleb \
                 --output-dir=C:/tmp/mh/out --name=Kleb"
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import unreal


def log(msg: str) -> None:
    unreal.log(f"[mh-export57] {msg}")


# ---------------------------------------------------------------------------- #
# Epic 5.7 Optimized naming conventions — the deterministic mapping contract   #
# ---------------------------------------------------------------------------- #

def _mi_kind(mi_leaf: str) -> str:
    """Pattern-match the MaterialInstance's leaf name to a slot kind.

    Epic names baked MIs consistently in the Optimized pipeline; the MI name
    is the only reliable signal for what a slot represents (the slot_name on
    the FBX mesh is generic 'head_shader_shader' style and doesn't
    distinguish skin from teeth from eye)."""
    n = (mi_leaf or "").lower()
    if n.startswith("mi_body_baked"):       return "skin_body"
    if n.startswith("mi_head_baked"):       return "skin_face"
    if n.startswith("mi_face_skin_baked"):  return "skin_face"
    if n.startswith("mi_teeth_baked"):      return "teeth"
    if n.startswith("mi_eyel_baked"):       return "eye_iris_L"
    if n.startswith("mi_eyer_baked"):       return "eye_iris_R"
    if "eyeshell" in n:                     return "eye_occlusion"
    if "eyeedge" in n or "lacrimal" in n:   return "wet"
    if "eyelash" in n:                      return "eyelashes"
    if "cartilage" in n:                    return "cartilage"
    if "saliva" in n:                       return "wet"
    if "m_hide" == n:                       return "hide"
    if "cloth" in n or n.startswith("mid_m_dg_"): return "cloth"
    if "hair_cards" in n or "mi_hair" in n: return "hair"
    if "facial_hair" in n:                  return "facial_hair"
    if "worldgridmaterial" in n:            return "placeholder"
    return "unknown"


def _tex_role(tex_leaf: str, mi_kind: str) -> str | None:
    """Pattern-match a texture's leaf name to the PBR role it feeds, scoped
    by the owning MI's kind. Returns None for textures that don't belong on
    this slot (e.g. a teeth BC pulled in via dep walk on a skin slot).

    Epic 5.7 Optimized naming convention (confirmed on Kleb build):
      Body:       T_Body_BC_VT, T_Body_N_VT, T_Body_SRMF_VT, T_Body_Scatter_VT
      Face skin:  T_Head_LOD{N}_BC_VT, _N_VT, _SRMF[_VT], _Scatter_VT,
                  T_BakedNormal_LOD{N}  (wrinkle/LOD-delta normal)
                  T_Face_Basecolor_Animated_CM{1,2,3}  (LOD0 animated BC layers)
                  T_Face_Normal_Animated_WM{1,2,3}     (LOD0 animated N layers)
      Teeth:      T_Teeth_BC, T_Teeth_N, T_Teeth_SRM, T_Teeth_Scatter
      Eye iris:   T_EyeIris{L,R}_BC, _N
      Eye sclera: T_EyeSclera{L,R}_BC, _N
      Hair:       <Groom>_RootUVSeedCoverage.tga (alpha)
                  <Groom>_CardsAtlas_Attribute.tga (alpha/coverage, packed)
                  <Groom>_CardsAtlas_Tangent.tga (tangent space)
                  <Groom>_ColorXYDepthGroupID.tga (colour packing)"""
    t = (tex_leaf or "").lower()

    # Reject textures that are clearly from a different slot-kind's bucket.
    if mi_kind != "skin_body" and t.startswith("t_body_"):            return None
    if mi_kind != "skin_face" and (t.startswith("t_head_") or t.startswith("t_bakednormal_")
                                   or t.startswith("t_face_")):       return None
    if mi_kind != "teeth" and t.startswith("t_teeth_"):                return None
    if mi_kind not in ("eye_iris_L",) and t.startswith("t_eyeirisl"):  return None
    if mi_kind not in ("eye_iris_R",) and t.startswith("t_eyeirisr"):  return None
    if mi_kind != "hair" and mi_kind != "facial_hair":
        if "cardsatlas" in t or "rootuvseedcoverage" in t or "colorxydepth" in t:
            return None
    # WorldGrid default normal is the MH "missing material" placeholder; drop.
    if t.startswith("t_default_material_grid"):                        return None

    # Role by filename pattern (scoped to kind).
    if mi_kind in ("skin_body", "skin_face", "teeth"):
        if "_scatter" in t:                           return "scatter"
        if (t.endswith("_bc_vt.tga") or t.endswith("_bc.tga")
                or "_basecolor_animated_" in t):      return "basecolor"
        if (t.endswith("_n_vt.tga") or t.endswith("_n.tga")
                or "_normal_animated_" in t
                or t.startswith("t_bakednormal_")):   return "normal"
        if (t.endswith("_srmf_vt.tga") or t.endswith("_srmf.tga")
                or t.endswith("_srm.tga")):           return "srmf"

    if mi_kind in ("eye_iris_L", "eye_iris_R"):
        if t.endswith("_bc.tga"):                     return "basecolor"
        if t.endswith("_n.tga"):                      return "normal"

    if mi_kind in ("hair", "facial_hair"):
        if "rootuvseedcoverage" in t:                 return "alpha"
        if "cardsatlas_attribute" in t:               return "alpha"      # R-channel packed
        if "cardsatlas_tangent" in t:                 return "tangent"
        if "colorxydepth" in t:                       return "basecolor"  # best we've got
        if t.endswith("_bc.tga") or t.endswith("_bc_vt.tga"): return "basecolor"
        if t.endswith("_n.tga")  or t.endswith("_n_vt.tga"):  return "normal"

    return None


def _load_texture_by_name(folder: str, asset_name: str):
    """Look up a Texture2D anywhere under `folder` (recursive) by its asset
    name. Returns the Texture2D object or None."""
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Texture2D")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_names=["Texture2D"],
            recursive_paths=True,
        )
    for a in ar.get_assets(filt) or []:
        if str(a.asset_name) == asset_name:
            try:
                return a.get_asset()
            except Exception:
                try:
                    return unreal.EditorAssetLibrary.load_asset(
                        f"{a.package_name}.{a.asset_name}")
                except Exception:
                    return None
    return None


def _expected_textures_for_kind(mi_leaf: str, mi_kind: str, folder: str):
    """Deterministic filename enumeration per MI kind, following Epic's
    5.7 Optimized naming convention. Returns {role: Texture2D} of what
    actually exists in the /Game/<Name>/ folder. Does not walk the MI
    graph — this is the source of truth for the contract.

    The convention (observed empirically on the Kleb build, documented in
    references/asset_paths.md):

      skin_body  (MI_Body_Baked_VT):
          basecolor = T_Body_BC_VT
          normal    = T_Body_N_VT
          srmf      = T_Body_SRMF_VT
          scatter   = T_Body_Scatter_VT

      skin_face  (MI_Face_Skin_Baked_LOD{N}_VT or MI_Head_Baked):
          LOD0 material samples the animated CM/WM stack directly:
              basecolor = T_Face_Basecolor_Animated_CM1
              normal    = T_Face_Normal_Animated_WM1
              scatter   = T_Head_LOD1_Scatter_VT   (nearest baked LOD)
          LOD{N>=1} uses its own baked atlas:
              basecolor = T_Head_LOD{N}_BC_VT
              normal    = T_Head_LOD{N}_N_VT
              srmf      = T_Head_LOD{N}_SRMF_VT    (LOD5to7 has just SRMF, no _VT)
              scatter   = T_Head_LOD{N}_Scatter_VT

      teeth (MI_Teeth_Baked):
          basecolor = T_Teeth_BC
          normal    = T_Teeth_N
          srmf      = T_Teeth_SRM
          scatter   = T_Teeth_Scatter

      eye_iris_L (MI_EyeL_Baked):
          basecolor = T_EyeIrisL_BC
          normal    = T_EyeIrisL_N
      eye_iris_R (MI_EyeR_Baked):
          basecolor = T_EyeIrisR_BC
          normal    = T_EyeIrisR_N
    """
    want = {}
    n = mi_leaf.lower()

    if mi_kind == "skin_body":
        want = {"basecolor": "T_Body_BC_VT", "normal": "T_Body_N_VT",
                "srmf": "T_Body_SRMF_VT", "scatter": "T_Body_Scatter_VT"}

    elif mi_kind == "skin_face":
        if "lod1" in n:
            want = {"basecolor": "T_Head_LOD1_BC_VT",
                    "normal":    "T_Head_LOD1_N_VT",
                    "srmf":      "T_Head_LOD1_SRMF_VT",
                    "scatter":   "T_Head_LOD1_Scatter_VT"}
        elif "lod3" in n:
            want = {"basecolor": "T_Head_LOD3_BC_VT",
                    "normal":    "T_Head_LOD3_N_VT",
                    "srmf":      "T_Head_LOD3_SRMF_VT",
                    "scatter":   "T_Head_LOD3_Scatter_VT"}
        elif "lod5" in n or "lod7" in n or "lod5to7" in n:
            want = {"basecolor": "T_Head_LOD5to7_BC_VT",
                    "normal":    "T_Head_LOD5to7_N_VT",
                    "srmf":      "T_Head_LOD5to7_SRMF",
                    "scatter":   "T_Head_LOD5to7_Scatter_VT"}
        else:
            # LOD0 / MI_Head_Baked — no static baked LOD0 atlas exists;
            # sample the animated stack (CM1/WM1 are the neutral layers).
            want = {"basecolor": "T_Face_Basecolor_Animated_CM1",
                    "normal":    "T_Face_Normal_Animated_WM1",
                    "scatter":   "T_Head_LOD1_Scatter_VT"}

    elif mi_kind == "teeth":
        want = {"basecolor": "T_Teeth_BC", "normal": "T_Teeth_N",
                "srmf": "T_Teeth_SRM", "scatter": "T_Teeth_Scatter"}

    elif mi_kind == "eye_iris_L":
        want = {"basecolor": "T_EyeIrisL_BC", "normal": "T_EyeIrisL_N"}

    elif mi_kind == "eye_iris_R":
        want = {"basecolor": "T_EyeIrisR_BC", "normal": "T_EyeIrisR_N"}

    out = {}
    for role, name in want.items():
        tex = _load_texture_by_name(folder, name)
        if tex is not None:
            out[role] = tex
    return out


def _lod0_preference(a_file: str, b_file: str) -> int:
    """When a skin_face MI references textures for multiple LODs (common —
    the face material shader samples LOD1/3/5 atlas assets via VT), prefer
    the LOD0-relevant texture. Returns -1 if a is preferred, +1 if b is,
    0 if neither is better. LOD0 face material uses the animated CM1 layer
    (no static baked BC_VT for LOD0 exists; Epic relies on the animated
    system for LOD0 base colour)."""
    an, bn = a_file.lower(), b_file.lower()

    def score(n):
        if "_animated_cm1" in n:       return 100   # LOD0 neutral animated
        if "_animated_wm1" in n:       return 100
        if "_lod1_" in n:              return 80
        if "_lod3_" in n:              return 40
        if "_lod5" in n or "_lod5to7_" in n: return 20
        if "_animated_cm" in n:        return 70   # CM2/CM3 = expression layers
        if "_animated_wm" in n:        return 70
        return 50

    sa, sb = score(an), score(bn)
    if sa > sb: return -1
    if sb > sa: return +1
    return 0


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
    """3-stage texture discovery, same approach as 5.6 export_mh.py:
       1. explicit texture parameters on the MI
       2. get_used_textures (runtime-shader texture list)
       3. AssetRegistry dependency walk of the MI's package (catches
          inherited textures the parent material pulls in)."""
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

    try:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        pkg = mi.get_outermost().get_name()
        try:
            opts = unreal.AssetRegistryDependencyOptions(
                include_soft_package_references=True,
                include_hard_package_references=True,
                include_searchable_names=False,
                include_soft_management_references=False,
                include_hard_management_references=False,
            )
            deps = ar.get_dependencies(pkg, opts) or []
        except Exception:
            deps = ar.get_dependencies(pkg) or []
        for dep in deps:
            ds = str(dep)
            if not ds.startswith("/Game/") and not ds.startswith("/MetaHumanCharacter/"):
                continue
            name = ds.rsplit("/", 1)[-1]
            asset = None
            try:
                asset = unreal.EditorAssetLibrary.load_asset(f"{ds}.{name}")
            except Exception:
                try:
                    asset = unreal.EditorAssetLibrary.load_asset(ds)
                except Exception:
                    asset = None
            if isinstance(asset, unreal.Texture2D) and asset not in seen:
                seen.add(asset)
                yield None, asset
    except Exception as e:
        log(f"  dep walk skipped for {mi.get_path_name()}: {e}")


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
            try:
                opts.set_editor_property(k, v)
            except Exception:
                pass
        task.options = opts
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(f"export failed: {asset.get_path_name()} -> {filepath}")


def _list_meshes_under(folder, class_name):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
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
    for a in ar.get_assets(filt) or []:
        try:
            obj = a.get_asset()
        except Exception:
            obj = None
        if obj is None:
            try:
                obj = unreal.EditorAssetLibrary.load_asset(f"{a.package_name}.{a.asset_name}")
            except Exception:
                obj = None
        if obj is not None:
            out.append(obj)
    return out


def _list_hair_card_static_meshes_under(folder):
    """MH hair grooms ship with StaticMesh card fallback meshes named
    `*_CardsMesh_Group<N>_LOD<N>.uasset` (one per LOD). Only LOD0 is exported
    to keep the web GLB small. These are the visible hair for targets where
    Groom strand sim is disabled (browsers)."""
    out = []
    for m in _list_meshes_under(folder, "StaticMesh"):
        name = m.get_name()
        if "CardsMesh" in name and "_LOD0" in name:
            out.append(m)
    return out


def _lookup_groom_for_hair_card(hair_card_mesh):
    """Given a hair-card StaticMesh, find the sibling GroomAsset (stored in
    the same folder, same stem sans `_CardsMesh_Group<N>_LOD<N>`). Grooms hold
    the packed hair-card atlas textures (coverage / color / tangent) that the
    card MI references indirectly."""
    name = hair_card_mesh.get_name()
    idx = name.find("_CardsMesh")
    if idx == -1:
        return None
    groom_name = name[:idx]
    mesh_pkg = str(hair_card_mesh.get_path_name()).split(".")[0]
    folder = mesh_pkg.rsplit("/", 1)[0]
    candidate = f"{folder}/{groom_name}.{groom_name}"
    try:
        return unreal.EditorAssetLibrary.load_asset(candidate)
    except Exception:
        return None


def _groom_texture2d_deps(groom_asset):
    """Return [Texture2D] the GroomAsset references (hair-card atlases)."""
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    pkg = str(groom_asset.get_path_name()).split(".")[0]
    try:
        opts = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True,
            include_hard_package_references=True,
            include_searchable_names=False,
            include_soft_management_references=False,
            include_hard_management_references=False,
        )
        deps = ar.get_dependencies(pkg, opts) or []
    except Exception:
        deps = ar.get_dependencies(pkg) or []
    out = []
    for d in deps:
        ds = str(d)
        if not ds.startswith("/Game/"):
            continue
        for a in ar.get_assets_by_package_name(ds) or []:
            try:
                obj = a.get_asset()
            except Exception:
                obj = None
            if obj is None:
                try:
                    obj = unreal.EditorAssetLibrary.load_asset(f"{ds}.{a.asset_name}")
                except Exception:
                    obj = None
            if isinstance(obj, unreal.Texture2D):
                out.append(obj)
    return out


def _lookup_hair_card_material_for(folder, comp_name):
    """5.7 hair-card MIs live under /Game/<Name>/Grooms/ alongside the cards,
    named `MI_HairCards_<GroomName>.uasset` or similar. Fall back to the
    generic MH shared MI_Hair_Cards under /Script/MetaHumanCharacter. This
    wiring is imperfect in 5.7 since the final material is resolved at
    runtime by the GroomComponent — we pick the best plausible lookup so
    stage 02 has something to crawl for textures."""
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "MaterialInstanceConstant")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[folder],
            class_names=["MaterialInstanceConstant"],
            recursive_paths=True,
        )
    is_facial = any(k in comp_name for k in (
        "eyebrow", "eyelash", "mustache", "moustache",
        "goatee", "beard", "stubble", "sideburn", "chinstrap", "peachfuzz"))
    priorities_facial = ["mi_facial_hair", "mi_facial_hair1", "mi_facial_hair2"]
    priorities_scalp = ["mi_hair_cards", "mi_hair", "mi_hair1", "mi_hair2"]
    priorities = priorities_facial if is_facial else priorities_scalp
    by_name = {}
    for a in ar.get_assets(filt) or []:
        by_name[str(a.asset_name).lower()] = a
    for key in priorities:
        if key in by_name:
            a = by_name[key]
            try:
                return a.get_asset()
            except Exception:
                try:
                    return unreal.EditorAssetLibrary.load_asset(
                        f"{a.package_name}.{a.asset_name}")
                except Exception:
                    return None
    return None


def main() -> int:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    p.add_argument("--folder", required=True,
                   help="/Game/<Name> folder where SkeletalMeshes live")
    p.add_argument("--output-dir", required=True, help="disk folder for FBX + TGA output")
    p.add_argument("--name", default=None,
                   help="character name for the manifest; defaults to folder leaf")
    args = p.parse_args(argv)

    name = args.name or args.folder.rstrip("/").rsplit("/", 1)[-1]
    out_dir = Path(args.output_dir)
    meshes_dir = out_dir / "meshes"
    textures_dir = out_dir / "textures"
    meshes_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)

    meshes = _list_meshes_under(args.folder, "SkeletalMesh")
    log(f"found {len(meshes)} SkeletalMesh(es) under {args.folder}")
    for m in meshes:
        log(f"  skel mesh: {m.get_path_name()}")

    hair_cards = _list_hair_card_static_meshes_under(args.folder)
    log(f"found {len(hair_cards)} hair-card StaticMesh(es) under {args.folder}")
    for m in hair_cards:
        log(f"  hair card: {m.get_path_name()}")

    mesh_records = []
    exported_tex_files = {}     # asset_path -> file_path, to avoid double-export
    seen_tex_assets = set()

    def _candidate_textures(mi):
        """Yield Texture2D objects potentially bound to this MI, via
        explicit texture parameters + shader runtime table + AssetRegistry
        dep walk. Role filtering happens in the caller based on mi_kind."""
        seen_local = set()
        try:
            names = unreal.MaterialEditingLibrary.get_texture_parameter_names(mi) or []
        except Exception:
            names = []
        for n in names:
            try:
                t = unreal.MaterialEditingLibrary.get_texture_parameter_value(mi, n)
            except Exception:
                t = None
            if t is not None and t not in seen_local:
                seen_local.add(t); yield t
        try:
            for t in unreal.MaterialEditingLibrary.get_used_textures(mi) or []:
                if t is not None and t not in seen_local:
                    seen_local.add(t); yield t
        except Exception:
            pass
        try:
            ar = unreal.AssetRegistryHelpers.get_asset_registry()
            pkg = mi.get_outermost().get_name()
            try:
                opts = unreal.AssetRegistryDependencyOptions(
                    include_soft_package_references=True,
                    include_hard_package_references=True,
                    include_searchable_names=False,
                    include_soft_management_references=False,
                    include_hard_management_references=False,
                )
                deps = ar.get_dependencies(pkg, opts) or []
            except Exception:
                deps = ar.get_dependencies(pkg) or []
            for dep in deps:
                ds = str(dep)
                if not (ds.startswith("/Game/") or ds.startswith("/MetaHumanCharacter/")):
                    continue
                leaf = ds.rsplit("/", 1)[-1]
                try:
                    a = unreal.EditorAssetLibrary.load_asset(f"{ds}.{leaf}")
                except Exception:
                    try:
                        a = unreal.EditorAssetLibrary.load_asset(ds)
                    except Exception:
                        a = None
                if isinstance(a, unreal.Texture2D) and a not in seen_local:
                    seen_local.add(a); yield a
        except Exception:
            pass

    def _export_tex(tex):
        """Export a Texture2D as TGA once; return the rel path."""
        tp = tex.get_path_name()
        if tp in exported_tex_files:
            return exported_tex_files[tp]
        rel = f"textures/{tex.get_name()}.tga"
        abs_ = str(textures_dir / f"{tex.get_name()}.tga")
        try:
            _run_export_task(tex, abs_)
            exported_tex_files[tp] = rel
            seen_tex_assets.add(tex)
            return rel
        except Exception as e:
            log(f"  TGA error for {tp}: {e}")
            return None

    def _build_slots_v2(mesh, forced_kind=None, extra_textures_by_role=None):
        """Return a list of deterministic slot records for this mesh.

        Source of truth is the expected-filename table
        `_expected_textures_for_kind` — MI leaf name + kind select a fixed
        set of texture names, which we look up by asset name under the
        character's /Game/<Name>/ folder. Zero guessing in either direction.
        """
        slots_v2 = []
        for slot_index, (slot_name, mi) in enumerate(_iter_materials(mesh)):
            mi_leaf = mi.get_name()
            kind = forced_kind or _mi_kind(mi_leaf)
            chosen = {}     # role -> Texture2D
            chosen.update(_expected_textures_for_kind(mi_leaf, kind, args.folder))
            # Merge in manually-attached textures (e.g. groom atlas for hair).
            for role, txs in (extra_textures_by_role or {}).items():
                if role not in chosen and txs:
                    chosen[role] = txs[0]
            textures_out = {}
            for role, tex in chosen.items():
                rel = _export_tex(tex)
                if rel:
                    textures_out[role] = rel
            slots_v2.append({
                "slot_index": slot_index,
                "slot_name": slot_name,
                "material": mi.get_path_name(),
                "mi_leaf": mi_leaf,
                "kind": kind,
                "textures": textures_out,
            })
        return slots_v2

    # --- SkeletalMesh export (body, face, outfits) -------------------------
    for mesh in meshes:
        mname = mesh.get_name()
        fbx_rel = f"meshes/{mname.lower()}.fbx"
        try:
            _run_export_task(mesh, str(meshes_dir / f"{mname.lower()}.fbx"))
            log(f"  + {fbx_rel}")
        except Exception as e:
            log(f"  FBX error for {mesh.get_path_name()}: {e}")
            continue
        # Infer mesh-level role so stage 02 can group slots.
        mn = mname.lower()
        if "facemesh" in mn:      role = "face"
        elif "bodymesh" in mn:    role = "body"
        elif "outfit" in mn:      role = "outfit"
        else:                     role = "other"
        slots_v2 = _build_slots_v2(mesh)
        mesh_records.append({
            "component": mname.lower(),
            "role": role,
            "mesh_kind": "skeletal",
            "asset_path": mesh.get_path_name(),
            "fbx_path": fbx_rel,
            "slots": slots_v2,
        })

    # --- Hair-card StaticMesh export ---------------------------------------
    # These meshes carry only a placeholder WorldGridMaterial in their own
    # static_materials; the real MI is wired by the GroomComponent at
    # runtime. We route the groom's dep textures in via extra_textures_by_role
    # so the slots_v2 record still gets a deterministic alpha/basecolor.
    for mesh in hair_cards:
        mname = mesh.get_name()
        fbx_rel = f"meshes/{mname.lower()}.fbx"
        try:
            _run_export_task(mesh, str(meshes_dir / f"{mname.lower()}.fbx"))
            log(f"  + {fbx_rel}")
        except Exception as e:
            log(f"  FBX error for {mesh.get_path_name()}: {e}")
            continue

        is_facial = any(k in mname.lower() for k in (
            "eyebrow", "eyelash", "mustache", "moustache",
            "goatee", "beard", "stubble", "sideburn", "chinstrap"))
        forced_kind = "facial_hair" if is_facial else "hair"

        # Pull groom atlas textures (coverage / tangent / color) — classify
        # each by filename and feed into slots_v2 under the correct role.
        groom = _lookup_groom_for_hair_card(mesh)
        extra_by_role = {}
        if groom is not None:
            for tex in _groom_texture2d_deps(groom):
                role = _tex_role(tex.get_name(), forced_kind)
                if role:
                    extra_by_role.setdefault(role, []).append(tex)

        slots_v2 = _build_slots_v2(mesh, forced_kind=forced_kind,
                                   extra_textures_by_role=extra_by_role)
        mesh_records.append({
            "component": mname.lower(),
            "role": "hair",
            "mesh_kind": "static",
            "asset_path": mesh.get_path_name(),
            "fbx_path": fbx_rel,
            "slots": slots_v2,
        })

    # Flat texture list for backwards-compat / cross-check (debug). Not used
    # by stage 02 — stage 02 reads slots[*].textures directly.
    flat_textures = [
        {"asset_path": ap, "file_path": rel}
        for ap, rel in sorted(exported_tex_files.items())
    ]

    manifest = {
        "schema": "mh_manifest.v2",
        "ue_version": "5.7",
        "character": name,
        "folder": args.folder,
        "meshes": mesh_records,
        "textures": flat_textures,
    }
    manifest_path = out_dir / "mh_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"wrote {manifest_path}  "
        f"({len(mesh_records)} meshes, {len(flat_textures)} textures)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        sys.exit(1)
