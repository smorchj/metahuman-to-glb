"""Propagate the face mesh's ARKit 52 shape keys onto the separate groom
meshes (brows, mustache, goatee, beard, stubble) so they follow the face
when a blendshape fires.

Why this exists
---------------
MH groom meshes (Epic's card-based hair system) are separate meshes placed
on the face surface at rest. They are skinned to the head bone and deform
with skeletal animation, but they carry NO blendshapes of their own.
When the face mesh's jawOpen key fires, the lower-face verts translate
~2 cm down while the beard hanging below stays put — visible detachment.

Strategy
--------
For each groom vert, find the k=4 nearest face verts (in world space, from
the face's Basis shape), compute inverse-distance-squared weights, and
sample the weighted average of every face shape key's delta. Write a
matching shape key onto the groom (same name → same index in the viewer's
morphTargetDictionary → slider drives all meshes in lockstep).

Key weighting property: with w = 1 / (d² + ε) then normalized, a groom
vert sitting exactly on a face vert (d≈0) gets ~100% of that neighbor's
delta, while a vert drifting between two face verts gets a clean blend
proportional to inverse-distance.

Detection
---------
Auto-detect groom meshes by MetaHuman's standardised object-name prefix:
  Eyebrows_*   Mustache_*   Moustache_*   Goatee_*   Beard_*   Stubble_*
  Sideburns_*   Fuzz_*
Explicitly skip Hair_* (scalp hair — skull doesn't deform with ARKit so
any morph transfer there would be noise).

Eyelashes are not a separate mesh on MH — they live as a material slot on
the face mesh (face_slot="eyelashes"), so they inherit the face's shape
keys automatically. No special handling needed here.

Space handling
--------------
Everything is done in world space: face Basis → world, groom verts →
world, NN search in world, delta rotations via matrix_world. The resulting
groom deltas are then brought back to groom-local before writing into the
shape key (Blender stores shape key positions in object-local space).
"""
from __future__ import annotations

import numpy as np
import bpy


# ---------------------------------------------------------------------- #
# Detection                                                               #
# ---------------------------------------------------------------------- #

# Prefix test is case-sensitive — matches UE's export naming. If a new
# facial-groom kind shows up (e.g. Whiskers_), add it here. Keep Hair_ OUT.
_GROOM_PREFIXES = (
    "Eyebrows_",
    "Mustache_",
    "Moustache_",
    "Goatee_",
    "Beard_",
    "Stubble_",
    "Sideburns_",
    "Fuzz_",
)


def _is_groom_name(name: str) -> bool:
    # Mesh-name prefix match. The full UE pattern is
    #   <kind>_<style>_CardsMesh_Group<N>_LOD<N>
    # but we only need the kind token for classification.
    return any(name.startswith(p) for p in _GROOM_PREFIXES)


def _find_groom_meshes() -> list[bpy.types.Object]:
    return [o for o in bpy.data.objects
            if o.type == "MESH" and _is_groom_name(o.name)]


# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #

def _basis_positions(mesh) -> np.ndarray:
    """(N, 3) float32 Basis shape key positions in object-local space.
    Falls back to mesh.vertices if no shape keys (shouldn't happen after
    apply_arkit52 ran, but defensively safe)."""
    sk = mesh.shape_keys
    if sk and "Basis" in sk.key_blocks:
        kb = sk.key_blocks["Basis"]
        arr = np.empty(len(kb.data) * 3, dtype=np.float32)
        kb.data.foreach_get("co", arr)
        return arr.reshape(-1, 3)
    arr = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)


def _vert_positions(mesh) -> np.ndarray:
    arr = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)


def _matrix_world_np(obj) -> tuple[np.ndarray, np.ndarray]:
    """Return (R, t) where R is 3x3 and t is 3, both float64. Captures the
    object's world transform, ignoring scale-shear subtleties (MH meshes
    have uniform scale + rotation in practice)."""
    M = np.array(obj.matrix_world, dtype=np.float64)
    return M[:3, :3], M[:3, 3]


# ---------------------------------------------------------------------- #
# Public entrypoint                                                       #
# ---------------------------------------------------------------------- #

# Number of nearest face verts to blend per groom vert. k=4 is the sweet
# spot: k=1 pops at seams (hair card with one vert on each side of mouth
# corner jumps), k=8 over-smooths and kills the corner definition.
_K = 4

# ε in inverse-distance weighting (w = 1 / (d² + ε)). Small enough that
# d≈0 still dominates (~100% weight), large enough that d→0 doesn't
# produce an actual division by zero.
_EPS = 1e-8


def apply(*, char_id: str = "(unknown)") -> dict:
    """Transfer the face's existing ARKit shape keys onto every groom mesh
    in the scene. Call AFTER apply_arkit52.apply() has populated the face
    mesh with its shape keys.

    Returns a summary dict listing each groom processed + stats.
    """
    face_obj = _find_face_mesh()
    if face_obj is None:
        raise RuntimeError("apply_arkit52_grooms: no face mesh in scene")
    mesh = face_obj.data
    sk = mesh.shape_keys
    if sk is None or len(sk.key_blocks) <= 1:
        print("[grooms] face has no shape keys, nothing to transfer", flush=True)
        return {"char_id": char_id, "grooms_processed": 0, "grooms": []}

    grooms = _find_groom_meshes()
    if not grooms:
        print("[grooms] no groom meshes found (prefixes: "
              + ", ".join(_GROOM_PREFIXES) + ")", flush=True)
        return {"char_id": char_id, "grooms_processed": 0, "grooms": []}

    # --- Face side: basis + per-shape-key deltas, all in world space ------- #
    basis_local = _basis_positions(mesh)
    R_face, t_face = _matrix_world_np(face_obj)
    basis_world = basis_local @ R_face.T + t_face   # (N_f, 3)

    # Per-shape-key deltas in LOCAL, then rotate to WORLD (no translation —
    # deltas are direction vectors, not points).
    face_deltas_world: dict[str, np.ndarray] = {}
    for kb in sk.key_blocks:
        if kb.name == "Basis":
            continue
        pos = np.empty(len(kb.data) * 3, dtype=np.float32)
        kb.data.foreach_get("co", pos)
        delta_local = pos.reshape(-1, 3) - basis_local
        if not np.any(np.abs(delta_local) > 1e-7):
            continue  # skip all-zero keys (shouldn't exist but be tidy)
        face_deltas_world[kb.name] = delta_local.astype(np.float64) @ R_face.T

    if not face_deltas_world:
        print("[grooms] face shape keys all zero, nothing to transfer", flush=True)
        return {"char_id": char_id, "grooms_processed": 0, "grooms": []}

    print(f"[grooms] face={face_obj.name}  basis_verts={basis_world.shape[0]}  "
          f"keys={len(face_deltas_world)}  grooms={[g.name for g in grooms]}",
          flush=True)

    # --- KD-tree over face basis in world space ----------------------------- #
    from mathutils.kdtree import KDTree
    from mathutils import Vector

    tree = KDTree(basis_world.shape[0])
    for i, p in enumerate(basis_world):
        tree.insert(Vector((float(p[0]), float(p[1]), float(p[2]))), i)
    tree.balance()

    per_groom_summary = []
    for groom_obj in grooms:
        summary = _apply_to_groom(groom_obj, face_deltas_world, basis_world, tree)
        per_groom_summary.append(summary)

    return {
        "char_id": char_id,
        "grooms_processed": len(grooms),
        "grooms": per_groom_summary,
        "k_neighbors": _K,
    }


def _apply_to_groom(groom_obj, face_deltas_world, basis_world, tree) -> dict:
    groom_mesh = groom_obj.data
    R_g, t_g = _matrix_world_np(groom_obj)
    groom_local = _vert_positions(groom_mesh).astype(np.float64)
    groom_world = groom_local @ R_g.T + t_g
    N_g = groom_world.shape[0]

    # ---- k-NN against the face basis -------------------------------------- #
    from mathutils import Vector
    nn_idx = np.empty((N_g, _K), dtype=np.int64)
    nn_dist = np.empty((N_g, _K), dtype=np.float64)
    for gi in range(N_g):
        p = groom_world[gi]
        results = tree.find_n(Vector((float(p[0]), float(p[1]), float(p[2]))), _K)
        for k, (_co, idx, d) in enumerate(results):
            nn_idx[gi, k] = idx
            nn_dist[gi, k] = d

    # Inverse-distance² weights, normalized per-vert. When one d is much
    # smaller than the others, it dominates (→ ~100%), which matches the
    # "if it's sitting on a face vert, follow it fully" intent.
    w = 1.0 / (nn_dist * nn_dist + _EPS)
    w /= w.sum(axis=1, keepdims=True)

    # ---- Wipe any prior shape keys on the groom --------------------------- #
    if groom_mesh.shape_keys is None:
        groom_obj.shape_key_add(name="Basis", from_mix=False)
    existing = [kb.name for kb in groom_mesh.shape_keys.key_blocks if kb.name != "Basis"]
    for name in reversed(existing):
        groom_obj.shape_key_remove(groom_mesh.shape_keys.key_blocks[name])

    # ---- Inverse groom rotation for world→local delta conversion ---------- #
    # Rotation-only inverse (no translation, since we're converting directions,
    # not points). For orthonormal R this is R.T; we use actual inverse to be
    # safe against non-uniform scale.
    R_g_inv = np.linalg.inv(R_g)

    created = 0
    skipped_zero = []
    for key_name, fd_world in face_deltas_world.items():
        # Sample the weighted face delta at each groom vert.
        #   fd_world[nn_idx]        shape (N_g, K, 3)
        #   w[..., None]            shape (N_g, K, 1)
        sampled_world = np.einsum("gk,gkd->gd", w, fd_world[nn_idx])  # (N_g, 3)

        if not np.any(np.abs(sampled_world) > 1e-6):
            skipped_zero.append(key_name)
            continue

        sampled_local = sampled_world @ R_g_inv.T   # directions → groom local
        new_positions = (groom_local + sampled_local).astype(np.float32)

        kb = groom_obj.shape_key_add(name=key_name, from_mix=False)
        # Same fix as apply_arkit52: new keys default to .value=1.0 in
        # Blender 5.0, which the glTF exporter bakes into default mesh
        # weights → zombie face at rest. Force 0.
        kb.value = 0.0
        kb.data.foreach_set("co", new_positions.reshape(-1))
        created += 1

    nn0 = nn_dist[:, 0]
    print(f"[grooms]   {groom_obj.name}: verts={N_g}  created={created}  "
          f"skipped_zero={len(skipped_zero)}  "
          f"nn0 mean={nn0.mean():.4f} p95={np.percentile(nn0, 95):.4f} max={nn0.max():.4f}",
          flush=True)

    return {
        "groom":              groom_obj.name,
        "verts":              int(N_g),
        "shape_keys_created": int(created),
        "shape_keys_skipped_zero": skipped_zero,
        "nn_dist_mean":       float(nn0.mean()),
        "nn_dist_p95":        float(np.percentile(nn0, 95)),
        "nn_dist_max":        float(nn0.max()),
    }


# Tiny shim so this module doesn't take a hard dep on apply_arkit52's
# internal _find_face_mesh (import-order pain during Blender headless runs).
def _find_face_mesh() -> bpy.types.Object | None:
    cands = [o for o in bpy.data.objects
             if o.type == "MESH"
             and "facemesh" in o.name.lower()
             and o.name.lower().endswith("lod0")]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        return max(cands, key=lambda o: len(o.data.vertices))
    return None
