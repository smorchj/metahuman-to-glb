"""Build script — copy src/* into site/ with a cache-bust token."""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path


def _render(template: str, replacements: dict) -> str:
    out = template
    for key, val in replacements.items():
        out = out.replace("{{" + key + "}}", str(val))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="Repo root (where src/ + site/ live)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    src = root / "src"
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)

    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    vars_ = {
        "built_at": built_at,
        "cache_bust": built_at.replace(":", "").replace("-", ""),
    }

    for name in ("index.html", "game.js", "style.css"):
        srcf = src / name
        dstf = site / name
        dstf.write_text(_render(srcf.read_text(encoding="utf-8"), vars_), encoding="utf-8")
        print(f"[build] {dstf.relative_to(root)}")

    print(f"[build] published -> {site.relative_to(root)}/ (cache bust: {vars_['cache_bust']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
