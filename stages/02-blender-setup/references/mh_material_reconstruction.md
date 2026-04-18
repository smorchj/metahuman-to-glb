# MetaHuman Material Reconstruction Reference (Stage 02)

Context for anyone (human or LLM) reading `tools/import_fbx.py` to understand
*why* it maps MetaHuman materials to Blender Principled BSDF the way it does.
UE's MH materials are data-driven node graphs we cannot port 1:1 to a GLB's
Principled BSDF — this file captures the translation decisions so they're
debuggable and extensible per new character.

## What stage 01 hands us

For each skinned mesh (`/Game/MetaHumans/<Name>/BP_<Name>` dependency walk):

1. **FBX** with all LODs, UVs, material slot names from UE.
2. **Textures** (`*.tga`) referenced by the MI tree of each slot's material.
   Each texture is keyed in `mh_manifest.json` by the **MI path** it was
   referenced from (NOT by which mesh uses it — an MI can be shared by many
   slots, or a texture can appear in multiple MIs).
3. **MI params** — each `materials[]` record carries the flattened final
   `{vectors, scalars}` of that slot's Material Instance. This is the only
   way to recover colors, roughness, metallic, tiling etc. for MIs that
   don't ship baked albedo textures (skin / cloth / hair).

We do NOT get:
- UE Material graph topology (the `.uasset` node network)
- IrisColorPicker palette pixel values
- Hair card geometry from groom asset
- Wrinkle blendshape color deltas

## Material families and how we reconstruct them

### Skin — head (`MI_HeadSynthesized_Baked`) and body (`MI_BodySynthesized`)

Stage 01 exports fully-baked PBR texture set:
`*BaseColor.tga`, `*Normal.tga`, `*Roughness.tga`, `*Cavity.tga`.
Maps 1:1 onto Principled BSDF. We multiply cavity into basecolor (85%) for
pore detail, enable subsurface with red-biased radius (skin SSS).
Micro normal (`T_SkinMicroNormal.tga`) is tiled 20× and mixed with base.

Wrinkle maps (`*WM1/2/3.tga`) and multi-channel skin color masks (`CM1/2/3`)
are NOT wired — they require a live facial rig to drive blend factors;
irrelevant for a static GLB.

### Face accessories — slots that ship NO textures

The face mesh has 15+ slots, most are parametric shaders in UE with zero
albedo textures: teeth, saliva, eye refractive, eye occlusion, eyelashes,
eyeEdge, cartilage, head_LODN duplicates. We classify by slot name
(`_classify_face_slot`) and emit flat-default Principled BSDFs with
hand-tuned PBR values per slot kind.

Exception: eyelashes get a coverage alpha texture from stage 01.

### Eyes (slots 3/4 = eyeLeft/eyeRight, `MI_EyeRefractive_Inst_L/R`)

UE drives iris color from an `IrisColorPicker` palette sampled at
`IrisColor1U/V` MI scalars — the palette is NOT in our export. We rebuild
the iris from `T_Iris_A_M.tga` (an atlas, not albedo) whose channel
semantics we measured empirically:

- **R**: fibril detail (continuous 0.25..0.75) → multiplier on iris color
- **G**: hard inverted iris mask (unused — we use radial UV instead)
- **B**: soft radial gradient from iris outward → limbus darkening factor
- **A**: sharp iris mask (unused — radial UV is more controllable)

The shader is built in UV space around a per-eye **pole UV**: the point on
the UV map corresponding to the forward-facing pole of the eyeball (iris
center in world space). Each eye has its own pole UV (Ada: L=(0.477, 0.512),
R=(0.525, 0.507)); detected at import time by `_compute_eye_pole_uv` which
tests all six axis directions and picks the one whose weighted-average UV
lands closest to (0.5, 0.5) — robust to FBX axis conversion and invariant
under skinning.

Shader graph (live):
```
TexCoord.UV → Mapping(location=(0.5 - 4·pu, 0.5 - 4·pv, 0), scale=4)
            → TexImage(T_Iris_A_M, EXTEND, Non-Color)
              → SeparateColor
                 R → MapRange(0.25..0.75 → 0.65..1.15)      [fibril gray]
                     Mix.MULTIPLY(iris_color, gray)         → iris_mul
                 B → Ramp(0.095→0, 1.0→1)                   [limbus fac]
                     Mix(iris_mul, iris_dark, limbus_fac)   → limbus

Radial UV distance from pole:
   → Ramp(0.131→0, 0.146→1)    [iris-vs-sclera crisp ring]
      Mix(limbus, sclera)                                   → ring
   → Ramp(0.022→1, 0.046→0)    [pupil disc]
      Mix(ring, pupil)                                      → final

final → Principled BSDF.BaseColor  (Roughness 0.25, IOR 1.5)
```

Colors are hand-tuned defaults (`IRIS`, `IRIS_DARK`, `SCLERA`, `PUPIL` in
`_build_face_accessory_material`). Replaceable per-character later if we
decide to surface them in the character manifest.

### Clothing — parametric, multi-overlay

**Texture set referenced by each MI** (per-garment, found by MI path
key-grouping in `_wire_materials`):

| File pattern | Role | Classified as | How we use it |
|---|---|---|---|
| `<item>_N.tga` | Base normal | `normal` | Direct to Principled.Normal via NormalMap |
| `<item>_AO.tga` | AO | `ao` | Multiply into basecolor at `C_AOMultAmount` |
| `<item>_Mask.tga` | Region selector | `mask` | R channel mixes `diffuse_color_1`↔`diffuse_color_2` |
| `micro_*_diffuse.tga` | Small-scale weave albedo | `detail_diffuse` | Tiled by `DetailTex_UVTtiling`, multiply-blended |
| `micro_*_N.tga` | Small-scale weave normal | `detail_normal` | NOT YET WIRED (future) |
| `macro_*.tga` | Large-scale variation (heather, canvas) | `macro_overlay` | Tiled by `MacroTex_UvTiling`, overlay-blended |
| `memory_*.tga` | Live-rig wrinkle overlay | — | Dropped (not useful without rig) |
| `pilling_*.tga`, `*_H.tga` | Pilling dust / height | — | Dropped for v1 |
| `WhiteSquareTexture.tga`, `black_masks.tga` | Placeholders | — | Dropped |

**MI param conventions** (MH cloth shader family — `M_f_top_shirt`,
`M_btm_slacks`, etc.):

| Param | Type | Meaning | How we apply |
|---|---|---|---|
| `diffuse_color_1` | vector | Primary flat color | Base of mask blend (or flat fallback) |
| `diffuse_color_2` | vector | Secondary flat color | Other side of mask blend |
| `C_color` | vector | Composited tint (rarely used) | Fallback when `diffuse_color_1` absent |
| `C_roughness value` | scalar | Roughness | Direct to Principled.Roughness |
| `c_metalness value` | scalar | Metallic | Direct to Principled.Metallic (often 0) |
| `Anisotropy`, `AnisotropicRotation` | scalar | Fabric sheen direction | NOT YET WIRED |
| `DetailTex_UVTtiling` | scalar (10..300) | detail_diffuse tile count | Mapping node scale (clamped 1..400) |
| `DetailTex_VariationStrength` | scalar (0..1) | detail_diffuse blend amt | Mix factor for detail multiply |
| `DetailTex_NormalStrength` | scalar (0..1) | detail_normal strength | NOT YET WIRED |
| `MacroTex_UvTiling` | scalar (1..10) | macro_overlay tile count | Mapping node scale |
| `MacroTexStrength` | scalar (0..1) | macro_overlay blend amt | OVERLAY blend factor |
| `C_AOMultAmount` | scalar (0..1) | AO blend strength | Multiply mix factor |
| `WearMaskStrength`, `VariationBlend*`, `FuzzPower`, `FuzzExp`, `wrinkle_*` | scalar | Secondary effects | NOT YET WIRED (minor visual delta) |

**Recipe** (`_build_pbr_material`, cloth path):

```
base = (Mask.R ? diffuse_color_1 : diffuse_color_2)         [two-tone]
     × detail_diffuse (tiled, multiply @ VariationStrength)  [weave]
     ⊕ macro_overlay (tiled, overlay @ MacroTexStrength)     [variation]
     × AO (multiply @ C_AOMultAmount)                        [shadow]
→ Principled.BaseColor
```

**Unknowns per-garment**:
- Which **Mask channel** is the primary color selector — we assume R;
  other garments may pack the selector in G/B/A (common when R is used
  for stitch masking). If a garment looks wrong-one-color, probe the
  mask texture's channel variances and update.
- Fuzz/sheen, fabric anisotropy — require Principled v2 Sheen or a custom
  node group. Skipped because glTF emits only Principled.

### Hair cards (`MI_Hair_Cards`, `MI_Facial_Hair`)

No albedo texture — color is synthesized from `hairMelanin` (scalar 0..1),
`hairRedness` (scalar 0..1), `hairDye` (vector, optional) in
`_pick_mi_basecolor`. Alpha mask from compact-atlas (`*CardsAtlas_Attribute`
R channel) preferred over legacy `*RootUVSeedCoverage` (A channel) — the
compact atlas gives true per-strand silhouettes; legacy mask reads as
opaque cardboard when cards overlap.

Root darkening via B channel of compact atlas → color ramp → multiply
into basecolor.

Render: `HASHED` dither (EEVEE/Cycles handles thin strands without
draw-order sorting).

## When adding a new character

Check in this order:

1. **Same archetype (`f_med_nrw`) + same outfit**: nothing to do; existing
   cloth paths work.
2. **Different outfit**: look at `mh_manifest.json → textures[]` filtered by
   `material == /.../M_new_garment...`. If all textures fall into the
   classified roles above, you're fine. If new texture naming appears,
   extend `_classify_texture`.
3. **Different archetype / body type**: body + face paths unchanged (they
   use `MI_BodySynthesized` / `MI_HeadSynthesized` universally).
4. **Eye colors look wrong**: not a classification bug — the hand-tuned
   `IRIS`/`IRIS_DARK` constants in the eye_refractive branch don't know
   about the character's IrisColorPicker palette selection. Override per
   character if needed.

## When extending: what to add

The clearest extension points, ranked by visual payoff per line of code:

1. **Detail normal wiring** (cloth): combine `detail_normal` with base
   normal at `DetailTex_NormalStrength`. Same pattern as `T_SkinMicroNormal`
   in `_build_skin_material`. Gives fabric bumpiness.
2. **Per-garment mask channel auto-detect**: sample a few pixels of the
   mask texture and pick the channel with highest variance as the
   color-1/color-2 selector (current R-hardcoded).
3. **Sheen emulation** (cloth): map `FuzzPower`/`FuzzExp` to
   Principled v2 Sheen Weight / Roughness. Subtle but sells velvet/wool.
4. **IrisColorPicker palette**: bundle the UE `T_IrisColorPicker.tga` in
   `characters/_shared/` and sample at `IrisColor1U/V` per-character to
   drive `IRIS` constant. Would eliminate hand-tuned eye colors.

## Debugging cookbook

- Material has NO basecolor at all (pure BSDF default gray):
  check `mh_manifest.textures[]` — if empty for that material path,
  stage 01 failed to resolve the MI's texture dependencies. Not stage 02's
  problem to fix.
- Eye is pure sclera (no iris): `_compute_eye_pole_uv` fell back to
  (0.5, 0.5). Check `[stage02] eye pole:` log line; if forward detection
  printed a weird axis or ultra-off-center UV, investigate pole detection
  first.
- Cloth is flat dark: either the Mask texture is binary 0/1 everywhere
  (no mid-value region selection), or mask R isn't the right channel for
  this particular garment. Check other channels.
- Cloth weave pattern missing: check `mh_manifest.textures[]` for a
  `micro_*_diffuse` file on this material path. If present but not in
  blend_manifest's `materials_applied[].textures`, `_classify_texture`
  isn't routing it to `detail_diffuse` — add the pattern.
