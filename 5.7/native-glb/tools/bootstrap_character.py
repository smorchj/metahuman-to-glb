"""bootstrap_character.py — initialize a per-character pipeline folder.

Operator-facing entry point. Given a UE asset path (or just a character
id), creates `characters/<id>/` from `_template/`, fills in the manifest's
character_id + mh_folder + output_name fields, and resets every stage to
"pending".

Designed to be Haiku-runnable: pure deterministic file operations + a
JSON edit. No model reasoning required.

Usage:
    python bootstrap_character.py --asset /Game/Ada/MHC_Ada
    python bootstrap_character.py --asset /Game/Foo
    python bootstrap_character.py --id ada              # uses default /Game/Ada

The pipeline-root --workspace defaults to the directory containing this
script's parent (so calling from anywhere resolves correctly).

Exit codes:
    0  character bootstrapped
    1  character already exists (won't overwrite)
    2  template missing (pipeline misinstall)
    3  bad arguments
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys


def _log(m): print(f"[bootstrap] {m}", flush=True)


def _strip_shell_prefix(asset_path: str) -> str:
    """Git Bash on Windows mangles paths starting with `/Game/...` by
    prepending the MSYS root (e.g. `C:/Program Files/Git/Game/Ada`).
    If the path contains `/Game/` anywhere, take from there."""
    s = asset_path.strip()
    idx = s.find("/Game/")
    if idx > 0:
        return s[idx:]
    return s


def _derive_id_from_asset(asset_path: str) -> str:
    """`/Game/Ada/MHC_Ada` -> "ada".  `/Game/Foo` -> "foo".
    Strip leading slashes, take the segment that looks like the
    character folder name (skip MHC_ prefixed assets).
    """
    asset_path = _strip_shell_prefix(asset_path)
    parts = [p for p in asset_path.strip("/").split("/") if p]
    if not parts:
        raise ValueError(f"empty asset path: {asset_path!r}")
    # If last part starts with MHC_ (the asset itself), use the parent.
    last = parts[-1]
    # Strip any `.AssetName` suffix (UE asset paths are sometimes
    # `/Game/Ada/MHC_Ada.MHC_Ada`).
    last = last.split(".")[0]
    if last.lower().startswith("mhc_") and len(parts) >= 2:
        return parts[-2].lower()
    return last.lower()


def _derive_mh_folder_from_asset(asset_path: str) -> str:
    """`/Game/Ada/MHC_Ada` -> `/Game/Ada`.  `/Game/Foo` -> `/Game/Foo`.
    Strip the asset filename if present; keep the folder.
    """
    s = _strip_shell_prefix(asset_path)
    if not s.startswith("/"):
        s = "/" + s
    parts = s.strip("/").split("/")
    last = parts[-1].split(".")[0]
    if last.lower().startswith("mhc_") and len(parts) >= 2:
        return "/" + "/".join(parts[:-1])
    if "." in parts[-1]:
        # foo.bar -> drop bar (asset.member form)
        return "/" + "/".join(parts[:-1])
    return s


def _derive_output_name(mh_folder: str) -> str:
    """`/Game/Ada` -> "Ada".  Used by stage 01 to find the SkeletalMesh
    container in UE."""
    parts = mh_folder.strip("/").split("/")
    return parts[-1]


def _parse_args():
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--asset", help="UE asset path, e.g. /Game/Ada/MHC_Ada or /Game/Ada")
    src.add_argument("--id", help="Character id (will default mh_folder to /Game/<Id> capitalised)")
    p.add_argument("--workspace", help="Pipeline root (defaults to the directory containing this script's parent)")
    p.add_argument("--force", action="store_true", help="Overwrite existing character folder")
    return p.parse_args()


def main():
    args = _parse_args()
    workspace = (
        args.workspace
        or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    )
    workspace = os.path.abspath(workspace)
    template_dir = os.path.join(workspace, "characters", "_template")

    if args.id:
        char_id = args.id.lower()
        mh_folder = f"/Game/{args.id[0].upper() + args.id[1:]}"
        output_name = args.id[0].upper() + args.id[1:]
    else:
        char_id = _derive_id_from_asset(args.asset)
        mh_folder = _derive_mh_folder_from_asset(args.asset)
        output_name = _derive_output_name(mh_folder)

    char_dir = os.path.join(workspace, "characters", char_id)
    _log(f"workspace: {workspace}")
    _log(f"char_id: {char_id}")
    _log(f"mh_folder: {mh_folder}")
    _log(f"output_name: {output_name}")
    _log(f"char_dir: {char_dir}")

    if not os.path.isdir(template_dir):
        _log(f"ERROR: template missing at {template_dir}")
        return 2

    if os.path.exists(char_dir):
        if not args.force:
            _log(f"character folder already exists; use --force to overwrite. ({char_dir})")
            return 1
        _log(f"--force: removing existing {char_dir}")
        shutil.rmtree(char_dir)

    shutil.copytree(template_dir, char_dir)

    # Patch manifest
    manifest_path = os.path.join(char_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["character_id"] = char_id
    manifest["mh_folder"] = mh_folder
    manifest["output_name"] = output_name
    manifest["created_at"] = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    # Reset every stage to a clean pending state.
    for stage_key, stage in manifest.get("stages", {}).items():
        stage["status"] = "pending"
        stage["started_at"] = None
        stage["completed_at"] = None
        stage["errors"] = []
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Drop a tiny source/README.md so future runs have provenance.
    source_dir = os.path.join(char_dir, "source")
    os.makedirs(source_dir, exist_ok=True)
    readme = os.path.join(source_dir, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(f"# {char_id} — source (5.7 native-glb)\n\n")
        if args.asset:
            f.write(f"Original asset path: `{args.asset}`\n\n")
        f.write(f"UE folder: `{mh_folder}`\n")
        f.write(f"UE asset name: `{output_name}`\n")
        f.write(
            "\nStage 00 will assemble this MetaHumanCharacter into "
            f"`/Game/{output_name}/` if it isn't already.\n"
        )

    _log(f"OK: created {char_dir}")
    _log("manifest stages: all pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
