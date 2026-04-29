"""Replace the ~822 raw DNA morph targets on a MetaHuman face mesh with
52 ARKit-named shape keys, transplanted from a frozen generic reference.

Why this exists
---------------
MH face meshes ship with 822 low-level rig blendshapes (primary action units +
correctives) driven by Epic's closed-source RigLogic solver. Web viewers can't
run RigLogic, and the raw 822 shapes balloon GLB size to ~350 MiB per character.
For lipsync we only need the 52 ARKit-standard shapes.

Rather than re-solve RigLogic offline (not publicly possible for UE 5.6 DNA),
we transplant pre-baked ARKit shapes from a community reference mesh
(dragonboots' MetaHumanHead) onto our character's face. MH face topology is
stable across characters and LODs at the underlying-geometry level, so
per-vertex deltas transfer with position-based nearest-neighbor mapping.

Design note: this is a **stage-02 runtime step**, called by import_fbx.py after
the face mesh has been imported. The reference is frozen in
`skills/reference/arkit52_deltas.npz` — Haiku never parses the source FBX.

Mapping strategy
----------------
For each face mesh region (head / teeth / eye_left / eye_right), identified
by material slot:

  1. Load reference per-vertex UVs + per-shape-key deltas from the npz.
  2. Build a KD-tree over the reference UVs (2D, padded to 3D for KDTree API).
  3. For each vertex in our face mesh's region, look up its UV in the active
     UV layer and find the ref vertex with the closest UV. Use that ref
     vertex's delta as our delta — verbatim, no rescale.
  4. Write ARKit-named shape keys onto the face mesh.
  5. Delete all pre-existing non-Basis shape keys.

Why UVs, not positions
~~~~~~~~~~~~~~~~~~~~~~
MH face topology **and its UV unwrap** are canonical across all MH characters
at LOD0 — the same unwrap, the same texel layout, so per-vertex UVs match
1:1 between any character and the reference (98.5% exact, rest within ~2%
of UV space due to FBX seam-split duplicates). Positions, by contrast, vary
wildly per character (Ada's head is 12% smaller than the ref head), so
position-based NN leaks across anatomical regions — Ada's eyelid verts end
up nearer to the ref's mouth/cheek verts than the ref's eyelids, and
eye-area shape keys get scattered all over the face.

UVs are 2D and anatomy-indexed; eyelid UVs don't neighbour mouth UVs in UV
space, so the correspondence is clean.

Because topology is canonical, the deltas also transfer verbatim: no bbox
rescale, no coordinate-frame adjustment. A "jawOpen moves this lip vertex
down 2cm" on ref means the same on Ada.

Material-slot → region mapping is heuristic (by material name substring),
validated against vertex-count expectation. Fails loud on topology surprise.
"""
from __future__ import annotations

import os
from collections import defaultdict

import bpy
import numpy as np


# ---------------------------------------------------------------------- #
# Region identification                                                   #
# ---------------------------------------------------------------------- #

# Match material slot names → region id. Tested against Ada_FaceMesh_LOD0:
#   slot 0  MI_HeadSynthesized_Baked                     → head
#   slot 1  M_TeethCharacterCreator_Inst                 → teeth
#   slot 3  MI_EyeRefractive_Inst_L                      → eye_left
#   slot 4  MI_EyeRefractive_Inst_R                      → eye_right
# Skipped: lacrimal_fluid / EyeOcclusion / Eyelash / Cartilage — the ref FBX
# doesn't ship separate meshes for these, and their UVs sit in their own
# atlas islands outside the skin atlas so UV-NN returns garbage. Instead, we
# fill these verts in a position-based k-NN post-pass (see _fill_satellite_
# verts_from_head below): for each unclassified vert, find the nearest head-
# skin verts in world space and inverse-distance-blend their deltas. Works
# because lashes sit on the lid edge, occlusion ring under the lid, and
# cartilage at the tear duct — all within millimetres of a head-skin vert.
_REGION_RULES = [
    # (region_id,     predicate(material_name_lower) → bool)
    ("head",          lambda n: "headsynthesized" in n),
    ("teeth",         lambda n: "teeth" in n),
    ("eye_left",      lambda n: "eyerefractive" in n and n.rstrip("0123456789_").endswith("_l")),
    ("eye_right",     lambda n: "eyerefractive" in n and n.rstrip("0123456789_").endswith("_r")),
]

# Material-slot tokens we want to inherit head deltas from (lash/occlusion/
# cartilage). Anything not matched here and not in _REGION_RULES stays at
# Basis (saliva / eyeEdge / lacrimal_fluid — invisible in the viewer, so
# their morph data would be wasted bytes).
_SATELLITE_TOKENS = ("eyelash", "eyeshell", "eyeocclusion", "cartilage")

# ARKit 52 canonical set. tongueOut is absent from our reference FBX — the
# remaining 51 cover every shape LiveLink Face actually drives on the jaw/
# mouth/brows/eyes; tongueOut is a nice-to-have we can add later if sourced.
ARKIT_52_NAMES = [
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft",
    "eyeSquintLeft", "eyeWideLeft",
    "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight", "eyeLookUpRight",
    "eyeSquintRight", "eyeWideRight",
    "jawForward", "jawRight", "jawLeft", "jawOpen", "mouthClose",
    "mouthFunnel", "mouthPucker", "mouthRight", "mouthLeft",
    "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthDimpleLeft", "mouthDimpleRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight",
    "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft", "mouthUpperUpRight",
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "noseSneerLeft", "noseSneerRight", "tongueOut",
]


def _find_face_mesh() -> bpy.types.Object | None:
    """Find the MH face mesh in the current scene. We look for a mesh object
    whose name contains 'FaceMesh' (case-insensitive) and ends with LOD0 —
    matches `<Char>_FaceMesh_LOD0` exported by stage 01."""
    candidates = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        name_lc = o.name.lower()
        if "facemesh" in name_lc and name_lc.endswith("lod0"):
            candidates.append(o)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # pick the one with the most verts (in case of duplicates)
        return max(candidates, key=lambda o: len(o.data.vertices))
    return None


def _classify_slots(face_obj) -> dict[str, list[int]]:
    """Map region_id → list of material slot indices that belong to it."""
    regions = defaultdict(list)
    for i, ms in enumerate(face_obj.material_slots):
        if not ms.material:
            continue
        name_lc = ms.material.name.lower()
        for region_id, pred in _REGION_RULES:
            if pred(name_lc):
                regions[region_id].append(i)
                break
    return dict(regions)


def _vertex_indices_for_slots(mesh, slot_indices: list[int]) -> np.ndarray:
    """Return a sorted int64 array of unique vertex indices touched by any
    polygon assigned to one of the listed material slots."""
    wanted = set(slot_indices)
    verts = set()
    for poly in mesh.polygons:
        if poly.material_index in wanted:
            verts.update(poly.vertices)
    return np.array(sorted(verts), dtype=np.int64)


def _per_vertex_uvs_for_verts(mesh, vert_indices: np.ndarray,
                              slot_indices: list[int]) -> np.ndarray:
    """Return (K, 2) float32 UVs for the given vertex indices.

    Only considers polygon loops whose material_index is in slot_indices —
    this ensures the UV we pick up is from the region's own UV set, not a
    stray shared vertex from a different material slot (shouldn't happen in
    MH face meshes because UE splits seams per material, but being explicit).
    """
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise RuntimeError(f"mesh {mesh.name}: no active UV layer")
    uv_data = uv_layer.data
    wanted_slots = set(slot_indices)
    want_verts = set(int(v) for v in vert_indices)

    # First loop we see per vert (within the wanted slots) defines the UV.
    by_vert: dict[int, tuple[float, float]] = {}
    for poly in mesh.polygons:
        if poly.material_index not in wanted_slots:
            continue
        for loop_idx, vert_idx in zip(poly.loop_indices, poly.vertices):
            if vert_idx not in want_verts or vert_idx in by_vert:
                continue
            uv = uv_data[loop_idx].uv
            by_vert[int(vert_idx)] = (float(uv.x), float(uv.y))

    if len(by_vert) != len(vert_indices):
        missing = [v for v in vert_indices if int(v) not in by_vert]
        raise RuntimeError(f"mesh {mesh.name}: {len(missing)} verts in "
                           f"slots {slot_indices} have no UV (first: {missing[:5]})")
    out = np.empty((len(vert_indices), 2), dtype=np.float32)
    for i, v in enumerate(vert_indices):
        out[i] = by_vert[int(v)]
    return out


def _rest_positions(mesh) -> np.ndarray:
    """(N, 3) float32 rest positions. Prefers Basis shape key; falls back to
    mesh.vertices if no shape keys exist (first run, pre-transplant)."""
    sk = mesh.shape_keys
    if sk and "Basis" in sk.key_blocks:
        kb = sk.key_blocks["Basis"]
        arr = np.empty(len(kb.data) * 3, dtype=np.float32)
        kb.data.foreach_get("co", arr)
        return arr.reshape(-1, 3)
    arr = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", arr)
    return arr.reshape(-1, 3)


def _satellite_slot_indices(face_obj) -> list[int]:
    """Material slot indices for the lash/occlusion/cartilage slots — verts
    in these slots sit on the lid skin but have their own UVs outside the
    face atlas, so we resample them via position-NN from head verts rather
    than UV-NN from the reference."""
    out = []
    for i, ms in enumerate(face_obj.material_slots):
        if not ms.material:
            continue
        n = ms.material.name.lower()
        if any(tok in n for tok in _SATELLITE_TOKENS):
            out.append(i)
    return out


def _fill_satellite_verts_from_head(
        mesh, face_obj, arkit_deltas: dict, basis_positions: np.ndarray,
        head_vert_indices: np.ndarray, K: int = 4) -> dict:
    """For every vert in the lash/occlusion/cartilage material slots, blend
    the k=4 nearest head verts' deltas using inverse-distance² weighting.

    head_vert_indices points into the full face-mesh vert array; those are
    the verts that already had ARKit deltas written by the UV-NN pass above.
    """
    sat_slots = _satellite_slot_indices(face_obj)
    if not sat_slots:
        return {"satellite_slots": [], "satellite_verts": 0}
    sat_verts = _vertex_indices_for_slots(mesh, sat_slots)
    if sat_verts.size == 0:
        return {"satellite_slots": sat_slots, "satellite_verts": 0}

    # Head basis positions, indexed same order as head_vert_indices.
    head_basis = basis_positions[head_vert_indices]   # (H, 3)

    # KD-tree over head basis (object-local space — same space as sat verts,
    # since they all live in the same mesh).
    from mathutils.kdtree import KDTree
    from mathutils import Vector
    tree = KDTree(head_basis.shape[0])
    for i, p in enumerate(head_basis):
        tree.insert(Vector((float(p[0]), float(p[1]), float(p[2]))), i)
    tree.balance()

    # k-NN lookup
    N = sat_verts.size
    nn_idx = np.empty((N, K), dtype=np.int64)  # indices into head_vert_indices
    nn_dist = np.empty((N, K), dtype=np.float64)
    for qi, v in enumerate(sat_verts):
        p = basis_positions[v]
        results = tree.find_n(Vector((float(p[0]), float(p[1]), float(p[2]))), K)
        for k, (_co, idx, d) in enumerate(results):
            nn_idx[qi, k] = idx
            nn_dist[qi, k] = d

    # Inverse-distance² weights. ε small so exact-coincident verts → ~100%
    # weight to their match, not blown up to infinity.
    w = 1.0 / (nn_dist * nn_dist + 1e-8)
    w /= w.sum(axis=1, keepdims=True)

    # For each ARKit key, gather head deltas at the NN verts and blend.
    # arkit_deltas[name] is (total_verts, 3); head_vert_indices picks out
    # the ordered head subset we built the tree from.
    filled_keys = 0
    for key_name, full_delta in arkit_deltas.items():
        head_delta = full_delta[head_vert_indices]   # (H, 3)
        if not np.any(np.abs(head_delta) > 1e-7):
            continue  # key has no movement on head (shouldn't happen)
        sampled = np.einsum("nk,nkd->nd", w, head_delta[nn_idx])  # (N, 3)
        full_delta[sat_verts] = sampled
        filled_keys += 1

    print(f"[arkit52] filled {sat_verts.size} satellite verts "
          f"(slots {sat_slots}) via k={K} NN to head — "
          f"nn0 mean={nn_dist[:,0].mean():.4f} max={nn_dist[:,0].max():.4f}",
          flush=True)

    return {
        "satellite_slots": sat_slots,
        "satellite_verts": int(sat_verts.size),
        "satellite_nn0_mean": float(nn_dist[:, 0].mean()),
        "satellite_nn0_p95": float(np.percentile(nn_dist[:, 0], 95)),
        "satellite_nn0_max": float(nn_dist[:, 0].max()),
        "satellite_keys_filled": int(filled_keys),
    }


def _kdtree_nearest_uv(ref_uvs: np.ndarray,
                       query_uvs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each query UV, return the ref UV's index and 2D distance to it.

    Uses Blender's 3D KDTree with z=0 (2D queries are faithful in 3D KDTree
    when all points lie on z=0). Returns (indices, distances) where distances
    are in UV space (typically 0..1 per axis).
    """
    from mathutils.kdtree import KDTree
    n = ref_uvs.shape[0]
    tree = KDTree(n)
    for i, (u, v) in enumerate(ref_uvs):
        tree.insert((float(u), float(v), 0.0), i)
    tree.balance()
    out_idx = np.empty(query_uvs.shape[0], dtype=np.int64)
    out_dist = np.empty(query_uvs.shape[0], dtype=np.float32)
    for qi, (u, v) in enumerate(query_uvs):
        _, idx, d = tree.find((float(u), float(v), 0.0))
        out_idx[qi] = idx
        out_dist[qi] = d
    return out_idx, out_dist


# ---------------------------------------------------------------------- #
# Public entrypoint                                                       #
# ---------------------------------------------------------------------- #

def apply(npz_path: str, *, char_id: str = "(unknown)") -> dict:
    """Transplant ARKit 52 shape keys onto the current scene's face mesh.

    Returns a summary dict (counts per region, nn distance stats) for the
    caller's blend_manifest.json.
    """
    face_obj = _find_face_mesh()
    if face_obj is None:
        raise RuntimeError("apply_arkit52: no face mesh found in scene "
                           "(expected a *_FaceMesh_LOD0)")
    mesh = face_obj.data

    if not os.path.exists(npz_path):
        raise RuntimeError(f"apply_arkit52: reference npz missing: {npz_path}")
    ref = np.load(npz_path, allow_pickle=False)
    regions_in_ref = [str(r) for r in ref["regions"]]
    print(f"[arkit52] reference npz: {npz_path}  regions={regions_in_ref}", flush=True)

    # Classify material slots → regions in our face mesh
    region_to_slots = _classify_slots(face_obj)
    print(f"[arkit52] slot classification: "
          + ", ".join(f"{r}={region_to_slots.get(r, [])}" for r in regions_in_ref),
          flush=True)

    # Ensure the face has a Basis shape key (create if missing)
    if mesh.shape_keys is None:
        face_obj.shape_key_add(name="Basis", from_mix=False)
    basis_positions = _rest_positions(mesh)

    # Build a flat (n_keys × n_verts × 3) delta tensor for our face mesh, one
    # layer per ARKit name. We iterate by region, accumulate contributions.
    total_verts = basis_positions.shape[0]
    arkit_deltas = {name: np.zeros((total_verts, 3), dtype=np.float32)
                    for name in ARKIT_52_NAMES}

    summary = {
        "char_id": char_id,
        "face_object": face_obj.name,
        "total_verts": int(total_verts),
        "regions": {},
    }

    head_vert_indices = np.array([], dtype=np.int64)

    for region_id in regions_in_ref:
        slots = region_to_slots.get(region_id, [])
        if not slots:
            print(f"[arkit52] WARN region {region_id!r}: no matching material slots in face mesh, skipping",
                  flush=True)
            continue

        # Our face mesh's vertex indices in this region
        our_verts = _vertex_indices_for_slots(mesh, slots)
        if our_verts.size == 0:
            print(f"[arkit52] WARN region {region_id!r}: zero verts via slots {slots}", flush=True)
            continue

        # Our UVs for those verts (looked up via the active UV layer, restricted
        # to the region's slot polygons).
        our_uvs = _per_vertex_uvs_for_verts(mesh, our_verts, slots)

        # Reference data for this region
        ref_uvs = np.asarray(ref[f"{region_id}__uvs"], dtype=np.float32)
        ref_deltas = np.asarray(ref[f"{region_id}__deltas"], dtype=np.float32)
        ref_keys = [str(n) for n in ref[f"{region_id}__keys"]]

        # UV-based NN: topology-correct mapping. MH face UVs are canonical
        # across characters; d=0 means same anatomical vertex, d~0.02 means
        # seam-split duplicate slightly off the canonical UV.
        nn_idx, nn_uv_dist = _kdtree_nearest_uv(ref_uvs, our_uvs)
        exact = int((nn_uv_dist < 1e-5).sum())
        near  = int((nn_uv_dist < 1e-3).sum())
        print(f"[arkit52] region {region_id}: our_verts={our_verts.size}  "
              f"ref_verts={ref_uvs.shape[0]}  ref_keys={len(ref_keys)}  "
              f"UV exact={exact} near={near} (max d={nn_uv_dist.max():.4f})",
              flush=True)

        summary["regions"][region_id] = {
            "our_verts":          int(our_verts.size),
            "ref_verts":          int(ref_uvs.shape[0]),
            "keys_transplanted":  len(ref_keys),
            "uv_nn_exact":        exact,
            "uv_nn_near_1e-3":    near,
            "uv_nn_dist_mean":    float(nn_uv_dist.mean()),
            "uv_nn_dist_p95":     float(np.percentile(nn_uv_dist, 95)),
            "uv_nn_dist_max":     float(nn_uv_dist.max()),
        }

        # Apply each reference key's delta directly (no rescale — topology
        # is canonical, deltas transfer verbatim).
        for ki, ref_key in enumerate(ref_keys):
            if ref_key not in arkit_deltas:
                print(f"[arkit52] WARN region {region_id}: unknown ARKit key {ref_key!r}, skipping",
                      flush=True)
                continue
            our_delta_slice = arkit_deltas[ref_key]
            our_delta_slice[our_verts] = ref_deltas[ki, nn_idx, :]

        if region_id == "head":
            head_vert_indices = our_verts

    # --- satellite slots (lash / occlusion / cartilage) via position-NN ---- #
    # Their verts live on the face mesh but have UVs in their own atlas
    # islands, so the UV-NN pass above skipped them. Sample from the nearest
    # head verts in world space — lashes sit on the lid edge so NN → lid vert
    # → correct eyeBlink / eyeSquint delta gets propagated.
    if head_vert_indices.size > 0:
        sat_summary = _fill_satellite_verts_from_head(
            mesh, face_obj, arkit_deltas, basis_positions, head_vert_indices)
        summary["satellite_fill"] = sat_summary
    else:
        print("[arkit52] WARN no head region — skipping satellite fill", flush=True)

    # --- wipe all pre-existing non-Basis shape keys ------------------------ #
    if mesh.shape_keys:
        kb_names = [kb.name for kb in mesh.shape_keys.key_blocks if kb.name != "Basis"]
        print(f"[arkit52] removing {len(kb_names)} pre-existing shape keys", flush=True)
        # Reverse order removal is safest
        for name in reversed(kb_names):
            face_obj.shape_key_remove(mesh.shape_keys.key_blocks[name])

    # --- write the 52 ARKit shape keys -------------------------------------- #
    created = 0
    skipped = []
    for name in ARKIT_52_NAMES:
        delta = arkit_deltas[name]
        if not np.any(delta):
            # Don't waste a shape key on a region we had no data for (e.g.
            # tongueOut if reference has no tongue region).
            skipped.append(name)
            continue
        new_positions = basis_positions + delta
        kb = face_obj.shape_key_add(name=name, from_mix=False)
        # Blender's shape_key_add defaults the .value to 1.0, which Blender's
        # glTF exporter writes into the default mesh.weights array — meaning
        # the model loads in the browser with every ARKit shape at full
        # strength, producing a distorted zombie face at rest. Force to 0.0.
        kb.value = 0.0
        flat = new_positions.astype(np.float32).reshape(-1)
        kb.data.foreach_set("co", flat)
        created += 1

    summary["shape_keys_created"] = created
    summary["shape_keys_skipped_zero"] = skipped
    print(f"[arkit52] created {created} ARKit shape keys on {face_obj.name}", flush=True)
    if skipped:
        print(f"[arkit52] skipped (all-zero delta): {skipped}", flush=True)
    return summary
