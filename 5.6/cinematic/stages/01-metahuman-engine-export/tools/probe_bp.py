"""
Read-only probe: find the MH Blueprint for a character and list every SkinnedMesh
component with its referenced SkeletalMesh asset path. Writes no files — just logs.

Usage inside UE commandlet (same wrapper as export_mh.py):
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/probe_bp.py -- --char=ada"
    (workspace still comes via MH_PIPELINE_WORKSPACE env var)
"""

import argparse
import json
import os
import sys

import unreal


def _parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    ns = p.parse_args(argv)
    return ns


def _log(m):
    unreal.log(f"[probe-bp] {m}")


def _asset_registry():
    return unreal.AssetRegistryHelpers.get_asset_registry()


def _list_assets_under(content_path, class_short_name):
    ar = _asset_registry()
    try:
        filt = unreal.ARFilter(
            package_paths=[content_path],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", class_short_name)],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[content_path],
            class_names=[class_short_name],
            recursive_paths=True,
        )
    return ar.get_assets(filt) or []


def _dump_all_asset_classes_under(content_path):
    ar = _asset_registry()
    filt = unreal.ARFilter(package_paths=[content_path], recursive_paths=True)
    assets = ar.get_assets(filt) or []
    by_class = {}
    for a in assets:
        cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
        by_class.setdefault(cls, []).append(str(a.package_name) + "." + str(a.asset_name))
    return by_class


def _inspect_bp_components(bp_path):
    """Load a Blueprint, walk its generated class CDO, collect SkinnedMeshComponent refs."""
    bp = unreal.EditorAssetLibrary.load_asset(bp_path)
    if bp is None:
        _log(f"could not load BP {bp_path}")
        return []

    _log(f"BP class: {type(bp).__name__}")
    found = []

    # Try: get the generated class CDO; enumerate its components
    gen_cls = None
    try:
        gen_cls = bp.generated_class()
    except Exception:
        try:
            gen_cls = bp.get_editor_property("generated_class")
        except Exception:
            gen_cls = None

    cdo = None
    if gen_cls is not None:
        try:
            cdo = unreal.get_default_object(gen_cls)
        except Exception:
            cdo = None

    if cdo is None:
        _log("no CDO accessible; trying SubobjectDataSubsystem")
        try:
            sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            handles = sds.k2_gather_subobject_data_for_blueprint(bp)
            for h in handles or []:
                data = sds.k2_find_subobject_data_from_handle(h)
                obj = data.get_object() if data else None
                if obj is None:
                    continue
                if isinstance(obj, unreal.SkinnedMeshComponent):
                    mesh = obj.get_editor_property("skeletal_mesh")
                    found.append({
                        "component": obj.get_name(),
                        "class": type(obj).__name__,
                        "skeletal_mesh": mesh.get_path_name() if mesh else None,
                    })
        except Exception as e:
            _log(f"SubobjectDataSubsystem probe failed: {e}")
    else:
        # Walk CDO component properties
        try:
            components = cdo.get_editor_property("blueprint_created_components") or []
        except Exception:
            components = []
        for c in components:
            if isinstance(c, unreal.SkinnedMeshComponent):
                mesh = None
                try:
                    mesh = c.get_editor_property("skeletal_mesh")
                except Exception:
                    pass
                found.append({
                    "component": c.get_name(),
                    "class": type(c).__name__,
                    "skeletal_mesh": mesh.get_path_name() if mesh else None,
                })

    return found


def main():
    args = _parse_args()
    char = args.char
    mh_folder = f"/Game/MetaHumans/{char.capitalize()}"

    _log(f"scanning {mh_folder}")
    classes = _dump_all_asset_classes_under(mh_folder)
    _log(f"asset classes under {mh_folder}:")
    for cls, items in sorted(classes.items()):
        _log(f"  {cls}: {len(items)}")
        for p in items[:10]:
            _log(f"    - {p}")
        if len(items) > 10:
            _log(f"    (+{len(items)-10} more)")

    # find any Blueprint under the char folder
    bps = _list_assets_under(mh_folder, "Blueprint")
    _log(f"blueprints under char folder: {len(bps)}")
    for bp in bps:
        bp_path = str(bp.package_name) + "." + str(bp.asset_name)
        _log(f"--- probing BP: {bp_path} ---")
        comps = _inspect_bp_components(bp_path)
        for c in comps:
            _log(f"  {c['class']} {c['component']} -> {c['skeletal_mesh']}")
        _log(f"  ({len(comps)} skinned components)")

    # --- dep walk on BP: every asset the BP references ---
    ar = _asset_registry()
    try:
        opts = unreal.AssetRegistryDependencyOptions(
            include_soft_package_references=True,
            include_hard_package_references=True,
            include_searchable_names=False,
            include_soft_management_references=False,
            include_hard_management_references=False,
        )
    except Exception:
        opts = None
    for bp in bps:
        bp_pkg = str(bp.package_name)
        _log(f"--- BP dep walk: {bp_pkg} ---")
        try:
            deps = ar.get_dependencies(bp_pkg, opts) if opts else ar.get_dependencies(bp_pkg)
            deps = deps or []
        except Exception as e:
            _log(f"  dep walk failed: {e}"); continue
        by_class = {}
        for d in deps:
            dep_pkg = str(d)
            if not dep_pkg.startswith("/Game/"):
                continue
            # resolve class by loading the asset data
            assets = ar.get_assets_by_package_name(dep_pkg) or []
            for a in assets:
                cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
                by_class.setdefault(cls, []).append(dep_pkg + "." + str(a.asset_name))
        for cls in sorted(by_class):
            items = sorted(set(by_class[cls]))
            _log(f"  {cls}: {len(items)}")
            for p in items[:20]:
                _log(f"    - {p}")
            if len(items) > 20:
                _log(f"    (+{len(items)-20} more)")

    # --- dep walk on every GroomAsset under MH folder (find hair card materials) ---
    grooms = _list_assets_under(mh_folder, "GroomAsset")
    _log(f"--- GroomAssets under {mh_folder}: {len(grooms)} ---")
    for g in grooms:
        g_pkg = str(g.package_name)
        _log(f"--- Groom dep walk: {g_pkg} ---")
        try:
            deps = ar.get_dependencies(g_pkg, opts) if opts else ar.get_dependencies(g_pkg)
            deps = deps or []
        except Exception as e:
            _log(f"  dep walk failed: {e}"); continue
        by_class = {}
        for d in deps:
            dep_pkg = str(d)
            if not dep_pkg.startswith("/Game/"):
                continue
            for a in ar.get_assets_by_package_name(dep_pkg) or []:
                cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") else str(a.asset_class)
                by_class.setdefault(cls, []).append(dep_pkg + "." + str(a.asset_name))
        for cls in sorted(by_class):
            items = sorted(set(by_class[cls]))
            _log(f"  {cls}: {len(items)}")
            for p in items[:20]:
                _log(f"    - {p}")

    # --- deep dep walk on hair MIs + their parent materials --------------------
    _log("--- hair MI transitive dep walk (MI -> parent M -> textures) ---")
    ar_opts = opts
    for mi_path in [
        "/Game/MetaHumans/Ada/Materials/MI_Hair_Cards",
        "/Game/MetaHumans/Ada/Materials/MI_Facial_Hair",
        "/Game/MetaHumans/Ada/Materials/MI_Hair",
        "/Game/MetaHumans/Ada/Materials/MI_Hair_Helmet",
    ]:
        mi = unreal.EditorAssetLibrary.load_asset(mi_path)
        if mi is None:
            _log(f"  {mi_path}: missing"); continue
        _log(f"  MI: {mi_path} (class={type(mi).__name__})")
        # follow the parent chain
        parent = None
        try:
            parent = mi.get_editor_property("parent")
        except Exception:
            pass
        visited = {mi_path}
        chain = [mi]
        while parent is not None and parent.get_path_name() not in visited:
            visited.add(parent.get_path_name())
            chain.append(parent)
            _log(f"    parent: {parent.get_path_name()}")
            try:
                parent = parent.get_editor_property("parent")
            except Exception:
                parent = None
        # texture params via MaterialEditingLibrary on the MI itself
        try:
            names = unreal.MaterialEditingLibrary.get_texture_parameter_names(mi) or []
            _log(f"    texture param names: {list(names)}")
            for n in names:
                try:
                    t = unreal.MaterialEditingLibrary.get_texture_parameter_value(mi, n)
                    _log(f"      {n} -> {t.get_path_name() if t else None}")
                except Exception as e:
                    _log(f"      {n}: probe failed: {e}")
        except Exception as e:
            _log(f"    texture param walk failed: {e}")
        # get_used_textures
        try:
            used = unreal.MaterialEditingLibrary.get_used_textures(mi) or []
            _log(f"    get_used_textures: {len(used)}")
            for t in used:
                _log(f"      {t.get_path_name()}")
        except Exception as e:
            _log(f"    get_used_textures failed: {e}")
        # dep walk on each asset in the chain
        for a in chain:
            p_pkg = a.get_path_name().split(".")[0]
            try:
                deps = ar.get_dependencies(p_pkg, ar_opts) if ar_opts else ar.get_dependencies(p_pkg)
                deps = deps or []
            except Exception:
                deps = []
            tex_deps = []
            for d in deps:
                dep_pkg = str(d)
                if not dep_pkg.startswith("/Game/"):
                    continue
                for ad in ar.get_assets_by_package_name(dep_pkg) or []:
                    cls = str(ad.asset_class_path.asset_name) if hasattr(ad, "asset_class_path") else str(ad.asset_class)
                    if cls == "Texture2D":
                        tex_deps.append(dep_pkg + "." + str(ad.asset_name))
            _log(f"    dep-walk textures for {p_pkg}: {len(tex_deps)}")
            for tp in tex_deps[:20]:
                _log(f"      {tp}")

    # --- dep walk directly on each GroomAsset (find per-group card textures) ---
    _log("--- GroomAsset direct dep walk ---")
    for g_path in [
        "/Game/MetaHumans/Ada/FemaleHair/Hair/Hair_S_Coil.Hair_S_Coil",
        "/Game/MetaHumans/Ada/FemaleHair/Hair/Eyebrows_M_Thin.Eyebrows_M_Thin",
        "/Game/MetaHumans/Ada/FemaleHair/Hair/Eyelashes_L_SlightCurl.Eyelashes_L_SlightCurl",
        "/Game/MetaHumans/Ada/FemaleHair/Hair/Peachfuzz_M_Thin.Peachfuzz_M_Thin",
    ]:
        g = unreal.EditorAssetLibrary.load_asset(g_path)
        if g is None:
            _log(f"  {g_path}: missing"); continue
        _log(f"  Groom: {g_path} (class={type(g).__name__})")
        pkg = g_path.split(".")[0]
        try:
            deps = ar.get_dependencies(pkg, ar_opts) if ar_opts else ar.get_dependencies(pkg)
            deps = deps or []
        except Exception:
            deps = []
        by_class = {}
        for d in deps:
            dep_pkg = str(d)
            if not dep_pkg.startswith("/Game/"):
                continue
            for ad in ar.get_assets_by_package_name(dep_pkg) or []:
                cls = str(ad.asset_class_path.asset_name) if hasattr(ad, "asset_class_path") else str(ad.asset_class)
                by_class.setdefault(cls, []).append(dep_pkg + "." + str(ad.asset_name))
        for cls in sorted(by_class):
            items = sorted(set(by_class[cls]))
            _log(f"    {cls}: {len(items)}")
            for p in items[:15]:
                _log(f"      - {p}")
        # Try to read hair_groups_cards directly
        try:
            groups = g.get_editor_property("hair_groups_cards") or []
            _log(f"    hair_groups_cards count: {len(groups)}")
            for i, grp in enumerate(groups):
                try:
                    textures = grp.get_editor_property("textures")
                    for tfield in ("depth_texture", "coverage_texture", "tangent_texture",
                                   "attributes_texture", "material_id_texture", "auxilary_data_texture",
                                   "layout_textures"):
                        try:
                            t = textures.get_editor_property(tfield)
                            if t:
                                _log(f"      group[{i}].{tfield}: {t.get_path_name()}")
                        except Exception:
                            pass
                except Exception as e:
                    _log(f"      group[{i}] textures read failed: {e}")
        except Exception as e:
            _log(f"    hair_groups_cards probe failed: {e}")

    # --- inspect body MaterialInstance vector parameters -----------------------
    _log("--- vector parameters on outfit materials ---")
    for mi_path in [
        "/Game/MetaHumans/Common/Materials/Shirt/Female/M_f_top_shirt.M_f_top_shirt",
        "/Game/MetaHumans/Common/Materials/Slacks/M_btm_slacks.M_btm_slacks",
        "/Game/MetaHumans/Common/Materials/Flats/M_shs_flats.M_shs_flats",
    ]:
        mi = unreal.EditorAssetLibrary.load_asset(mi_path)
        if mi is None:
            _log(f"  {mi_path}: not found"); continue
        _log(f"  {mi_path} (class={type(mi).__name__})")
        mic = unreal.MaterialEditingLibrary
        try:
            names = mic.get_vector_parameter_names(mi) or []
            for n in names:
                v = mic.get_material_vector_parameter_value(mi, n)
                _log(f"    vec {n} = ({v.r:.3f},{v.g:.3f},{v.b:.3f},{v.a:.3f})")
        except Exception as e:
            _log(f"    vec-param probe failed: {e}")
        try:
            snames = mic.get_scalar_parameter_names(mi) or []
            for n in snames:
                sv = mic.get_material_scalar_parameter_value(mi, n)
                _log(f"    scalar {n} = {sv}")
        except Exception as e:
            _log(f"    scalar-param probe failed: {e}")


if __name__ == "__main__":
    main()
