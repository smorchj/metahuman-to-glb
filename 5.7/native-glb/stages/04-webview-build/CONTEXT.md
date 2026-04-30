# Stage 04 — Webview Build (GitHub Pages)

Pure-Python stage (no Blender, no UE). Consumes stage 03's GLBs and
builds a static site under `docs/` (GitHub Pages' default publish root)
that renders each character in a browser using Google's `<model-viewer>`
web component.

## Scope (hard rule)

You run this stage's launcher and verify its outputs. Nothing else.

Do **not** modify pipeline code: `stages/*/tools/*.py`, `tools/*.ps1`,
`_config/`, `RUN.md`, `CLAUDE.md`. If a script throws or produces wrong
output, surface the actual error to the operator. Silently working
around it (try/except around imports, skipping failed assets, lowering
thresholds) turns a fixable bug into an invisible regression. Do not
touch other stages' contracts, code, or manifest blocks, or other
characters' artifacts.

## Inputs

| Source | File / Location | Section | Why |
|---|---|---|---|
| Workspace config | `_config/pipeline.yaml` | `characters_dir` | Where characters live |
| Character manifest | `characters/<id>/manifest.json` | `character_id` | Identify the character. DO NOT read or modify other stages' status fields. |
| Stage 03 GLB | `characters/<id>/03-glb/<id>.glb` | binary | The asset to host |
| Stage 03 manifest | `characters/<id>/03-glb/glb_manifest.json` | `tri_count`, `file_size_bytes` | Metadata shown on gallery |
| Page templates | `stages/04-webview-build/templates/` | all | HTML/CSS scaffolding |

## Preconditions (file existence only)

Verify the following files exist on disk before running. Do **not** read
prior-stage `status` fields from the manifest — files on disk are
ground truth:

- `characters/<id>/03-glb/<id>.glb`
- `characters/<id>/03-glb/glb_manifest.json`

If either is missing, abort with an actionable message ("stage 03
output missing: <path> — run stage 03 first"). Do **not** attempt to
fix or retroactively mark other stages' status.

## Process

1. Invoke `tools/run_site.ps1 -Char <id>`. It runs:
   `python build_site.py --char <id> --workspace <abs>`
2. `build_site.py` runs in two phases:
   **BUILD phase**:
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
   **VALIDATE phase** (catches CDN / importmap / Draco / shader regressions
   before they reach a user):
   - Spins up a local HTTP server on a free port in `docs/`.
   - Opens the freshly-built character page in headless Chromium via
     Playwright.
   - Waits up to 25 s for `window.__viewer` to be set (signals the GLB
     mounted and the first frame rendered).
   - Captures any console errors / unhandled page errors.
   - Takes a viewport screenshot, writes `docs/characters/<id>/preview.png`.
   - Tears down the server. Fails the stage if the viewer didn't mount,
     or if any console errors fired during the run.
3. Update `characters/<id>/manifest.json` — **only** the
   `stages.04_webview_build` block. Do not read, modify, or "fix" any
   other stage's status, timestamps, or errors. The dispatcher (Opus)
   owns those fields:
   - `stages.04_webview_build.status = "done"` on success
   - `stages.04_webview_build.started_at = <ISO timestamp at launch>`
   - `stages.04_webview_build.completed_at = <ISO timestamp at finish>`
   - `stages.04_webview_build.errors = []`
4. On failure: `stages.04_webview_build.status = "failed"`, append
   actionable message to `stages.04_webview_build.errors[]`, leave
   artifacts in place. Touch only this stage's block.

## Outputs

| Artifact | Location | Notes |
|---|---|---|
| Per-character viewer | `docs/characters/<id>/index.html` + `<id>.glb` | One folder per character |
| Gallery index | `docs/index.html` | Links to every published character |
| Site stylesheet | `docs/assets/style.css` | Shared across all pages |
| Render-validation screenshot | `docs/characters/<id>/preview.png` | Headless-Chromium snapshot of the live viewer (1280×800). Evidence the GLB actually rendered. |
| Jekyll opt-out | `docs/.nojekyll` | Ensures underscore-prefixed paths work |
| Updated char manifest | `characters/<id>/manifest.json` | `stages.04_webview_build` fields |

## Tooling requirement

The validate phase needs **Playwright + Chromium** installed on the
local machine:

```
pip install playwright
playwright install chromium
```

One-time setup (the Chromium binary is ~300 MB). If Playwright is
missing, this stage fails with an actionable error.

For CI / environments without browsers, pass `--skip-validate` to
`build_site.py` — the build phase still runs and writes the site,
but no `preview.png` is produced and the manifest will still mark
the stage `done`.

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

## Resolved issues (testing record)

- **Grey hair/beard color (2026-04-30)**: `base_color` values in
  `mh_materials.json` are sRGB (Blender color-picker space), but
  `THREE.Color.setRGB()` stores linear internally. Without conversion,
  sRGB 0.095 was treated as linear 0.095, displaying as ~34% grey
  instead of the correct near-black. Fix: append `.convertSRGBToLinear()`
  to every `setRGB(base_color)` call in `viewer.js`. Applies to hair,
  eyelashes, eye occlusion, and sclera colors. Caught on character
  "bruce" where the mutton chops beard rendered medium grey instead of
  dark brown.

- **Asymmetric facial hair / cards clipping into face (2026-04-30)**:
  Hair card meshes from UE sit flush against the face surface. After
  FBX→GLB export, some cards on one side end up slightly behind the
  face mesh (up to 0.014 units). This makes one side of the
  mustache/beard appear thinner because embedded cards are hidden by
  the opaque face. Caught on "bruce" (horseshoe mustache, 42% of
  left-side cards behind face vs 30% on right).
  **Viewer-side mitigation** (polygon offset only): `polygonOffset`
  with `factor=-4, units=-16` in `templates/viewer.js` biases hair
  fragments toward the camera in the depth buffer. Does not move
  geometry. The earlier vertex shader push (`objectNormal * 0.002`)
  was removed because it displaced all hair card verts (including
  eyebrows) outward by 2 mm, causing visible hovering.
  **Upstream fix** (2026-04-30): `_fix_hair_face_clearance()` in
  `stages/02-blender-assemble/tools/import_glb.py`. Uses
  `mathutils.bvhtree.BVHTree` to raycast each hair card vertex against
  the face surface. Any vertex closer than 0.5 mm (or behind the
  surface) is pushed outward along the face normal to 0.5 mm clearance.
  Runs after material wiring, before saving the blend. This is the
  proper geometry-level fix; the viewer-side shader hack remains as a
  fallback for edge cases the BVH pass doesn't fully resolve.

## Failure modes (known)

- `<id>.glb` missing → stage 03 hasn't run. Ask operator to run stage 03 first.
- `docs/` is git-ignored → add `docs/` to the repo (remove from `.gitignore`)
  so GitHub Pages can serve it.
- `playwright not installed` → run the one-time setup above. Bypass
  with `--skip-validate` if you genuinely can't install browsers.
- `viewer never mounted` → the page loads but `window.__viewer` never
  gets set. Usually a JS / CDN / importmap regression in
  `templates/viewer.html` or `templates/viewer.js`. The error from
  the page's red <pre> is captured into the manifest's `errors[]`.
- `console errors during mount` → some asset (Draco loader, texture,
  morph data) failed. The first 3 console errors are captured into
  `errors[]` for diagnosis.
