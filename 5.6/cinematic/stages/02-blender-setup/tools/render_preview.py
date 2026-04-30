"""
Stage 02 preview renderer — runs after import_fbx.py to produce a headless PNG
render of the rebuilt blend so pipeline changes can be visually evaluated
without a live Blender. Deliberately decoupled from the scene the .blend ships
with: we append lights + a camera, frame the character, render.

Usage:
    blender --background characters/<id>/02-blend/<id>.blend \
            --python stages/02-blender-setup/tools/render_preview.py \
            -- --char <id> --workspace "<abs>"
Output: characters/<id>/02-blend/preview.png
"""
import argparse, os, sys, math
import bpy


def _parse():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--view", default="front",
                   choices=["front", "threequarter", "profile"])
    return p.parse_args(argv)


def _all_lod0_bbox():
    """World-space bbox over visible LOD0 meshes, so we can frame the char."""
    xs, ys, zs = [], [], []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or not obj.name.endswith("_LOD0"):
            continue
        for v in obj.bound_box:
            world = obj.matrix_world @ obj.bound_box.__class__(v) if False else \
                    obj.matrix_world @ __import__("mathutils").Vector(v)
            xs.append(world.x); ys.append(world.y); zs.append(world.z)
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def _add_lights():
    # Key light (warm, camera-left, high)
    key = bpy.data.lights.new("preview_key", "AREA")
    key.energy = 400; key.size = 2.0; key.color = (1.0, 0.96, 0.92)
    ko = bpy.data.objects.new("preview_key", key)
    ko.location = (1.2, -1.6, 2.0)
    ko.rotation_euler = (math.radians(60), math.radians(20), math.radians(35))
    bpy.context.collection.objects.link(ko)
    # Fill (cool, camera-right, eye-level)
    fill = bpy.data.lights.new("preview_fill", "AREA")
    fill.energy = 150; fill.size = 3.0; fill.color = (0.85, 0.9, 1.0)
    fo = bpy.data.objects.new("preview_fill", fill)
    fo.location = (-1.6, -1.2, 1.6)
    fo.rotation_euler = (math.radians(70), math.radians(-10), math.radians(-30))
    bpy.context.collection.objects.link(fo)
    # Rim (behind, slight warm)
    rim = bpy.data.lights.new("preview_rim", "AREA")
    rim.energy = 300; rim.size = 1.5; rim.color = (1.0, 0.95, 0.85)
    ro = bpy.data.objects.new("preview_rim", rim)
    ro.location = (0.5, 1.4, 2.2)
    ro.rotation_euler = (math.radians(115), 0, math.radians(180))
    bpy.context.collection.objects.link(ro)


def _add_world_hdri_fallback():
    """No HDRI bundled — use a neutral gray world so shaders aren't pitch black."""
    w = bpy.data.worlds.get("PreviewWorld") or bpy.data.worlds.new("PreviewWorld")
    bpy.context.scene.world = w
    w.use_nodes = True
    for n in list(w.node_tree.nodes):
        w.node_tree.nodes.remove(n)
    bg = w.node_tree.nodes.new("ShaderNodeBackground")
    bg.inputs["Color"].default_value = (0.05, 0.05, 0.06, 1.0)
    bg.inputs["Strength"].default_value = 1.0
    out = w.node_tree.nodes.new("ShaderNodeOutputWorld")
    w.node_tree.links.new(bg.outputs["Background"], out.inputs["Surface"])


def _add_camera(view):
    (mn, mx) = _all_lod0_bbox()
    cx = (mn[0] + mx[0]) / 2
    # Face is near the top of the bbox; aim camera ~5-10cm below the crown
    head_h = mx[2] - 0.12
    char_w = max(mx[0] - mn[0], 0.4)

    cam = bpy.data.cameras.new("preview_cam"); cam.lens = 70
    co = bpy.data.objects.new("preview_cam", cam)
    if view == "front":
        co.location = (cx, mn[1] - 1.6, head_h)
    elif view == "threequarter":
        co.location = (cx + 0.8, mn[1] - 1.4, head_h)
    else:
        co.location = (cx + 1.6, (mn[1] + mx[1]) / 2, head_h)
    # Point at head
    target_empty = bpy.data.objects.new("preview_target", None)
    target_empty.location = (cx, (mn[1] + mx[1]) / 2, head_h)
    bpy.context.collection.objects.link(target_empty)
    bpy.context.collection.objects.link(co)
    tc = co.constraints.new("TRACK_TO")
    tc.target = target_empty; tc.track_axis = "TRACK_NEGATIVE_Z"; tc.up_axis = "UP_Y"
    bpy.context.scene.camera = co


def _configure_render(out_path):
    s = bpy.context.scene
    # Cycles — Eevee Next's BLENDED alpha doesn't render thin hair-card
    # strand masks (compact-atlas eyebrows invisible); Cycles handles them
    # correctly and SSS is also closer to the target aesthetic.
    s.render.engine = "CYCLES"
    s.render.resolution_x = 1024
    s.render.resolution_y = 1280
    s.render.resolution_percentage = 100
    s.render.image_settings.file_format = "PNG"
    s.render.filepath = out_path
    try:
        s.cycles.samples = 32
        s.cycles.use_adaptive_sampling = True
        s.cycles.adaptive_threshold = 0.05
        s.cycles.use_denoising = True
    except Exception:
        pass


def main():
    args = _parse()
    ws = os.path.abspath(args.workspace)
    out = os.path.join(ws, "characters", args.char, "02-blend",
                       f"preview_{args.view}.png")
    _add_world_hdri_fallback()
    _add_lights()
    _add_camera(args.view)
    _configure_render(out)
    bpy.ops.render.render(write_still=True)
    print(f"[preview] wrote {out}", flush=True)


if __name__ == "__main__":
    main()
