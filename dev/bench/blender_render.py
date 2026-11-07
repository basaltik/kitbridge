"""Executed inside Blender; renders a source Blendkit file as the reference image."""
import json, math, os, sys
import bpy
from mathutils import Vector

def frame_direction():
    # Keep identical to c4d_render.frame_direction (which maps this through blender_to_c4d).
    return Vector((1.0, -1.0, 0.6)).normalized()

def _params():
    try: return json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
    except (ValueError, IndexError): return json.load(open(sys.argv[-1], encoding="utf-8"))

def _meshes():
    return [o for o in bpy.context.scene.objects if o.type == "MESH" and not o.hide_render]

def _setup_world(hdr_path):
    """HDRI lights the scene; film_transparent hides it from the camera (C4D does the same)."""
    if not (hdr_path and os.path.isfile(hdr_path)):
        raise RuntimeError("bench HDRI not found: %r" % hdr_path)
    w = bpy.data.worlds.new("Kitbridge benchmark world")
    bpy.context.scene.world = w; w.use_nodes = True
    n = w.node_tree.nodes; l = w.node_tree.links; n.clear()
    out = n.new("ShaderNodeOutputWorld")
    tex = n.new("ShaderNodeTexEnvironment"); tex.image = bpy.data.images.load(hdr_path, check_existing=True)
    env = n.new("ShaderNodeBackground"); env.inputs["Strength"].default_value = 1.0
    l.new(tex.outputs["Color"], env.inputs["Color"])
    l.new(env.outputs[0], out.inputs[0])

def render(params):
    scene = bpy.context.scene; objs = _meshes()
    if not objs and params.get("geometry") and os.path.isfile(params["geometry"]):
        # Material asset: the .blend has no mesh. Use the package's preview sphere, but put the
        # ORIGINAL material back on it — the FBX importer creates simplified "Name.001" copies.
        originals = {m.name: m for m in bpy.data.materials}
        if not hasattr(bpy.ops.import_scene, "fbx"):
            raise RuntimeError("FBX importer is unavailable for material preview")
        bpy.ops.import_scene.fbx(filepath=params["geometry"])
        objs = _meshes()
        for o in objs:
            for slot in o.material_slots:
                if slot.material is None:
                    continue
                stem = slot.material.name.rsplit(".", 1)[0] if slot.material.name not in originals else slot.material.name
                if stem in originals:
                    slot.material = originals[stem]
                elif len(originals) == 1:
                    slot.material = next(iter(originals.values()))
    if not objs: raise RuntimeError("source blend and package geometry contain no mesh objects")
    corners = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector((min(p.x for p in corners), min(p.y for p in corners), min(p.z for p in corners)))
    hi = Vector((max(p.x for p in corners), max(p.y for p in corners), max(p.z for p in corners)))
    target = (lo + hi) * 0.5; radius = (hi - lo).length * 0.5  # half diagonal, as in c4d_render.py
    cam_data = bpy.data.cameras.new("Kitbridge benchmark camera"); cam = bpy.data.objects.new("Kitbridge benchmark camera", cam_data)
    scene.collection.objects.link(cam); scene.camera = cam; cam_data.lens = 50; cam_data.sensor_width = 36
    cam.location = target + frame_direction() * (radius / math.tan(math.radians(39.6/2)) / 0.9)
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    _setup_world(params.get("hdr"))
    try:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 128
        scene.cycles.use_denoising = True
    except Exception as exc:
        raise RuntimeError("Cycles is unavailable: %s" % exc)
    scene.render.resolution_x = scene.render.resolution_y = 512; scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"; scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = True
    # Glass whose refraction reaches the (hidden) world counts as transparent, as Redshift does;
    # otherwise Cycles gives glass alpha 1 and every glass silhouette differs.
    scene.cycles.film_transparent_glass = True
    scene.view_settings.view_transform = "Standard"; scene.view_settings.look = "None"; scene.view_settings.exposure = 0; scene.view_settings.gamma = 1
    scene.render.filepath = params["out"]
    bpy.ops.render.render(write_still=True)
    return {"ok": True, "out": params["out"], "bbox_min": list(lo), "bbox_max": list(hi)}

if __name__ == "__main__":
    try:
        print(json.dumps(render(_params())))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise
