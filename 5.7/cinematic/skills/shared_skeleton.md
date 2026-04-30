# Shared Skeleton Across MetaHumans

All MetaHumans in a given UE version reference the same skeleton asset:

  /Game/MetaHumans/Common/Female/Medium/NormalWeight/Body/metahuman_base_skel

This is true regardless of character gender, body archetype, or whether the character
was made with the legacy web MHC or the in-engine 5.6 Creator.

## Implication for the pipeline

- Export the skeleton FBX **once per pipeline run**, not per character.
- The per-character `mh_manifest.json` records a *reference* to the shared skeleton path
  (relative to the workspace root), not a copy.
- Stage 02 (Blender) imports the shared skeleton once, then binds each character's
  meshes to it.
- When UE version changes (5.7, 5.8, ...), the skeleton may change. Each version folder
  under `stages/01-metahuman-engine-export/<ver>/` writes its own skeleton FBX into a
  shared location (e.g. `characters/_shared/5.6.1/skeleton/`).

## Do not

- Do not re-export the skeleton per character (waste, divergent file hashes)
- Do not assume the Female path means female-only; male chars use the same asset
- Do not mix skeletons across UE versions in the same character's build
