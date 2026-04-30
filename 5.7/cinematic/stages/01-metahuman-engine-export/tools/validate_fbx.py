"""
Stage 01 validator — standalone Python, no UE required.

Checks that mh_manifest.json matches the artifacts on disk, and sanity-checks FBX files
(header magic + non-zero size). Updates characters/<id>/manifest.json on success.

Usage:
    python validate_fbx.py --char ada --workspace "C:/Users/smorc/Metahuman to GLB"
"""

import argparse
import datetime as _dt
import json
import os
import sys

FBX_BINARY_HEADER = b"Kaydara FBX Binary"  # first bytes of binary FBX files


def _iso_now():
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def _check_fbx(path):
    if not os.path.exists(path):
        return f"missing: {path}"
    if os.path.getsize(path) == 0:
        return f"empty: {path}"
    with open(path, "rb") as f:
        head = f.read(len(FBX_BINARY_HEADER))
    if head != FBX_BINARY_HEADER:
        return f"not a binary FBX (header mismatch): {path}"
    return None


def _check_texture(path):
    if not os.path.exists(path):
        return f"missing: {path}"
    if os.path.getsize(path) == 0:
        return f"empty: {path}"
    return None


def validate(char, workspace):
    char_root    = os.path.join(workspace, "characters", char)
    out_root     = os.path.join(char_root, "01-fbx")
    mh_path      = os.path.join(out_root, "mh_manifest.json")
    char_man_path = os.path.join(char_root, "manifest.json")

    if not os.path.exists(mh_path):
        return [f"mh_manifest.json not found at {mh_path}"]

    mh = _load_json(mh_path)
    errors = []

    # meshes
    if not mh.get("meshes"):
        errors.append("no meshes recorded in mh_manifest.json")
    for m in mh.get("meshes", []):
        fp = os.path.join(out_root, m["fbx_path"])
        err = _check_fbx(fp)
        if err:
            errors.append(f"mesh {m.get('component','?')}: {err}")

    # textures
    if not mh.get("textures"):
        errors.append("no textures recorded in mh_manifest.json")
    for t in mh.get("textures", []):
        fp = os.path.join(out_root, t["file_path"])
        err = _check_texture(fp)
        if err:
            errors.append(f"texture {t.get('asset_path','?')}: {err}")

    # skeleton — may be a reference only (fbx_path=null) when embedded in a mesh FBX
    skel = mh.get("skeleton")
    if not skel:
        errors.append("no skeleton recorded")
    elif skel.get("fbx_path"):
        fp = os.path.normpath(os.path.join(out_root, skel["fbx_path"]))
        err = _check_fbx(fp)
        if err:
            errors.append(f"skeleton: {err}")
    # else: reference-only; stage 02 reads the skeleton out of a mesh FBX. OK.

    # --- update char manifest ---
    char_manifest = _load_json(char_man_path)
    stage = char_manifest["stages"]["01_fbx_export"]
    stage["started_at"] = stage.get("started_at") or _iso_now()
    if errors:
        stage["status"] = "failed"
        stage["errors"] = errors
    else:
        stage["status"] = "done"
        stage["completed_at"] = _iso_now()
        stage["errors"] = []
    _save_json(char_man_path, char_manifest)

    return errors


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    args = p.parse_args()

    errors = validate(args.char, os.path.abspath(args.workspace))
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print("  - " + e)
        sys.exit(1)
    print("VALIDATION OK")


if __name__ == "__main__":
    main()
