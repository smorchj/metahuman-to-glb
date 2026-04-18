# MetaHuman → GLB

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

This is a **fun weekend project**. Only tested with **Ada** from the
MetaHuman demo. The fundamentals are in place, but a lot of render quality
and automation work is still on the table (see [Known gaps](#known-gaps)
below, and the open issues). Tested running in Safari on iPhone X.

The pipeline is designed to run with **Claude Haiku** as the per-stage
executor (Opus designs the contracts, Haiku runs them). Interesting if
someone wants to try adapting it to a small local model — the stage
boundaries keep context small enough that a weak model should be able to
execute each step.

## Known gaps

- **Eye shader is bare-minimum.** Iris / limbus / pupil / sclera-vein math
  works ish, but refraction, caustics, and sub-surface on the caruncle are
  faked or hidden. Iris and pupil size seems slightly off now as well. 
- **Hair shader is weak.** Currently a built-in alphaHash against the
  compact-atlas R channel, MI-synthesised base colour, right now scalp is showing clearly through,
  no anisotropy, no root darkening, no tip translucency.
- **Some MH material maps are skipped** because the Unreal node graphs
  are too complex to round-trip through Blender's Principled BSDF + glTF.
  A generic system for reconstructing UE material graphs automatically
  (instead of per-MI hard-coding) is a prerequisite for full automation.
- **Brow and lash colour are hardcoded.** The MI synth produces auburn
  brows (driven by `hairRedness`) so I hardcoded it to be dark brown.
  Brow color should be fixed properly.

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
