# FBX Export Rules (Reference)

## Hard rules before any export

1. **Zero transforms.** Component position + rotation must be 0 at export time or the rig
   lands offset in Blender. Reset per-component before running `AssetExportTask`.
2. **LOD0 only** for v1. Disable "Level of Detail" in `FbxExportOption`.
3. **Bake Material Inputs = false.** This flag only writes texture *names* into the FBX
   material slots — it does not embed images. We export textures separately.
4. **Vertex colors on.** MH meshes use vertex color for mask data; preserve.
5. **Morph targets on.** Face blendshapes are part of the rig; we want them in the FBX
   for stage 02, even if stage 03 drops them for the web build.

## Textures: separate pass

FBX carries mesh + material *names* only. For each skeletal mesh:

  for mat in mesh.materials:
      for param in mat.texture_parameters:
          export Texture2D(param) → 01-fbx/textures/<name>.tga
          record {asset_path, file_path, material, slot} in manifest

Textures format: **TGA** (alpha-safe, Blender-friendly, lossless). PNG also fine; pick
one and stay consistent.

## Output naming

  meshes/body.fbx
  meshes/head.fbx
  meshes/teeth.fbx
  meshes/eye_left.fbx
  meshes/eye_right.fbx
  meshes/eyelashes.fbx
  textures/<OriginalAssetName>.tga         # preserve UE asset names for traceability
  skeleton/metahuman_base_skel.fbx         # written once, referenced by manifest

## Shared-asset dedupe

If the exporter has already written `metahuman_base_skel.fbx` or the archetype body in
a prior run in the same pipeline invocation, skip re-export. Record the existing path in
the manifest so stage 02 can still find it.

## Validation checks (run after export)

- Every asset path in the manifest exists on disk and is non-zero bytes
- Mesh FBXs open (header parse)
- Each material in the manifest references at least one texture file that exists
- Skeleton FBX is present
