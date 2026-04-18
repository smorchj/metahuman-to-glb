"""Stage 04 — build GitHub Pages site from stage 03 GLBs.

Invoked as:
    python build_site.py --char <id> --workspace <abs>

No Blender dependency. Standard-library only.

Inputs:  characters/<id>/03-glb/<id>.glb + glb_manifest.json
         characters/<id>/03-glb/mh_materials.json   (optional — MH material map)
         characters/<id>/03-glb/textures/*.png      (optional — sidecar textures)
         characters/*/manifest.json (to build the gallery)
         stages/04-webview-build/templates/*
Outputs: docs/index.html
         docs/characters/<id>/index.html + <id>.glb
         docs/characters/<id>/mh_materials.json + textures/*
         docs/assets/style.css + docs/assets/viewer.js
         docs/.nojekyll
         Updates characters/<id>/manifest.json (stages.04_webview_build)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers

def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _render(template: str, vars: dict) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_mib(n: int) -> str:
    return f"{n / 1_048_576:.1f}"


def _copy_tree(src: Path, dst: Path) -> int:
    """Mirror src/ into dst/. Returns count of files copied."""
    if not src.exists():
        return 0
    n = 0
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Site builders

def _build_character_page(
    char_id: str,
    workspace: Path,
    templates: Path,
    docs: Path,
    built_at: str,
) -> dict:
    """Copy GLB + mapping + textures and render per-character viewer."""
    char_dir = workspace / "characters" / char_id
    glb_src = char_dir / "03-glb" / f"{char_id}.glb"
    glb_manifest = _load_json(char_dir / "03-glb" / "glb_manifest.json")

    out_dir = docs / "characters" / char_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # GLB
    shutil.copy2(glb_src, out_dir / f"{char_id}.glb")

    # Optional MH material mapping + sidecar textures.
    mapping_src = char_dir / "03-glb" / "mh_materials.json"
    has_mapping = mapping_src.exists()
    if has_mapping:
        shutil.copy2(mapping_src, out_dir / "mh_materials.json")
    tex_count = _copy_tree(char_dir / "03-glb" / "textures", out_dir / "textures")

    tri_count = glb_manifest.get("tri_count", 0)
    file_size_mb = _safe_mib(glb_manifest.get("file_size_bytes", 0))

    viewer_tpl = (templates / "viewer.html").read_text(encoding="utf-8")
    rendered = _render(viewer_tpl, {
        "character_id": char_id,
        "glb_file": f"{char_id}.glb",
        "tri_count": f"{tri_count:,}",
        "file_size_mb": file_size_mb,
        "built_at": built_at,
        "cache_bust": built_at.replace(":", "").replace("-", ""),
    })
    (out_dir / "index.html").write_text(rendered, encoding="utf-8")
    print(f"[stage04] {char_id}: GLB + mapping={has_mapping} + {tex_count} textures", flush=True)

    return {
        "id": char_id,
        "tri_count": tri_count,
        "file_size_mb": file_size_mb,
        "has_mapping": has_mapping,
    }


def _discover_published_characters(workspace: Path) -> list[str]:
    """Every character with stage 03 done."""
    chars_dir = workspace / "characters"
    found = []
    for p in sorted(chars_dir.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        mf = p / "manifest.json"
        if not mf.exists():
            continue
        try:
            data = _load_json(mf)
        except Exception:  # noqa: BLE001
            continue
        stage3 = data.get("stages", {}).get("03_glb_export", {})
        if stage3.get("status") == "done":
            found.append(p.name)
    return found


def _build_gallery(
    workspace: Path,
    templates: Path,
    docs: Path,
    built_at: str,
) -> int:
    published = _discover_published_characters(workspace)
    cards = []
    for cid in published:
        try:
            glb_mf = _load_json(workspace / "characters" / cid / "03-glb" / "glb_manifest.json")
        except Exception:  # noqa: BLE001
            continue
        tri = glb_mf.get("tri_count", 0)
        mib = _safe_mib(glb_mf.get("file_size_bytes", 0))
        has_map = (workspace / "characters" / cid / "03-glb" / "mh_materials.json").exists()
        # Cache-bust GLB + mapping URLs too — the browser otherwise serves a
        # stale GLB after a re-export and any shape-key / geometry changes
        # stay invisible until a hard refresh.
        cb = built_at.replace(":", "").replace("-", "")
        map_attr = f' data-map="characters/{cid}/mh_materials.json?v={cb}"' if has_map else ""
        cards.append(
            f'<a class="card" href="characters/{cid}/index.html">'
            f'<div class="card-preview" data-glb="characters/{cid}/{cid}.glb?v={cb}"{map_attr}></div>'
            f'<div class="meta">'
            f'<span class="name">{cid}</span>'
            f'<span class="stats"><span>{tri:,} tris</span>'
            f'<span>&middot;</span><span>{mib} MiB</span></span>'
            f'</div>'
            f'</a>'
        )

    index_tpl = (templates / "index.html").read_text(encoding="utf-8")
    rendered = _render(index_tpl, {
        "cards": "\n    ".join(cards) if cards else "<p>No characters published yet.</p>",
        "count": len(published),
        "built_at": built_at,
        "cache_bust": built_at.replace(":", "").replace("-", ""),
    })
    (docs / "index.html").write_text(rendered, encoding="utf-8")
    return len(published)


# ---------------------------------------------------------------------------
# Manifest I/O

def _update_char_manifest(char_dir: Path, status: str, errors: list[str]) -> None:
    path = char_dir / "manifest.json"
    data = _load_json(path)
    stage = data.setdefault("stages", {}).setdefault("04_webview_build", {})
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
    stage.setdefault("output_dir", "../../docs/characters/")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    args = p.parse_args()

    workspace = Path(args.workspace)
    char_dir = workspace / "characters" / args.char
    templates = workspace / "stages" / "04-webview-build" / "templates"
    docs = workspace / "docs"

    glb_src = char_dir / "03-glb" / f"{args.char}.glb"
    if not glb_src.exists():
        print(f"[stage04] FAILED: missing {glb_src}", flush=True)
        _update_char_manifest(char_dir, "failed", [f"missing {glb_src}"])
        return 1

    try:
        docs.mkdir(parents=True, exist_ok=True)
        assets = docs / "assets"
        assets.mkdir(exist_ok=True)
        shutil.copy2(templates / "style.css", assets / "style.css")
        shutil.copy2(templates / "viewer.js", assets / "viewer.js")
        (docs / ".nojekyll").write_text("", encoding="utf-8")

        built_at = _iso_now()
        card = _build_character_page(args.char, workspace, templates, docs, built_at)
        count = _build_gallery(workspace, templates, docs, built_at)
        print(f"[stage04] built {args.char} ({card['tri_count']:,} tris, {card['file_size_mb']} MiB)", flush=True)
        print(f"[stage04] gallery has {count} character(s)", flush=True)
        print(f"[stage04] docs/ at {docs}", flush=True)

        _update_char_manifest(char_dir, "done", [])
        print("[stage04] char manifest updated: status=done", flush=True)
        return 0

    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[stage04] FAILED: {exc}\n{traceback.format_exc()}", flush=True)
        try:
            _update_char_manifest(char_dir, "failed", [str(exc)])
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
