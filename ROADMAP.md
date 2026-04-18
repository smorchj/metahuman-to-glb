# Roadmap — deferred work

Non-blocking follow-ups. Not scoped into v1.

## LODs packed into a single GLB (`MSFT_lod`)

**Current v1 behavior**: stage 01 embeds all LODs in each mesh FBX. Stage 03 will emit
one GLB per LOD level (`ada_lod0.glb`, `ada_lod1.glb`, …). The consumer app picks which
file to load.

**Target**: a single `ada.glb` using the [`MSFT_lod`](https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Vendor/MSFT_lod)
extension, so a three.js loader plugin can swap LODs at runtime from one asset.

**Why deferred**: Blender's built-in glTF exporter does not write `MSFT_lod`. Requires
either (a) a custom glTF exporter addon, or (b) post-processing the exported GLB with a
Python tool (e.g. `pygltflib`) to merge per-LOD GLBs into one and insert the extension
node. Both are non-trivial and add a maintenance surface that v1 doesn't need.

**How to pick this up**:
- Keep stage 03's per-LOD GLBs as the source artifacts.
- Add `stages/04-pack-lod-glb/` that consumes `03-glb/ada_lod*.glb` and emits
  `ada.glb` with the `MSFT_lod` extension + `screenCoverage` thresholds.
- Add a validator that round-trips the packed GLB through a three.js-compatible loader
  and confirms the LOD chain is walkable.

## Groom hair

Deferred for v1. No free FBX exporter for Groom; Alembic export requires the paid Fab
plugin. Current manifest records `groom: "unsupported_v1"`. Revisit once we have a
licensing path or a MH version that ships a free exporter.

## Archetype dedupe for batch runs

The body mesh (`f_med_nrw_body`) is shared across every female/medium/normal-weight MH.
Exporting it per-character is wasteful at batch scale. Add a batch orchestrator that
exports the body once per archetype into `characters/_shared/<ue_ver>/archetypes/<archetype>/`
and links each character manifest to the shared file.
