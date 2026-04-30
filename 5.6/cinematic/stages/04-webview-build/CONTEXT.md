# Stage 04 — Webview Build (GitHub Pages)

Pure-Python stage (no Blender, no UE). Consumes stage 03's GLBs and
builds a static site under `docs/` (GitHub Pages' default publish root)
that renders each character in a browser using Google's `<model-viewer>`
web component.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `characters_dir` | Where characters live |
| Character manifest | `characters/<id>/manifest.json` | `character_id`, `stages.03_glb_export.status` | Which characters to publish |
| Stage 03 GLB | `characters/<id>/03-glb/<id>.glb` | binary | The asset to host |
| Stage 03 manifest | `characters/<id>/03-glb/glb_manifest.json` | `tri_count`, `file_size_bytes` | Metadata shown on gallery |
| Page templates | `stages/04-webview-build/templates/` | all | HTML/CSS scaffolding |

## Process

1. Invoke `tools/run_site.ps1 -Char <id>`. It runs (via Blender's bundled Python):
   `python build_site.py --char <id> --workspace <abs>`
2. `build_site.py`:
   - Reads `<id>`'s stage 03 manifest + GLB.
   - Copies GLB to `docs/characters/<id>/<id>.glb`.
   - Renders `docs/characters/<id>/index.html` from `templates/viewer.html`
     (template vars: `{{character_id}}`, `{{glb_file}}`, `{{tri_count}}`, `{{file_size_mb}}`).
   - Scans **all** `characters/*/manifest.json` for characters with
     `stages.03_glb_export.status == "done"` and regenerates
     `docs/index.html` as a gallery index.
   - Copies `templates/style.css` → `docs/assets/style.css`.
   - Writes/overwrites `docs/.nojekyll` (prevents GitHub's Jekyll from
     dropping paths starting with `_`).
3. Update `characters/<id>/manifest.json`:
   - `stages.04_webview_build.status = "done"` on success.
   - `stages.04_webview_build.completed_at = <ISO timestamp>`.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Per-character viewer | `docs/characters/<id>/index.html` + `<id>.glb` | One folder per character |
| Gallery index | `docs/index.html` | Links to every published character |
| Site stylesheet | `docs/assets/style.css` | Shared across all pages |
| Jekyll opt-out | `docs/.nojekyll` | Ensures underscore-prefixed paths work |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.04_webview_build` fields |

## Publishing to GitHub Pages

In the repo's GitHub settings:

1. **Settings → Pages → Source**: "Deploy from a branch"
2. **Branch**: `main`, **Folder**: `/docs`
3. Push. Site lives at `https://<user>.github.io/<repo>/`.

No build action required — `<model-viewer>` loads from unpkg CDN and the
GLB files are static assets.

## Idempotency

Re-running is safe. Every output file is unconditionally overwritten.
The gallery index rebuilds from scratch every run by scanning character
manifests, so removing a character from `characters/` and re-running
this stage will remove it from the gallery on the next rebuild
(but not from `docs/characters/<id>/` — delete that folder manually).

## Known current behavior (v1)

- **No build step, no bundler, no npm**: single static HTML + one `<script>`
  tag loading `<model-viewer>` from unpkg.
- **Per-character call only rebuilds index**, not other characters' viewers.
  This is fine — their `index.html` files from prior runs still work.
- **Lighting**: relies on `<model-viewer>`'s default neutral environment
  map. No per-character lighting tuning. Looks fine for presentation; if
  skin reads flat, swap to a custom `environment-image` in the template.
- **No animations**: GLBs from stage 03 ship in rest pose. If/when stage 02
  bakes animation, `<model-viewer>` will pick them up via the `animation-name`
  attribute — no stage 04 change needed.

## Failure modes (known)

- `<id>.glb` missing → stage 03 hasn't run. Ask operator to run stage 03 first.
- `docs/` is git-ignored → add `docs/` to the repo (remove from `.gitignore`)
  so GitHub Pages can serve it.
