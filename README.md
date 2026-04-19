# MetaHuman → GLB

<p align="center">
  <a href="https://smorchj.github.io/metahuman-to-glb/">
    <img src="assets/hero.png" alt="Ada — MetaHuman rendered in the three.js gallery" width="80%" />
  </a>
</p>

<p align="center">
  <a href="https://smorchj.github.io/metahuman-to-glb/">
    <img src="https://img.shields.io/badge/LIVE%20DEMO-metahuman--to--glb-06B6D4?style=for-the-badge&labelColor=0a0420&logo=github&logoColor=white" alt="live demo" />
  </a>
</p>

Deterministic four-stage pipeline that turns an Unreal MetaHuman into a
web-ready, Draco-compressed GLB and publishes it as a browsable three.js
viewer on GitHub Pages.

- **Stage 01** — Export FBX + textures from UE via a commandlet
- **Stage 02** — Assemble meshes + rebuild MetaHuman materials in Blender
- **Stage 03** — Export GLB (Draco, texture cap, tri budget) + sidecar material mapping
- **Stage 04** — Build a static three.js gallery under `docs/` for GitHub Pages

Each stage is a pure script (Python / PowerShell). The LLM is glue; it reads
the stage's contract, runs the launcher, and updates a per-character manifest.

## Live demo

**https://smorchj.github.io/metahuman-to-glb/** — gallery built from
`docs/` on every push to `main`.

## Status

This is a **fun weekend project**. Currently running against **Ada** and
**Taro** from the MetaHuman demo set — both are in the live gallery. The
fundamentals are in place, but a lot of render quality and automation work
is still on the table (see [Known gaps](#known-gaps) below, and the open
issues). Tested running in Safari on iPhone X.

The pipeline is designed to run with **Claude Haiku** as the per-stage
executor (Opus designs the contracts, Haiku runs them). Interesting if
someone wants to try adapting it to a small local model — the stage
boundaries keep context small enough that a weak model should be able to
execute each step.

The viewer has a live look-dev panel for hair tuning — append `?tune=1`
to any character page (e.g. `.../characters/ada/?tune=1`) to get sliders
for roughness floor, root darkening, seed variance, anisotropy strength
and rotation. Dialled-in values paste straight into a per-character
override table in the viewer.

## Known gaps

- **Eye shader is bare-minimum.** Iris / limbus / pupil / sclera-vein math
  works ish, but refraction, caustics, and sub-surface on the caruncle are
  faked or hidden. Iris and pupil size seem slightly off
  ([#5](https://github.com/smorchj/metahuman-to-glb/issues/5)).
- **Eye occlusion has no alpha mask.** The eyeshell submesh renders as a
  flat 40% dark layer across the *entire* eye — no texture fades it to
  zero in the center. Consequences: slight whole-eye darkening when open,
  and a visible horizontal streak mid-blink where the upper and lower
  lid halves of the skirt overlap. Needs MH's eyeshell occlusion mask
  exported from UE and wired as `alphaMap`
  ([#19](https://github.com/smorchj/metahuman-to-glb/issues/19)).
- **No scalp darkening under hair cards.** Hair cards sit on bare head
  skin — MH bakes a scalp/root gradient into
  `FaceBakedGroomRootTipGradientRegionMasks` (already in `01-fbx/`) but
  stage 02 doesn't sample it. Reads as "wig" up close
  ([#15](https://github.com/smorchj/metahuman-to-glb/issues/15)).
- **Hair color curve is too bright.** Two-pass hair, anisotropic spec via
  `_CardsAtlas_Tangent`, root darkening and per-strand seed variance are
  all wired now, but the `hairMelanin` → RGB curve in the MI-synth
  basecolor is lifted — Taro's hair reads too blonde vs the UE reference.
  Tip translucency also still missing
  ([#13](https://github.com/smorchj/metahuman-to-glb/issues/13)).
- **Asymmetric brow expressions are muted.** ARKit 52's `browInnerUp` is
  a single bilateral key (not split) and MediaPipe tends to regress L/R
  toward symmetry under low signal, so lift-one-brow / angry-knot
  expressions collapse. Three linked issues:
  split `browInnerUp` into L/R custom keys
  ([#17](https://github.com/smorchj/metahuman-to-glb/issues/17)),
  add a `browInward` L/R pair for the nose-scrunch pinch
  ([#16](https://github.com/smorchj/metahuman-to-glb/issues/16)),
  decouple L/R signals in the MediaPipe driver
  ([#18](https://github.com/smorchj/metahuman-to-glb/issues/18)).
- **Clothing picks the wrong base colour.** Mask-blended
  `diffuse_color_1/2` aren't wired through correctly — garments render
  flat instead of showing the secondary tone in masked regions
  ([#12](https://github.com/smorchj/metahuman-to-glb/issues/12)).
- **Some MH material maps are skipped** because the Unreal node graphs
  are too complex to round-trip through Blender's Principled BSDF + glTF.
  A generic system for reconstructing UE material graphs automatically
  (instead of per-MI hard-coding) is a prerequisite for full automation
  ([#7](https://github.com/smorchj/metahuman-to-glb/issues/7)).
- **Brow colour is hardcoded.** The MI synth produces auburn brows
  (driven by `hairRedness`) so it's pinned to dark brown. Should be
  derived properly from the scalp hair color
  ([#8](https://github.com/smorchj/metahuman-to-glb/issues/8)).
- **GLB payloads are over GitHub's 50 MB recommendation.** Ada at 51 MB,
  Taro at 76 MB after Draco mesh compression + 1024/256 texture caps.
  Switching sidecar + embedded textures to KTX2/Basis Universal would
  drop both well under 50 MB and speed up client decode
  ([#20](https://github.com/smorchj/metahuman-to-glb/issues/20)).

## Contributing

Open source under the **MIT license**. PRs very welcome — especially on
the gaps above. File an issue first if it's a bigger architectural change
so we don't duplicate work.

## Layout

```
_config/pipeline.yaml        # paths, UE version, active character, GLB caps
CONTEXT.md                   # Layer 1 — task routing for the orchestrator
CLAUDE.md                    # Layer 0 — agent orientation
stages/
  01-metahuman-engine-export/<ue-ver>/   # version-pinned UE exporter
  02-blender-setup/
  03-export-to-glb/
  04-webview-build/
characters/<id>/
  manifest.json              # per-character, per-stage status
  source/README.md           # pointer to the UE project + MH folder
  01-fbx/ 02-blend/ 03-glb/   # stage outputs (gitignored — rebuild locally)
docs/                        # stage 04 output, served by GitHub Pages
```

## Running the pipeline

You need:

- Unreal Engine **5.6** with the MetaHumans sample project
- Blender **5.0** (for stages 02 + 03)
- Python 3.10+ (for stage 04 and orchestration)

Edit `_config/pipeline.yaml` with your local paths and the character id
you want to run (`active_character`). Copy `characters/_template/` to
`characters/<id>/` for a new MetaHuman.

Run stages one at a time (or let the orchestrator walk the manifest):

```bash
# Stage 01: UE commandlet writes FBX + textures
# Stage 02: blender -b -P stages/02-blender-setup/tools/import_fbx.py -- --char <id> --workspace .
# Stage 03: blender -b -P stages/03-export-to-glb/tools/export_glb.py  -- --char <id> --workspace .
# Stage 04: python stages/04-webview-build/tools/build_site.py --char <id> --workspace .
```

See each stage's `CONTEXT.md` for its exact contract (Inputs → Process →
Outputs).

## License

MIT — see [LICENSE](LICENSE).
