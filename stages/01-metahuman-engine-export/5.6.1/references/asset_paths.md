# Asset Paths — UE 5.6.1

## Per-character (what we export fresh per run)

```
/Game/MetaHumans/<CharName>/
  BP_<CharName>                 # blueprint (reference only)
  Face/Mesh/Face_LOD0_Mesh      # head skeletal mesh
  Face/Textures/T_*             # per-char face textures
  Materials/MI_*                # per-char material instances
```

## Shared (exported once per pipeline run, cached in _shared/5.6.1/)

```
/Game/MetaHumans/Common/Female/Medium/NormalWeight/Body/
  metahuman_base_skel           # THE skeleton, used by all chars regardless of gender
  f_med_nrw_body                # female medium narrow body mesh
/Game/MetaHumans/Common/Male/.../m_med_nrw_body
/Game/MetaHumans/Common/<archetype>/...
```

## How the script finds assets

`unreal.AssetRegistryHelpers.get_asset_registry()` + `ARFilter` with `recursive_paths=True`
on `/Game/MetaHumans/<CharName>/` — captures face + any per-char mesh variants.

For the shared body mesh, the character's BP component tree names the asset. The current
v1 export walks all SkeletalMesh assets under the char folder and separately exports the
shared skeleton. Body-mesh deduping by archetype is a v2 improvement.

## The "Mesh=None" 5.6 quirk

Sometimes the assembled BP's Body component shows `Mesh=None` even though the viewport
renders correctly. Fallback: read the MetaHuman Creator source BP reference (under
`/Game/MetaHumans/<CharName>/Source/`) to resolve the skeletal mesh asset. If our v1
script hits this, it currently surfaces as a warning; log and handle in v2.
