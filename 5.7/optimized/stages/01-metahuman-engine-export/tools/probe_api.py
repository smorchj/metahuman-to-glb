"""
UE 5.7 API surface probe.

Runs inside UnrealEditor-Cmd.exe and writes a plain-text report to
stdout (captured by run_probe.ps1) plus to
`stages/01-metahuman-engine-export/5.7/references/api_probe.txt`.

Doesn't need an actual MetaHuman asset in the project — this is a
reconnaissance of what `unreal.*` exposes after the MetaHuman plugins
are loaded, so we know exactly which classes + methods to drive in
the real export rewrite.

Usage:
    UnrealEditor-Cmd.exe <uproject> -run=pythonscript \
        -script="<abs>/probe_api.py"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import unreal

SECTIONS: list[str] = []


def banner(title: str) -> None:
    bar = "=" * 60
    SECTIONS.append(f"\n{bar}\n{title}\n{bar}")


def line(s: str = "") -> None:
    SECTIONS.append(s)


def try_attr(obj, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except Exception:
        return False


def class_exists(name: str) -> bool:
    return try_attr(unreal, name)


def list_methods(cls) -> list[str]:
    out: list[str] = []
    for attr in dir(cls):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(cls, attr)
        except Exception:
            continue
        kind = type(value).__name__
        out.append(f"  .{attr}  ({kind})")
    return out


def probe_classes() -> None:
    banner("1. MetaHuman UCLASS presence")
    targets = [
        "MetaHuman",
        "MetaHumanCharacter",
        "MetaHumanCharacterEditorSubsystem",
        "MetaHumanCharacterFactoryNew",
        "MetaHumanIdentity",
        "MetaHumanIdentityFactoryNew",
        "MetaHumanIdentityFace",
        "MetaHumanIdentityBody",
        "MetaHumanIdentityPose",
        "IdentityPoseType",
        "IdentityErrorCode",
        "PromotedFrameUtils",
        "DNADataLayer",
    ]
    for name in targets:
        line(f"  unreal.{name}: {class_exists(name)}")


def probe_subsystem() -> None:
    banner("2. MetaHumanCharacterEditorSubsystem methods")
    if not class_exists("MetaHumanCharacterEditorSubsystem"):
        line("  (not available)")
        return
    cls = unreal.MetaHumanCharacterEditorSubsystem
    for s in list_methods(cls):
        line(s)


def probe_identity() -> None:
    banner("3. MetaHumanIdentity methods")
    if not class_exists("MetaHumanIdentity"):
        line("  (not available)")
        return
    for s in list_methods(unreal.MetaHumanIdentity):
        line(s)


def probe_character() -> None:
    banner("4. MetaHumanCharacter methods")
    if not class_exists("MetaHumanCharacter"):
        line("  (not available)")
        return
    for s in list_methods(unreal.MetaHumanCharacter):
        line(s)


def _asset_path_of(data) -> str:
    """Return an /Game/... object path for an AssetData in a 5.6/5.7-safe way."""
    # 5.7: .asset_class_path + .package_name + .asset_name
    pkg = getattr(data, "package_name", None)
    name = getattr(data, "asset_name", None)
    if pkg and name:
        return f"{pkg}.{name}"
    # 5.6 legacy fallback
    return getattr(data, "object_path", "<unknown>")


def probe_assets() -> None:
    banner("5. Assets of relevant classes in /Game/")
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    for script_pkg, cls_name in (
        ("/Script/MetaHumanCharacter", "MetaHumanCharacter"),
        ("/Script/MetaHumanIdentity",  "MetaHumanIdentity"),
    ):
        line(f"  {cls_name}:")
        try:
            afilter = unreal.ARFilter(
                class_paths=[unreal.TopLevelAssetPath(script_pkg, cls_name)],
                recursive_paths=True,
                package_paths=["/Game"],
            )
        except Exception:
            afilter = unreal.ARFilter(
                class_names=[cls_name],
                recursive_paths=True,
                package_paths=["/Game"],
            )
        try:
            data = registry.get_assets(afilter)
            if not data:
                line("    (none)")
            for d in data:
                line(f"    - {_asset_path_of(d)}")
        except Exception as e:
            line(f"    (query error: {e})")


def probe_specific_character() -> None:
    """Drill into any MetaHumanCharacter asset we can find: list its property
    values so we can see where the member meshes actually live."""
    banner("6. Drill into the first MetaHumanCharacter asset (Kleb)")
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        afilter = unreal.ARFilter(
            class_paths=[unreal.TopLevelAssetPath("/Script/MetaHumanCharacter", "MetaHumanCharacter")],
            recursive_paths=True,
            package_paths=["/Game"],
        )
    except Exception:
        afilter = unreal.ARFilter(
            class_names=["MetaHumanCharacter"],
            recursive_paths=True,
            package_paths=["/Game"],
        )
    data_list = registry.get_assets(afilter)
    if not data_list:
        line("  (no MetaHumanCharacter asset found in /Game/)")
        return
    data = data_list[0]
    path = _asset_path_of(data)
    line(f"  asset path: {path}")
    asset = unreal.load_asset(path)
    if asset is None:
        line("  (load_asset returned None)")
        return
    line(f"  class: {asset.get_class().get_name()}")
    # Dump each getset_descriptor property we know about.
    for prop in (
        "internal_collection",
        "fixed_body_type",
        "head_model_settings",
        "skin_settings",
        "eyes_settings",
        "makeup_settings",
        "face_evaluation_settings",
        "body_textures",
        "synthesized_face_textures",
        "synthesized_face_textures_info",
        "high_res_body_textures_info",
        "has_high_resolution_textures",
        "preview_material_type",
    ):
        try:
            val = getattr(asset, prop)
        except Exception as e:
            line(f"  .{prop:30s}  ERROR: {e}")
            continue
        line(f"  .{prop:30s} = {type(val).__name__}: {_short_repr(val)}")

    # can_build_meta_human? If True, subsystem.build_meta_human(character)
    # would materialize the SkeletalMesh + textures we need to export.
    try:
        subsystem = unreal.get_editor_subsystem(unreal.MetaHumanCharacterEditorSubsystem)
        line(f"  subsystem.can_build_meta_human: {subsystem.can_build_meta_human(asset)}")
    except Exception as e:
        line(f"  can_build_meta_human error: {e}")

    # MetaHumanCollection introspection.
    try:
        coll = asset.internal_collection
        if coll is None:
            line("  internal_collection: None")
            return
        line("  internal_collection detail:")
        _dump_object(coll, indent="    ")
        inst = getattr(coll, "default_instance", None)
        if inst is not None:
            line("  default_instance detail:")
            _dump_object(inst, indent="    ")
    except Exception as e:
        line(f"  (internal_collection introspection error: {e})")


def _dump_object(obj, indent: str = "  ") -> None:
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(obj, attr)
        except Exception as e:
            line(f"{indent}.{attr:30s} ERROR: {e}")
            continue
        if callable(v):
            continue
        line(f"{indent}.{attr:30s} = {type(v).__name__}: {_short_repr(v)}")


def _short_repr(val) -> str:
    s = repr(val)
    return (s[:200] + "…") if len(s) > 200 else s


def probe_engine_version() -> None:
    banner("0. Engine version")
    try:
        line(f"  unreal.SystemLibrary.get_engine_version(): "
             f"{unreal.SystemLibrary.get_engine_version()}")
    except Exception as e:
        line(f"  get_engine_version error: {e}")
    try:
        line(f"  unreal.SystemLibrary.get_build_version(): "
             f"{unreal.SystemLibrary.get_build_version()}")
    except Exception:
        pass


def main() -> None:
    probe_engine_version()
    probe_classes()
    probe_subsystem()
    probe_identity()
    probe_character()
    probe_assets()
    probe_specific_character()

    report = "\n".join(SECTIONS) + "\n"
    print(report)

    # Write next to the script: tools/ -> 5.7/ -> references/api_probe.txt
    out_path = Path(__file__).resolve().parent.parent / "references" / "api_probe.txt"
    out_path.write_text(report, encoding="utf-8")
    unreal.log(f"[probe_api] wrote {out_path}")


if __name__ == "__main__":
    main()
