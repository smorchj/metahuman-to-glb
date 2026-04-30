# MetaHuman 5.6.1 Asset Layout (Reference)

Post-assembly (whether legacy web-MHC or in-engine 5.6 Creator with "UE Optimized"), a
MetaHuman character lives in `/Game/MetaHumans/<CharName>/`. Structure:

```
/Game/MetaHumans/<CharName>/
  BP_<CharName>                       # Actor blueprint — entry point
  Body/                               # per-char body variant assets (if any)
  Face/
    Mesh/Face_LOD0_Mesh               # per-char head skeletal mesh
    Textures/                         # per-char head textures (BaseColor, Normal, Roughness, etc.)
  Materials/                          # per-char material instances (derived from Common)
/Game/MetaHumans/Common/                 # SHARED across all MHs — export once per archetype
  Female/Medium/NormalWeight/Body/
    metahuman_base_skel              # THE skeleton (used by all chars, all genders!)
    f_med_nrw_body                   # body skeletal mesh by archetype
  Male/.../
    m_med_nrw_body
  ...                                # other body archetypes
  Textures/, Materials/              # shared body textures + masters
```

## Blueprint component hierarchy (gotcha)

In the BP, the top-level `Body` mesh is **just the hands**. Actual body and head are
children:

  Body (hands only)
   ├── Body_Mesh       (torso/legs)
   │   └── Face_Mesh   (head, eyes, teeth, eyelashes as sub-components)
   │       ├── Teeth
   │       ├── Eyes (L/R)
   │       └── Eyelashes

Walk the component tree — don't just export the top node.

## Hidden geometry per LOD

MH strips occluded geometry per LOD for perf. LOD0 retains most but not all body under
clothing. For web GLB we take LOD0 as-is; if a visible seam shows up after Blender import,
stage 02 handles it.

## Archetype ID

Body archetype matters for batch: `f_med_nrw`, `m_med_nrw`, `m_tal_nrw`, etc. Two chars
sharing an archetype share a body mesh **and** body textures. Manifest records archetype;
the exporter dedupes shared exports per archetype per run.

## Skeleton

One skeleton for all MHs: `/Game/MetaHumans/Common/Female/Medium/NormalWeight/Body/metahuman_base_skel`
— counterintuitively under the Female path, but used by male chars too. Export once,
manifest references it, stages 02+ reuse.
