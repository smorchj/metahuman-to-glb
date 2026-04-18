"""
Stage 01 / UE 5.6.1 — MetaHuman → FBX + textures + manifest.

Runs inside UnrealEditor-Cmd.exe as:
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/export_mh.py -- --char=<id> --workspace=<abs workspace>"

Can also be run from inside an open editor by pasting this file's contents into the
Output Log's Python prompt, then calling main(char="ada", workspace=r"...").

v1 scope:
- export body + head + teeth + eyes + eyelashes as FBX
- export every Texture2D referenced by those meshes' materials
- export shared skeleton once, into _shared/<ue_ver>/skeleton/
- skip groom hair (recorded as unsupported in manifest)
- write mh_manifest.json

Nothing outside `/Game/MetaHumans/<CharName>/` and `/Game/MetaHumans/Common/` is touched.
"""

import argparse
import datetime as _dt
import json
import os
import sys
import traceback

import unreal  # provided by UE Python

# ---------------------------------------------------------------------------- #
# Arg parsing (UE passes args after `--` on the -script= string)               #
# ---------------------------------------------------------------------------- #

def _parse_args():
    # UE's -script= parser mangles paths with backslashes (treats \0, \5, \t as escapes)
    # and splits on whitespace, which our workspace path has ("Metahuman to GLB"). So the
    # workspace comes in via env var MH_PIPELINE_WORKSPACE; only --char goes on CLI.
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True, help="character id, e.g. 'ada'")
    p.add_argument("--workspace", required=False, default=None,
                   help="(optional) pipeline workspace root; falls back to MH_PIPELINE_WORKSPACE env")
    ns = p.parse_args(argv)
    if not ns.workspace:
        ns.workspace = os.environ.get("MH_PIPELINE_WORKSPACE")
    if not ns.workspace:
        raise RuntimeError(
            "workspace path not provided — pass --workspace or set MH_PIPELINE_WORKSPACE"
        )
    return ns


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #

def _log(msg):
    unreal.log(f"[mh-export] {msg}")


def _iso_now():
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def _asset_registry():
    return unreal.AssetRegistryHelpers.get_asset_registry()


def _infer_mesh_role(asset_path):
    """Best-effort classification of a SkeletalMesh by its /Game/ path segments.
    Used only to label mesh_records for stage 02; never affects export correctness."""
    p = asset_path.lower()
    for segment, role in (
        ("/face/",    "face"),
        ("/body/",    "body"),
        ("/tops/",    "top"),
        ("/bottoms/", "bottom"),
        ("/shoes/",   "shoes"),
        ("/gloves/",  "gloves"),
        ("/hats/",    "hat"),
        ("/hair/",    "hair"),
    ):
        if segment in p:
            return role
    return "other"


def _bp_dependency_packages(mh_folder):
    """Return the set of /Game/ package paths the character BP hard/soft-references.
    Empty set if no BP found — caller can fall back to a folder scan."""
    ar = _asset_registry()
    # find the one BP under the MH folder
    try:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Blueprint")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_names=["Blueprint"],
            recursive_paths=True,
        )
    bps = ar.get_assets(filt) or []
    if not bps:
        return set()
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
    pkgs = set()
    for bp in bps:
        bp_pkg = str(bp.package_name)
        try:
            deps = ar.get_dependencies(bp_pkg, opts) if opts else ar.get_dependencies(bp_pkg)
        except Exception:
            deps = []
        for d in deps or []:
            s = str(d)
            if s.startswith("/Game/"):
                pkgs.add(s)
    return pkgs


def _lookup_groom_for_hair_card(hair_card_mesh):
    """Given a hair-card StaticMesh (e.g. Hair_S_Coil_CardsMesh_Group0_LOD0), find
    the sibling GroomAsset (Hair_S_Coil) in the same folder. MH stores each hair
    groom's atlas textures (coverage / color / tangent) as Texture2D refs on the
    Groom — not on the card mesh or its MaterialInstance — so we follow this
    pointer to reach them."""
    mesh_name = hair_card_mesh.get_name()
    # strip the _CardsMesh_Group<N>_LOD<N> suffix to get the groom name
    stem = mesh_name
    idx = stem.find("_CardsMesh")
    if idx == -1:
        return None
    groom_name = stem[:idx]
    mesh_pkg = str(hair_card_mesh.get_path_name()).split(".")[0]
    folder = mesh_pkg.rsplit("/", 1)[0]
    candidate = f"{folder}/{groom_name}.{groom_name}"
    try:
        g = unreal.EditorAssetLibrary.load_asset(candidate)
    except Exception:
        g = None
    return g


def _groom_texture2d_deps(groom_asset):
    """Return [unreal.Texture2D ...] the GroomAsset references. These are the
    packed hair-card atlases: *_RootUVSeedCoverage (alpha), *_ColorXYDepthGroupID,
    *_CardsAtlas_Tangent, plus strand-space textures used by the Niagara hair system
    (we record them all; stage 02 filters by name)."""
    ar = _asset_registry()
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
    for d in deps or []:
        dep_pkg = str(d)
        if not dep_pkg.startswith("/Game/"):
            continue
        for a in ar.get_assets_by_package_name(dep_pkg) or []:
            cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") \
                  else str(a.asset_class)
            if cls != "Texture2D":
                continue
            try:
                t = a.get_asset()
            except Exception:
                t = None
            if t is None:
                try:
                    t = unreal.EditorAssetLibrary.load_asset(dep_pkg + "." + str(a.asset_name))
                except Exception:
                    t = None
            if t is not None:
                out.append(t)
    return out


def _lookup_hair_card_material_for(mh_folder, comp_name):
    """MH hair-card StaticMeshes have placeholder WorldGridMaterial in their
    static_materials slots (the real material is assigned at runtime by the
    GroomComponent). The real material lives under /Game/MetaHumans/<Name>/Materials/
    as `MI_Hair_Cards` (scalp hair) or `MI_Facial_Hair` (eyebrows/lashes).

    Returns the unreal.MaterialInstance, or None if nothing suitable was found.
    Selection is by component name: 'eyebrow'/'eyelash' → facial hair MI, everything
    else (head-hair groups) → cards MI."""
    ar = _asset_registry()
    try:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "MaterialInstanceConstant")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_names=["MaterialInstanceConstant"],
            recursive_paths=True,
        )
    want_facial = any(k in comp_name for k in ("eyebrow", "eyelash"))
    priorities = (["mi_facial_hair"] if want_facial
                  else ["mi_hair_cards", "mi_hair1", "mi_hair2", "mi_hair"])
    by_name = {}
    for a in ar.get_assets(filt) or []:
        by_name[str(a.asset_name).lower()] = a
    for name_key in priorities:
        if name_key in by_name:
            a = by_name[name_key]
            full = str(a.package_name) + "." + str(a.asset_name)
            try:
                return a.get_asset()
            except Exception:
                try:
                    return unreal.EditorAssetLibrary.load_asset(full)
                except Exception:
                    return None
    return None


def _list_hair_card_static_meshes_from_bp(mh_folder):
    """Return [unreal.StaticMesh ...] for MH hair cards (StaticMesh assets named
    '*CardsMesh*_LOD0'). These are the fallback meshes used where Groom is disabled
    (web targets — no Niagara strand sim). They live under
      /Game/MetaHumans/<Name>/FemaleHair/Hair/   (or MaleHair/Hair/)
    and each group has LOD0..LOD4 siblings — we only keep LOD0 for v1.

    Discovery: BP dep walk does NOT reach them (the character BP references Groom
    assets; the StaticMesh cards are referenced internally by the Groom, which the
    AssetRegistry doesn't surface as a BP dependency). We therefore folder-scan the
    MH folder recursively for StaticMesh and pick the CardsMesh_LOD0 subset.
    Groom curves themselves are deferred in v1 (no free Alembic exporter path)."""
    ar = _asset_registry()
    out = []
    seen = set()

    try:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "StaticMesh")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[mh_folder],
            class_names=["StaticMesh"],
            recursive_paths=True,
        )
    for a in ar.get_assets(filt) or []:
        asset_name = str(a.asset_name)
        if "CardsMesh" not in asset_name:
            continue
        if "_LOD0" not in asset_name:
            continue
        full = str(a.package_name) + "." + asset_name
        if full in seen:
            continue
        seen.add(full)
        obj = None
        try:
            obj = a.get_asset()
        except Exception:
            pass
        if obj is None:
            try:
                obj = unreal.EditorAssetLibrary.load_asset(full)
            except Exception:
                obj = None
        if obj is not None:
            out.append(obj)
    return out


def _iter_materials_on_static_mesh(mesh):
    """Yield (slot_name, material_interface) for a StaticMesh. UE StaticMesh uses
    FStaticMaterial (not FSkeletalMaterial) — same fields, different type."""
    slot_mats = None
    try:
        slot_mats = mesh.get_editor_property("static_materials")
    except Exception:
        pass
    if not slot_mats:
        try:
            slot_mats = mesh.static_materials
        except Exception:
            slot_mats = []
    for sm in slot_mats or []:
        try:
            mi = sm.get_editor_property("material_interface")
            slot = str(sm.get_editor_property("material_slot_name"))
        except Exception:
            mi = getattr(sm, "material_interface", None)
            slot = str(getattr(sm, "material_slot_name", "") or "")
        if mi is not None:
            yield slot, mi


def _list_skeletal_meshes_from_bp(mh_folder):
    """Collect SkeletalMesh assets referenced by the MH character's Blueprint. This
    catches clothing / shoes / accessories that live outside /Game/MetaHumans/<Name>/
    (e.g. shared outfits under /Game/MetaHumans/Common/Female/.../Tops/Shirt/)."""
    ar = _asset_registry()
    out = []
    seen = set()
    for pkg in _bp_dependency_packages(mh_folder):
        for a in ar.get_assets_by_package_name(pkg) or []:
            cls = str(a.asset_class_path.asset_name) if hasattr(a, "asset_class_path") \
                  else str(a.asset_class)
            if cls != "SkeletalMesh":
                continue
            full = pkg + "." + str(a.asset_name)
            if full in seen:
                continue
            seen.add(full)
            obj = None
            try:
                obj = a.get_asset()
            except Exception:
                pass
            if obj is None:
                try:
                    obj = unreal.EditorAssetLibrary.load_asset(full)
                except Exception:
                    obj = None
            if obj is not None:
                out.append(obj)
    return out


def _list_skeletal_meshes_under(content_path):
    """Return [unreal.SkeletalMesh ...] under a /Game/... path, recursive.

    UE 5.6 removed AssetData.object_path. We use AssetData.get_asset() directly, with
    a fallback that reconstructs the path from package_name + asset_name.
    """
    ar = _asset_registry()
    # class_paths (UE5+) replaced class_names (UE4). Accept either shape.
    try:
        filt = unreal.ARFilter(
            package_paths=[content_path],
            class_paths=[unreal.TopLevelAssetPath(
                "/Script/Engine", "SkeletalMesh")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=[content_path],
            class_names=["SkeletalMesh"],
            recursive_paths=True,
        )
    assets = ar.get_assets(filt)
    out = []
    for a in assets:
        obj = None
        try:
            obj = a.get_asset()
        except Exception:
            pass
        if obj is None:
            # fallback: reconstruct path from package + asset name
            try:
                pkg = str(a.package_name)
                name = str(a.asset_name)
                obj = unreal.EditorAssetLibrary.load_asset(f"{pkg}.{name}")
            except Exception:
                obj = None
        if obj is not None:
            out.append(obj)
    return out


def _get_lod_count(mesh):
    """Return the LOD count of a SkeletalMesh. UE exposes this under several names
    across versions; try them in order."""
    for attr in ("get_num_lods", "get_lod_count", "get_lod_num"):
        fn = getattr(mesh, attr, None)
        if callable(fn):
            try:
                n = int(fn())
                if n > 0:
                    return n
            except Exception:
                pass
    try:
        lod_info = mesh.get_editor_property("lod_info")
        if lod_info is not None:
            return len(lod_info)
    except Exception:
        pass
    return 1  # at minimum LOD0 exists


def _iter_materials_on_skeletal_mesh(mesh):
    """Yield (slot_name, material_interface) for a SkeletalMesh in UE 5.6.

    SkeletalMesh.materials is Array[FSkeletalMaterial]. Each SkeletalMaterial has
    material_interface and material_slot_name. Falls back to get_editor_property
    if the attribute isn't exposed directly.
    """
    slot_mats = None
    try:
        slot_mats = mesh.get_editor_property("materials")
    except Exception:
        pass
    if slot_mats is None:
        try:
            slot_mats = mesh.materials
        except Exception:
            slot_mats = []
    for skm in slot_mats or []:
        try:
            mi = skm.get_editor_property("material_interface")
            slot = str(skm.get_editor_property("material_slot_name"))
        except Exception:
            mi = getattr(skm, "material_interface", None)
            slot = str(getattr(skm, "material_slot_name", "") or "")
        if mi is not None:
            yield slot, mi


def _read_mi_params(mi):
    """Return {'vectors': {name: [r,g,b,a]}, 'scalars': {name: float}} for a
    MaterialInstance. Used when an outfit MI has no basecolor texture but exposes a
    Color / Tint / BaseColor vector param we can feed to Blender's Principled BSDF.

    Walks editor properties defensively across UE point releases — shape of
    vector_parameter_values / scalar_parameter_values is stable but the
    parameter_info.name accessor varies.
    """
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
            out["vectors"][name] = [float(pv.r), float(pv.g), float(pv.b), float(pv.a)]
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
            out["scalars"][name] = float(s.get_editor_property("parameter_value"))
        except Exception:
            continue

    return out


def _iter_textures_in_material(material_interface):
    """Yield (param_name_or_None, unreal.Texture2D) for a material interface.

    MH's baked MaterialInstances inherit most texture params from parent materials, so
    get_texture_parameter_names returns only explicit overrides. To catch inherited
    textures we walk AssetRegistry dependencies of the material's package — every
    Texture2D the material actually references shows up there.
    """
    seen = set()

    # 1) explicit texture parameters (override slots)
    try:
        names = unreal.MaterialEditingLibrary.get_texture_parameter_names(material_interface)
    except Exception:
        names = []
    for n in names or []:
        try:
            tex = unreal.MaterialEditingLibrary.get_texture_parameter_value(material_interface, n)
        except Exception:
            tex = None
        if tex is not None and tex not in seen:
            seen.add(tex)
            yield str(n), tex

    # 2) get_used_textures (runtime-shader texture list — captures inherited)
    try:
        textures = unreal.MaterialEditingLibrary.get_used_textures(material_interface)
    except Exception:
        textures = []
    for t in textures or []:
        if t is not None and t not in seen:
            seen.add(t)
            yield None, t

    # 3) AssetRegistry dependency walk (catches inherited textures the above miss)
    try:
        ar = _asset_registry()
        pkg = material_interface.get_outermost().get_name()
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
            # fallback: older/newer overload that returns all deps
            deps = ar.get_dependencies(pkg) or []
        for dep in deps:
            dep_str = str(dep)
            if not dep_str.startswith("/Game/"):
                continue
            asset_name = dep_str.rsplit("/", 1)[-1]
            asset = None
            try:
                asset = unreal.EditorAssetLibrary.load_asset(f"{dep_str}.{asset_name}")
            except Exception:
                try:
                    asset = unreal.EditorAssetLibrary.load_asset(dep_str)
                except Exception:
                    asset = None
            if isinstance(asset, unreal.Texture2D) and asset not in seen:
                seen.add(asset)
                yield None, asset
    except Exception as e:
        _log(f"  dep walk skipped for {material_interface.get_path_name()}: {e}")


def _list_textures_under(content_paths):
    """Broad fallback: all Texture2D under one or more /Game/... paths, recursive."""
    ar = _asset_registry()
    try:
        filt = unreal.ARFilter(
            package_paths=list(content_paths),
            class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Texture2D")],
            recursive_paths=True,
        )
    except Exception:
        filt = unreal.ARFilter(
            package_paths=list(content_paths),
            class_names=["Texture2D"],
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
                pkg = str(a.package_name); name = str(a.asset_name)
                obj = unreal.EditorAssetLibrary.load_asset(f"{pkg}.{name}")
            except Exception:
                obj = None
        if isinstance(obj, unreal.Texture2D):
            out.append(obj)
    return out


# ---------------------------------------------------------------------------- #
# Export primitives                                                            #
# ---------------------------------------------------------------------------- #

def _make_fbx_options():
    """Build FbxExportOption defensively — UE point releases shuffle enum names and
    occasionally add/remove fields. Set what we can; ignore attrs that don't exist."""
    opts = unreal.FbxExportOption()

    def _set(name, value):
        try:
            opts.set_editor_property(name, value)
        except Exception as e:
            _log(f"  FbxExportOption.{name} not settable: {e}")

    _set("ascii", False)
    _set("force_front_x_axis", False)
    _set("vertex_color", True)
    _set("level_of_detail", True)           # include all LODs (stage 03 will emit one GLB per LOD)
    _set("collision", False)
    _set("export_morph_targets", True)
    _set("export_preview_mesh", False)
    _set("map_skeletal_motion_to_root", False)
    _set("export_local_time", True)
    # NOTE: bake_material_inputs enum name varies across UE versions; default is
    # "no bake" which is what we want — we export textures ourselves.
    return opts


def _export_asset(asset, filepath, fbx_options=None):
    task = unreal.AssetExportTask()
    task.object = asset
    task.filename = filepath
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    task.use_file_archive = False
    task.write_empty_files = False
    if fbx_options is not None:
        task.options = fbx_options
    ok = unreal.Exporter.run_asset_export_task(task)
    if not ok:
        raise RuntimeError(f"export failed for {asset.get_path_name()} -> {filepath}")


def _export_texture(tex, filepath):
    """Export a Texture2D as TGA."""
    _export_asset(tex, filepath, fbx_options=None)


# ---------------------------------------------------------------------------- #
# Main                                                                         #
# ---------------------------------------------------------------------------- #

def _character_paths(workspace, char_id, ue_version):
    char_root = os.path.join(workspace, "characters", char_id)
    out_root  = os.path.join(char_root, "01-fbx")
    shared    = os.path.join(workspace, "characters", "_shared", ue_version)
    return {
        "char_root":      char_root,
        "manifest":       os.path.join(char_root, "manifest.json"),
        "out_root":       out_root,
        "meshes_dir":     _ensure_dir(os.path.join(out_root, "meshes")),
        "textures_dir":   _ensure_dir(os.path.join(out_root, "textures")),
        "mh_manifest":    os.path.join(out_root, "mh_manifest.json"),
        "shared_skel":    _ensure_dir(os.path.join(shared, "skeleton")),
    }


def _load_char_manifest(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(char=None, workspace=None):
    """Entry point. When run headless, args come from sys.argv."""
    if char is None or workspace is None:
        args = _parse_args()
        char = args.char
        workspace = args.workspace

    workspace = os.path.abspath(workspace)
    _log(f"char={char}  workspace={workspace}")

    # --- read char manifest to learn the /Game/MetaHumans/<Name> path ------- #
    char_manifest_path = os.path.join(workspace, "characters", char, "manifest.json")
    char_manifest = _load_char_manifest(char_manifest_path)
    mh_folder    = char_manifest["mh_folder"]            # e.g. "/Game/MetaHumans/Ada"
    ue_version   = char_manifest["ue_version"]

    paths = _character_paths(workspace, char, ue_version)

    # --- collect skeletal meshes ------------------------------------------- #
    # Primary: follow the character BP's dependency graph. MH body/face live under
    # /Game/MetaHumans/<Name>/, but **clothing** (shirt / slacks / shoes, etc.) lives
    # in shared archetype folders like /Game/MetaHumans/Common/Female/.../Tops/Shirt/.
    # A folder scan of the char root would miss every clothing asset.
    # Fallback: a recursive folder scan under /Game/MetaHumans/<Name>/ (covers naked
    # characters with no BP deps, and legacy shapes we haven't seen yet).
    bp_meshes = _list_skeletal_meshes_from_bp(mh_folder)
    folder_meshes = _list_skeletal_meshes_under(mh_folder)
    # dedupe by asset path, BP order first so clothing ordering is stable
    meshes = []
    seen_paths = set()
    for m in list(bp_meshes) + list(folder_meshes):
        p = m.get_path_name()
        if p in seen_paths:
            continue
        seen_paths.add(p)
        meshes.append(m)
    if not meshes:
        raise RuntimeError(
            f"No SkeletalMesh assets discovered for {mh_folder} (neither via BP deps "
            f"nor folder scan). Has the character been assembled? See stage CONTEXT.md "
            f"precondition."
        )
    _log(f"found {len(meshes)} skeletal meshes "
         f"({len(bp_meshes)} via BP dep walk, {len(folder_meshes)} via folder scan)")
    for m in meshes:
        _log(f"  mesh: {m.get_path_name()}")

    fbx_opts = _make_fbx_options()

    mesh_records = []
    texture_records = []
    seen_textures = set()
    warnings = []

    # --- per-mesh export ---------------------------------------------------- #
    for mesh in meshes:
        asset_path = mesh.get_path_name()
        # derive a friendly component name from the asset name
        comp = mesh.get_name().lower()
        fbx_rel = f"meshes/{comp}.fbx"
        fbx_abs = os.path.join(paths["out_root"], fbx_rel)

        try:
            _export_asset(mesh, fbx_abs, fbx_opts)
        except Exception as e:
            warnings.append(f"mesh export failed: {asset_path}: {e}")
            _log(f"ERROR exporting mesh {asset_path}: {e}")
            continue

        # material list + texture walk
        # seen_textures prevents re-exporting the same file, but we still
        # record the (material, texture) linkage every time so a texture shared
        # across several materials (e.g. T_Iris_A_* on both eye materials)
        # resolves correctly in stage 02.
        mats = []
        for slot, mi in _iter_materials_on_skeletal_mesh(mesh):
            mi_path = mi.get_path_name()
            mats.append({
                "slot": slot,
                "material": mi_path,
                "params": _read_mi_params(mi),
            })
            for param_name, tex in _iter_textures_in_material(mi):
                tp = tex.get_path_name()
                tex_rel = f"textures/{tex.get_name()}.tga"
                tex_abs = os.path.join(paths["out_root"], tex_rel)
                if tp not in seen_textures:
                    seen_textures.add(tp)
                    try:
                        _export_texture(tex, tex_abs)
                    except Exception as e:
                        warnings.append(f"texture export failed: {tp}: {e}")
                        _log(f"ERROR exporting texture {tp}: {e}")
                        continue
                texture_records.append({
                    "asset_path": tp,
                    "file_path": tex_rel,
                    "material": mi_path,
                    "param": param_name,
                })

        lod_count = _get_lod_count(mesh)
        role = _infer_mesh_role(asset_path)
        mesh_records.append({
            "component": comp,
            "role": role,
            "asset_path": asset_path,
            "fbx_path": fbx_rel,
            "lod_count": lod_count,
            "materials": mats,
        })
        _log(f"  {comp} [{role}]: {lod_count} LOD(s), {len(mats)} material slot(s)")

    # --- hair cards (StaticMesh, LOD0 only, from BP deps) ------------------- #
    # MH hair is a Groom asset (Alembic) which has no free FBX exporter, but each
    # hair groom also ships as a StaticMesh "cards" fallback for lower LODs. We
    # export LOD0 of each hair-cards mesh so the character at least has something
    # hair-shaped. This is a fallback; Groom is still recorded as unsupported_v1.
    hair_card_meshes = _list_hair_card_static_meshes_from_bp(mh_folder)
    _log(f"hair card StaticMeshes found via BP deps: {len(hair_card_meshes)}")
    for mesh in hair_card_meshes:
        asset_path = mesh.get_path_name()
        comp = mesh.get_name().lower()
        fbx_rel = f"meshes/{comp}.fbx"
        fbx_abs = os.path.join(paths["out_root"], fbx_rel)
        try:
            _export_asset(mesh, fbx_abs, fbx_opts)
        except Exception as e:
            warnings.append(f"hair-card export failed: {asset_path}: {e}")
            _log(f"ERROR exporting hair card {asset_path}: {e}")
            continue

        # Hair cards reference placeholder materials (WorldGridMaterial) on the
        # StaticMesh; the real hair-cards MI lives under /Game/MetaHumans/<Name>/
        # Materials/. Look it up and use it as the effective material for every
        # slot — stage 02 will pull textures and color from this MI.
        override_mi = _lookup_hair_card_material_for(mh_folder, comp)
        if override_mi is not None:
            _log(f"    hair material override: {override_mi.get_path_name()}")

        # Hair-card atlas textures live on the sibling GroomAsset, not on the MI.
        # Export them now and link each to the override MI so stage 02 can wire
        # coverage → alpha when building the hair material.
        groom_tex_records = []
        groom = _lookup_groom_for_hair_card(mesh)
        if groom is not None and override_mi is not None:
            override_mi_path = override_mi.get_path_name()
            for tex in _groom_texture2d_deps(groom):
                tp = tex.get_path_name()
                tex_rel = f"textures/{tex.get_name()}.tga"
                tex_abs = os.path.join(paths["out_root"], tex_rel)
                if tp not in seen_textures:
                    seen_textures.add(tp)
                    try:
                        _export_texture(tex, tex_abs)
                    except Exception as e:
                        warnings.append(f"groom texture export failed: {tp}: {e}")
                        _log(f"ERROR exporting groom texture {tp}: {e}")
                        continue
                groom_tex_records.append({
                    "asset_path": tp,
                    "file_path": tex_rel,
                    "material": override_mi_path,
                    "param": None,
                    "source": "groom_atlas",
                })
            _log(f"    groom atlas textures for {groom.get_name()}: {len(groom_tex_records)}")
        texture_records.extend(groom_tex_records)

        mats = []
        for slot, mi in _iter_materials_on_static_mesh(mesh):
            effective_mi = override_mi if override_mi is not None else mi
            mi_path = effective_mi.get_path_name()
            mats.append({
                "slot": slot,
                "material": mi_path,
                "params": _read_mi_params(effective_mi),
            })
            for param_name, tex in _iter_textures_in_material(effective_mi):
                tp = tex.get_path_name()
                tex_rel = f"textures/{tex.get_name()}.tga"
                tex_abs = os.path.join(paths["out_root"], tex_rel)
                if tp not in seen_textures:
                    seen_textures.add(tp)
                    try:
                        _export_texture(tex, tex_abs)
                    except Exception as e:
                        warnings.append(f"texture export failed: {tp}: {e}")
                        _log(f"ERROR exporting texture {tp}: {e}")
                        continue
                texture_records.append({
                    "asset_path": tp,
                    "file_path": tex_rel,
                    "material": mi_path,
                    "param": param_name,
                })

        mesh_records.append({
            "component": comp,
            "role": "hair",
            "mesh_kind": "static",    # non-skinned; stage 02 parents to head bone
            "asset_path": asset_path,
            "fbx_path": fbx_rel,
            "lod_count": 1,
            "materials": mats,
        })
        _log(f"  {comp} [hair/static]: {len(mats)} material slot(s)")

    # --- broad Texture2D sweep under every folder the meshes live in ------- #
    # Safety net: material introspection misses inherited textures. We sweep the
    # char folder AND every parent folder of an exported mesh (so shared clothing
    # textures under /Game/MetaHumans/Common/Female/.../Tops/Shirt/ get picked up).
    sweep_paths = {mh_folder}
    for m in list(meshes) + list(hair_card_meshes):
        # take the directory of the mesh's package path: "/Game/.../Shirt/xyz" -> "/Game/.../Shirt"
        pkg_dir = str(m.get_path_name()).rsplit(".", 1)[0].rsplit("/", 1)[0]
        if pkg_dir.startswith("/Game/"):
            sweep_paths.add(pkg_dir)
    sweep_paths = sorted(sweep_paths)
    _log(f"texture sweep paths: {sweep_paths}")
    for tex in _list_textures_under(sweep_paths):
        tp = tex.get_path_name()
        if tp in seen_textures:
            continue
        seen_textures.add(tp)
        tex_rel = f"textures/{tex.get_name()}.tga"
        tex_abs = os.path.join(paths["out_root"], tex_rel)
        try:
            _export_texture(tex, tex_abs)
            texture_records.append({
                "asset_path": tp,
                "file_path": tex_rel,
                "material": None,     # discovered by sweep, not material walk
                "param": None,
            })
        except Exception as e:
            warnings.append(f"texture sweep export failed: {tp}: {e}")
            _log(f"ERROR sweep-exporting texture {tp}: {e}")
    _log(f"  total textures exported: {len(texture_records)}")

    # --- shared skeleton: NOT a separate export ----------------------------- #
    # The `Skeleton` asset class has no FBX exporter (confirmed in UE 5.6). The skeleton
    # hierarchy is embedded in every SkeletalMesh FBX we exported above, so Blender can
    # pick it up from body.fbx. We just record the reference here for traceability.
    skel_asset_path = "/Game/MetaHumans/Common/Female/Medium/NormalWeight/Body/metahuman_base_skel"
    skel_rec = {
        "asset_path": skel_asset_path,
        "fbx_path": None,
        "note": "Skeleton has no standalone FBX exporter in UE 5.6. "
                "Skeleton hierarchy is embedded in each SkeletalMesh FBX — stage 02 "
                "reads it from the body mesh.",
    }

    # --- write mh_manifest.json -------------------------------------------- #
    mh_manifest = {
        "character_id": char,
        "ue_version":   ue_version,
        "exported_at":  _iso_now(),
        "archetype":    char_manifest.get("archetype"),
        "meshes":       mesh_records,
        "textures":     texture_records,
        "skeleton":     skel_rec,
        "groom":        "unsupported_v1",
        "warnings":     warnings,
    }
    with open(paths["mh_manifest"], "w", encoding="utf-8") as f:
        json.dump(mh_manifest, f, indent=2)
    _log(f"wrote {paths['mh_manifest']}  "
         f"({len(mesh_records)} meshes, {len(texture_records)} textures, "
         f"{len(warnings)} warnings)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log("FATAL: " + str(e))
        _log(traceback.format_exc())
        # exit non-zero so the launcher fails loudly
        sys.exit(1)
