# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
#  Original code Copyright (C) BlenderKit developers (bk_maya / bk_client).
#  Adapted for Kitbridge.
#
# ##### END GPL LICENSE BLOCK #####

"""Headless Blender export & bake script for Kitbridge (milestone 2a).

Invoked as::

    blender -b --factory-startup <source.blend> --python kb_export.py -- <params.json>

Stdout protocol::

    KB_PROGRESS <0..1> <msg>
    KB_WARNING <msg>
    KB_DONE <manifest path>
    KB_ERROR <msg>
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import sys
import traceback
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import bmesh  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]

# Add plugin/kitbridge to sys.path so we can import kitbridge.core.shader_graph
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from kitbridge.core import shader_graph  # noqa: E402

SCHEMA_VERSION = 1
# Keep equal to pipeline.EXPORTER_VERSION; raise both when packages need converting again.
EXPORTER_VERSION = 7  # 3: WebP/AVIF; 4: normalized relief; 5: UDIM and light manifests; 6: pristine bakes, fine normals, SSS/translucency; 7: displacement method
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ── Stdout Protocol Helpers ──────────────────────────────────────────────────


def emit_progress(fraction: float, message: str = "") -> None:
    clamped = max(0.0, min(1.0, float(fraction)))
    sys.stdout.write(f"KB_PROGRESS {clamped:.3f} {message}\n")
    sys.stdout.flush()


def emit_warning(message: str) -> None:
    clean = " ".join(str(message).splitlines()).strip()
    if clean:
        sys.stdout.write(f"KB_WARNING {clean}\n")
        sys.stdout.flush()


def emit_done(manifest_path: str) -> None:
    sys.stdout.write(f"KB_DONE {manifest_path}\n")
    sys.stdout.flush()


def emit_error(message: str) -> None:
    clean = " ".join(str(message).splitlines()).strip()
    sys.stdout.write(f"KB_ERROR {clean}\n")
    sys.stdout.flush()


def log_info(message: str) -> None:
    sys.stdout.write(f"[kb_export] {message}\n")
    sys.stdout.flush()


def _safe_filename(name: str, fallback: str = "unnamed") -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (name or "").strip()).strip("._-")
    return cleaned or fallback


# ── Step 1: Scene Preparation, Modifiers, Instances & Rig Flattening ─────────


def _unhide_collections(layer_col: Any) -> None:
    with contextlib.suppress(Exception):
        layer_col.exclude = False
        layer_col.hide_viewport = False
    col = getattr(layer_col, "collection", None)
    if col is not None:
        with contextlib.suppress(Exception):
            col.hide_viewport = False
            col.hide_render = False
            col.hide_select = False
    for child in getattr(layer_col, "children", ()):
        _unhide_collections(child)


def _find_boolean_cutters_and_helpers(scene: Any) -> Set[str]:
    """Identify boolean modifier target objects and wire/bounds helper widgets."""
    cutters: Set[str] = set()
    for obj in scene.objects:
        for mod in getattr(obj, "modifiers", ()):
            if getattr(mod, "type", "") == "BOOLEAN":
                target = getattr(mod, "object", None)
                if target is not None:
                    cutters.add(target.name)
                col = getattr(mod, "collection", None)
                if col is not None:
                    for c_obj in col.all_objects:
                        cutters.add(c_obj.name)
        if getattr(obj, "hide_render", False) and getattr(obj, "display_type", "") in (
            "WIRE",
            "BOUNDS",
        ):
            cutters.add(obj.name)
    return cutters


def _ensure_geonodes_realize_instances(obj: Any) -> None:
    """Insert a Realize Instances node before Group Output in Geometry Nodes modifiers."""
    for mod in getattr(obj, "modifiers", ()):
        if getattr(mod, "type", "") != "NODES":
            continue
        node_group = getattr(mod, "node_group", None)
        if node_group is None or getattr(node_group, "library", None) is not None:
            continue
        try:
            nodes = node_group.nodes
            links = node_group.links
            for node in list(nodes):
                if node.bl_idname != "NodeGroupOutput":
                    continue
                geom_in = node.inputs[0] if len(node.inputs) > 0 else None
                if geom_in is None or not geom_in.is_linked:
                    continue
                src_link = geom_in.links[0]
                if src_link.from_node.bl_idname == "GeometryNodeRealizeInstances":
                    continue
                realize = nodes.new("GeometryNodeRealizeInstances")
                from_sock = src_link.from_socket
                links.remove(src_link)
                links.new(from_sock, realize.inputs["Geometry"])
                links.new(realize.outputs["Geometry"], geom_in)
        except Exception as exc:
            log_info(f"geonodes realize skipped on {obj.name!r}: {exc}")


def _realize_collection_instances(scene: Any, view_layer: Any) -> None:
    """Make collection-instancing Empty objects into real objects."""
    instancers = [
        o
        for o in list(scene.objects)
        if o.type == "EMPTY"
        and getattr(o, "instance_type", "NONE") == "COLLECTION"
        and getattr(o, "instance_collection", None) is not None
    ]
    if not instancers:
        return
    with contextlib.suppress(Exception):
        bpy.ops.object.mode_set(mode="OBJECT")
    for obj in instancers:
        try:
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            view_layer.objects.active = obj
            bpy.ops.object.duplicates_make_real(use_base_parent=True, use_hierarchy=True)
            obj.instance_type = "NONE"
        except Exception as exc:
            log_info(f"duplicates_make_real failed on {obj.name!r}: {exc}")


def _apply_modifiers_and_convert_meshes(scene: Any, view_layer: Any, cutters: Set[str]) -> None:
    """Convert convertible geometry objects to evaluated meshes and validate CustomData."""
    convertible_types = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
    candidates = [
        o
        for o in list(scene.objects)
        if o.type in convertible_types and o.name not in cutters
    ]
    if not candidates:
        return

    with contextlib.suppress(Exception):
        bpy.ops.object.mode_set(mode="OBJECT")

    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in candidates:
        try:
            _ensure_geonodes_realize_instances(obj)
            if obj.data is not None and getattr(obj.data, "users", 1) > 1:
                obj.data = obj.data.copy()
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            view_layer.objects.active = obj
            bpy.ops.object.convert(target="MESH")
        except Exception as exc:
            log_info(f"object.convert failed on {obj.name!r} ({exc}); trying new_from_object")
            with contextlib.suppress(Exception):
                eval_obj = obj.evaluated_get(depsgraph)
                new_mesh = bpy.data.meshes.new_from_object(eval_obj)
                if new_mesh is not None and obj.type == "MESH":
                    obj.modifiers.clear()
                    obj.data = new_mesh

        if obj.type == "MESH" and obj.data is not None:
            with contextlib.suppress(Exception):
                obj.data.validate(verbose=False)
                obj.data.update()

    with contextlib.suppress(Exception):
        bpy.ops.object.select_all(action="DESELECT")


def _flatten_rig_to_single_root(group_name: str = "") -> int:
    """Detach armature-parented objects preserving world transforms and remove armatures.

    Adapted from ``bk_client/client/tools/export_usd.py::_flatten_rig_to_single_root``.
    """
    scene = bpy.context.scene
    armatures = [o for o in list(bpy.data.objects) if o.type == "ARMATURE"]
    if not armatures:
        return 0

    objs = [o for o in list(scene.objects) if o.type != "ARMATURE"]
    worlds = {o.name: o.matrix_world.copy() for o in objs}

    for o in objs:
        if o.parent in armatures:
            o.parent = None
            o.matrix_world = worlds[o.name]

    removed = 0
    for arm in armatures:
        with contextlib.suppress(Exception):
            bpy.data.objects.remove(arm, do_unlink=True)
            removed += 1

    name = group_name or os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "Model"
    group = bpy.data.objects.new(name, None)
    scene.collection.objects.link(group)
    for o in list(scene.objects):
        if o is group or o.parent is not None:
            continue
        world = o.matrix_world.copy()
        o.parent = group
        o.matrix_parent_inverse = group.matrix_world.inverted()
        o.matrix_world = world

    log_info(f"rig-flatten: removed {removed} armature(s) under {name!r}")
    return removed


def _prune_non_asset_objects(scene: Any, cutters: Set[str]) -> bool:
    """Remove cutters, cameras, and non-geometry helpers; return True if asset is only lights."""
    has_meshes = any(
        o.type == "MESH" and o.name not in cutters and o.data is not None and len(o.data.polygons) > 0
        for o in scene.objects
    )
    has_lights = any(o.type == "LIGHT" for o in scene.objects)
    only_lights = (not has_meshes) and has_lights

    to_remove: List[Any] = []
    for obj in list(scene.objects):
        if obj.name in cutters:
            to_remove.append(obj)
            continue
        if obj.type in ("CAMERA", "SPEAKER", "LIGHT_PROBE", "LATTICE", "ARMATURE"):
            to_remove.append(obj)
            continue
        if obj.type == "MESH" and (obj.data is None or len(obj.data.polygons) == 0):
            to_remove.append(obj)

    for obj in to_remove:
        with contextlib.suppress(Exception):
            bpy.data.objects.remove(obj, do_unlink=True)

    return only_lights


def _select_asset_material(asset_name: str = "", asset_id: str = "") -> Optional[Any]:
    """Locate the primary material in a material asset ``.blend`` file.

    Adapted from ``bk_client/client/tools/export_usd.py::_prepare_material_asset``.
    """
    mats = [m for m in bpy.data.materials if not m.name.startswith("Dots Stroke")]
    if not mats:
        return None

    for m in mats:
        if getattr(m, "asset_data", None) is not None:
            return m
    if asset_id:
        for m in mats:
            for attr in ("blendkit", "blenderkit"):
                bk = getattr(m, attr, None)
                if bk is not None and (
                    getattr(bk, "id", "") == asset_id
                    or getattr(bk, "asset_base_id", "") == asset_id
                ):
                    return m
    if asset_name:
        for m in mats:
            if m.name.lower() == asset_name.lower():
                return m
    for m in mats:
        if m.use_nodes and m.node_tree is not None:
            return m
    return mats[0]


def _prepare_material_sphere_scene(asset_name: str = "", asset_id: str = "") -> Any:
    """Replace scene geometry with a clean 1 m UV sphere carrying the asset material."""
    chosen = _select_asset_material(asset_name=asset_name, asset_id=asset_id)
    if chosen is None:
        raise RuntimeError("No material datablock found in material asset .blend file.")

    scene = bpy.context.scene
    for obj in list(scene.objects):
        with contextlib.suppress(Exception):
            bpy.data.objects.remove(obj, do_unlink=True)

    mesh = bpy.data.meshes.new("PreviewSphereMesh")
    bm = bmesh.new()
    try:
        try:
            bmesh.ops.create_uvsphere(
                bm,
                u_segments=64,
                v_segments=32,
                radius=0.5,
                calc_uvs=True,
            )
        except TypeError:
            bmesh.ops.create_uvsphere(
                bm,
                u_segments=64,
                v_segments=32,
                diameter=0.5,
                calc_uvs=True,
            )
        bm.to_mesh(mesh)
    finally:
        bm.free()

    if not mesh.uv_layers:
        mesh.uv_layers.new(name="UVMap")
    else:
        mesh.uv_layers[0].name = "UVMap"
        mesh.uv_layers.active = mesh.uv_layers[0]
        mesh.uv_layers[0].active_render = True

    for poly in mesh.polygons:
        poly.use_smooth = True
    mesh.materials.append(chosen)
    mesh.validate(verbose=False)
    mesh.update()

    sphere_obj = bpy.data.objects.new("PreviewSphere", mesh)
    scene.collection.objects.link(sphere_obj)
    bpy.context.view_layer.objects.active = sphere_obj
    sphere_obj.select_set(True)
    log_info(f"material-sphere: created 1m UV sphere for material {chosen.name!r}")
    return sphere_obj


def prepare_scene(asset_type: str, asset_name: str = "", asset_id: str = "") -> bool:
    """Prepare the scene for export; return ``only_lights`` flag."""
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    _unhide_collections(view_layer.layer_collection)

    if asset_type.lower() == "material":
        _prepare_material_sphere_scene(asset_name=asset_name, asset_id=asset_id)
        return False

    cutters = _find_boolean_cutters_and_helpers(scene)
    for obj in list(scene.objects):
        if obj.name in cutters:
            continue
        with contextlib.suppress(Exception):
            obj.hide_set(False)
        obj.hide_viewport = False
        obj.hide_render = False
        obj.hide_select = False

    _realize_collection_instances(scene, view_layer)
    _apply_modifiers_and_convert_meshes(scene, view_layer, cutters)
    _flatten_rig_to_single_root(asset_name)
    only_lights = _prune_non_asset_objects(scene, cutters)
    return only_lights


# ── Step 2: Unpack & Downscale Textures ──────────────────────────────────────


def _ext_for_image(image: Any, source_path: str) -> str:
    _, ext = os.path.splitext(source_path)
    if ext and len(ext) <= 6:
        return ext.lower()
    fmt = str(getattr(image, "file_format", "PNG") or "PNG").upper()
    fmt_map = {
        "JPEG": ".jpg",
        "JPG": ".jpg",
        "PNG": ".png",
        "TARGA": ".tga",
        "TARGA_RAW": ".tga",
        "TIFF": ".tif",
        "OPEN_EXR": ".exr",
        "OPEN_EXR_MULTILAYER": ".exr",
        "HDR": ".hdr",
        "WEBP": ".webp",
        "BMP": ".bmp",
    }
    return fmt_map.get(fmt, ".png")


# Formats Blender reads but Redshift / Arnold / C4D bitmaps don't: Redshift shows its magenta
# "invalid texture" colour (Blendkit started shipping WebP in 2026).
_CONVERT_TO_PNG = (".webp", ".avif")


def _convert_to_png(image: Any, path: str, tex_dir: str, claimed: Set[str]) -> str:
    """Re-save *image* (already loaded from *path*) as PNG next to it; return the new path."""
    stem = os.path.splitext(os.path.basename(path))[0]
    claimed.discard(os.path.basename(path).lower())
    png_path = _unique_texture_path(tex_dir, stem + ".png", claimed)
    _ = image.pixels[0]  # background Blender loads pixels lazily; save() fails on an unloaded image
    image.file_format = "PNG"
    image.filepath_raw = png_path
    image.save()  # raw pixels, no view transform: colour and data maps stay exact
    if not (os.path.isfile(png_path) and os.path.getsize(png_path) > 0):
        raise RuntimeError("PNG was not written")
    with contextlib.suppress(OSError):
        os.remove(path)
    log_info(f"texture: converted {os.path.basename(path)} -> {os.path.basename(png_path)}")
    return png_path


def _unique_texture_path(tex_dir: str, desired_name: str, claimed: Set[str]) -> str:
    stem, ext = os.path.splitext(desired_name)
    safe_stem = _safe_filename(stem, "tex")
    safe_ext = ext.lower() if ext else ".png"
    candidate = f"{safe_stem}{safe_ext}"
    idx = 0
    while candidate.lower() in claimed:
        candidate = f"{safe_stem}_{idx:03d}{safe_ext}"
        idx += 1
    claimed.add(candidate.lower())
    return os.path.join(tex_dir, candidate)


def _udim_tile_source_path(image: Any, tile: Any, raw_fp: str) -> str:
    """Resolve one tiled image source without changing Blender's active tile."""
    tile_fp = str(getattr(tile, "filepath", "") or "")
    template = tile_fp or raw_fp
    path = template.replace("<UDIM>", str(int(tile.number)))
    return os.path.normpath(bpy.path.abspath(path, library=image.library))


def _collect_udim_image(
    image: Any,
    tex_dir: str,
    claimed: Set[str],
    max_texture_size: int,
) -> None:
    raw_fp = str(getattr(image, "filepath", "") or "")
    src_hint = raw_fp or image.name
    base = os.path.basename(src_hint.replace("\\", "/"))
    if not os.path.splitext(base)[1] or not re.search(r"\d{4}(?=\.[^.]+$)", base):
        base = os.path.basename(str(image.name).replace("\\", "/"))
    template = shader_graph.udim_texture_template(base)
    stem, ext = os.path.splitext(template)
    if ext.lower() in _CONVERT_TO_PNG:
        template = stem + ".png"
    manifest_template = shader_graph.udim_texture_template(template)
    stem, ext = os.path.splitext(manifest_template)
    if ext.lower() in _CONVERT_TO_PNG:
        manifest_template = stem + ".png"
        with contextlib.suppress(Exception):
            image.file_format = "PNG"
    for tile in list(getattr(image, "tiles", ())):
        tile_num = int(tile.number)
        target_abs = os.path.join(
            tex_dir, shader_graph.udim_tile_filename(manifest_template, tile_num)
        )
        tile_image = None
        try:
            image.tiles.active = tile
            width, height = image.size
            pixels = list(image.pixels)
            tile_image = bpy.data.images.new(
                f"{image.name}_tile_{tile_num}",
                width=width,
                height=height,
                alpha=True,
                float_buffer=bool(getattr(image, "is_float", False)),
            )
            tile_image.file_format = "PNG" if ext.lower() == ".png" else image.file_format
            tile_image.pixels.foreach_set(pixels)
            tile_image.filepath_raw = target_abs
            tile_image.save()
            bpy.data.images.remove(tile_image)
            tile_image = None
        except Exception as exc:
            if tile_image is not None:
                with contextlib.suppress(Exception):
                    bpy.data.images.remove(tile_image)
            emit_warning(f"UDIM tile {tile_num} for '{image.name}' could not be written ({exc}).")
            continue
        if not os.path.isfile(target_abs) or os.path.getsize(target_abs) <= 0:
            emit_warning(f"UDIM tile {tile_num} for '{image.name}' was not written.")
            continue
        claimed.add(os.path.basename(target_abs).lower())
        if max_texture_size > 0:
            with contextlib.suppress(Exception):
                image.tiles.active = tile
                w, h = image.size
                longest = max(int(w), int(h))
                if longest > max_texture_size:
                    image.scale(
                        max(1, int(round(w * max_texture_size / longest))),
                        max(1, int(round(h * max_texture_size / longest))),
                    )
                    scaled_pixels = list(image.pixels)
                    scaled = bpy.data.images.new(
                        f"{image.name}_scaled_{tile_num}",
                        width=image.size[0],
                        height=image.size[1],
                        alpha=True,
                        float_buffer=bool(getattr(image, "is_float", False)),
                    )
                    scaled.file_format = "PNG" if ext.lower() == ".png" else image.file_format
                    scaled.pixels.foreach_set(scaled_pixels)
                    scaled.filepath_raw = target_abs
                    scaled.save()
                    bpy.data.images.remove(scaled)
        log_info(f"texture: collected UDIM {tile_num} -> {os.path.basename(target_abs)}")
    image["kitbridge_rel_path"] = f"textures/{os.path.basename(manifest_template)}"
    image["kitbridge_udim"] = True
    image["kitbridge_is_udim"] = True


def unpack_and_collect_textures(output_dir: str, max_texture_size: int = 0) -> None:
    """Unpack/copy all scene images into ``<output_dir>/textures/`` and downscale if needed.

    Adapted from ``bk_client/client/tools/export_usd.py::unpack_textures`` and
    ``bk_client/client/tools/export_glb.py::downscale_images``.
    """
    tex_dir = os.path.join(output_dir, "textures")
    os.makedirs(tex_dir, exist_ok=True)
    bpy.data.use_autopack = False

    claimed: Set[str] = set()
    for image in list(bpy.data.images):
        if image.name in ("Render Result", "Viewer Node"):
            continue
        if getattr(image, "type", "IMAGE") not in ("IMAGE", "UV_TEST"):
            continue

        raw_fp = str(getattr(image, "filepath", "") or "")
        is_udim = (
            getattr(image, "source", "") == "TILED"
            or "<UDIM>" in raw_fp
            or len(getattr(image, "tiles", ())) > 1
        )
        if is_udim:
            _collect_udim_image(image, tex_dir, claimed, max_texture_size)
            continue

        is_packed = len(image.packed_files) > 0
        if is_packed:
            src_hint = image.packed_files[0].filepath or raw_fp or image.name
        else:
            src_hint = raw_fp or image.name

        base = os.path.basename(src_hint.replace("\\", "/"))
        if not base or "." not in base:
            ext = _ext_for_image(image, src_hint)
            base = f"{_safe_filename(image.name.split('.')[0], 'texture')}{ext}"

        target_abs = _unique_texture_path(tex_dir, base, claimed)
        written = False

        # 1) Packed image data
        if len(image.packed_files) > 0:
            try:
                _ = image.pixels[0]
                image.save(filepath=target_abs, save_copy=True)
                written = os.path.isfile(target_abs) and os.path.getsize(target_abs) > 0
            except Exception as exc:
                log_info(f"packed image save failed for {image.name!r}: {exc}")

        # 2) External / relative image on disk
        if not written and raw_fp:
            with contextlib.suppress(Exception):
                src_abs = os.path.normpath(bpy.path.abspath(raw_fp, library=image.library))
                if os.path.isfile(src_abs):
                    if os.path.abspath(src_abs) != os.path.abspath(target_abs):
                        shutil.copy2(src_abs, target_abs)
                    written = True

        # 3) Fallback: save in-memory image buffer
        if not written:
            with contextlib.suppress(Exception):
                w, h = image.size
                if w > 0 and h > 0:
                    image.filepath_raw = target_abs
                    image.save()
                    written = os.path.isfile(target_abs) and os.path.getsize(target_abs) > 0

        if not written:
            log_info(f"texture: skipping unwritten/empty image {image.name!r}")
            continue

        if not is_packed:
            image.filepath = target_abs
            image.filepath_raw = target_abs
            with contextlib.suppress(Exception):
                image.reload()

        if os.path.splitext(target_abs)[1].lower() in _CONVERT_TO_PNG:
            try:
                target_abs = _convert_to_png(image, target_abs, tex_dir, claimed)
                image.filepath = target_abs
            except Exception as exc:
                emit_warning(f"Texture '{image.name}' is {os.path.splitext(target_abs)[1]} and could "
                             f"not be converted to PNG ({exc}); renderers may show it magenta.")

        # Respect max_texture_size by downscaling copies on disk
        if max_texture_size > 0:
            try:
                w, h = image.size
                longest = max(int(w), int(h))
                if longest > max_texture_size and w > 0 and h > 0:
                    nw = max(1, int(round(w * max_texture_size / longest)))
                    nh = max(1, int(round(h * max_texture_size / longest)))
                    image.scale(nw, nh)
                    if is_packed:
                        image.save(filepath=target_abs, save_copy=True)
                    else:
                        image.filepath_raw = target_abs
                        image.save()
                    log_info(f"downscale: {os.path.basename(target_abs)} {w}x{h} -> {nw}x{nh}")
            except Exception as exc:
                log_info(f"downscale failed for {image.name!r}: {exc}")

        rel_tex = f"textures/{os.path.basename(target_abs)}"
        image["kitbridge_rel_path"] = rel_tex


# ── Step 3: Flatten Node Groups & Adapt to shader_graph ──────────────────────


def _ungroup_all(node_tree: Any, owner: Any) -> int:
    """Recursively expand all ``ShaderNodeGroup`` nodes in *node_tree*.

    Adapted from ``bk_client/client/tools/export_usd.py::_ungroup_all``.
    """
    expanded = 0
    safety = 32
    while safety > 0:
        groups = [
            n
            for n in node_tree.nodes
            if n.bl_idname == "ShaderNodeGroup" and getattr(n, "node_tree", None) is not None
        ]
        if not groups:
            break
        safety -= 1
        progressed = False
        for gn in groups:
            if _manual_inline(node_tree, gn):
                expanded += 1
                progressed = True
                continue
            try:
                with bpy.context.temp_override(
                    window=bpy.context.window,
                    area=None,
                    region=None,
                    material=owner if isinstance(owner, bpy.types.Material) else None,
                    edit_tree=node_tree,
                    active_node=gn,
                    selected_nodes=[gn],
                ):
                    for n in node_tree.nodes:
                        n.select = False
                    gn.select = True
                    node_tree.nodes.active = gn
                    bpy.ops.node.group_ungroup()
                expanded += 1
                progressed = True
            except Exception as exc:
                log_info(f"ungroup failed for {gn.name!r}: {exc}")
        if not progressed:
            break
    return expanded


def _find_matching_socket(sockets: Any, ref_sock: Any, ref_collection: Any) -> Optional[Any]:
    """Match a group boundary socket by index/name so duplicate socket names work cleanly."""
    try:
        idx = list(ref_collection).index(ref_sock)
        if 0 <= idx < len(sockets) and sockets[idx].name == ref_sock.name:
            return sockets[idx]
    except (ValueError, IndexError):
        pass
    sock = sockets.get(ref_sock.name)
    if sock is not None:
        return sock
    try:
        idx = list(ref_collection).index(ref_sock)
        if 0 <= idx < len(sockets):
            return sockets[idx]
    except (ValueError, IndexError):
        pass
    return None


def _copy_color_ramp(src: Any, dst: Any) -> None:
    """ColorRamp data is a nested struct: plain property copying leaves the default black→white."""
    for attr in ("interpolation", "color_mode", "hue_interpolation"):
        with contextlib.suppress(Exception):
            setattr(dst, attr, getattr(src, attr))
    while len(dst.elements) < len(src.elements):
        dst.elements.new(1.0)
    while len(dst.elements) > len(src.elements):
        dst.elements.remove(dst.elements[-1])
    for d, s in zip(dst.elements, src.elements):
        d.position = s.position
        d.color = tuple(s.color)


def _copy_curve_mapping(src: Any, dst: Any) -> None:
    """RGB/Float/Vector Curves: copy every curve's points (also nested, also lost otherwise)."""
    for attr in ("clip_min_x", "clip_min_y", "clip_max_x", "clip_max_y", "use_clip", "extend"):
        with contextlib.suppress(Exception):
            setattr(dst, attr, getattr(src, attr))
    for sc, dc in zip(src.curves, dst.curves):
        while len(dc.points) < len(sc.points):
            dc.points.new(0.5, 0.5)
        while len(dc.points) > len(sc.points):
            dc.points.remove(dc.points[-1])
        for dp, sp in zip(dc.points, sc.points):
            dp.location = tuple(sp.location)
            with contextlib.suppress(Exception):
                dp.handle_type = sp.handle_type
    with contextlib.suppress(Exception):
        dst.update()


def _manual_inline(parent_tree: Any, group_node: Any) -> bool:
    """Inline a ``ShaderNodeGroup`` into *parent_tree* in pure Python.

    Adapted from ``bk_client/client/tools/export_usd.py::_manual_inline``.
    """
    try:
        inner = group_node.node_tree
        if inner is None:
            return False

        copies: Dict[Any, Any] = {}
        for n in inner.nodes:
            if n.bl_idname in ("NodeGroupInput", "NodeGroupOutput"):
                continue
            new_node = parent_tree.nodes.new(n.bl_idname)
            for attr in ("label", "location", "width", "height"):
                with contextlib.suppress(Exception):
                    setattr(new_node, attr, getattr(n, attr))
            for prop in n.bl_rna.properties:
                if prop.is_readonly or prop.identifier in ("rna_type", "name", "select"):
                    continue
                with contextlib.suppress(Exception):
                    setattr(new_node, prop.identifier, getattr(n, prop.identifier))
            if getattr(n, "color_ramp", None) is not None:
                _copy_color_ramp(n.color_ramp, new_node.color_ramp)
            if getattr(n, "mapping", None) is not None and hasattr(n.mapping, "curves"):
                _copy_curve_mapping(n.mapping, new_node.mapping)
            # Copy unconnected socket defaults
            for idx, inp in enumerate(n.inputs):
                if idx < len(new_node.inputs) and not inp.is_linked:
                    with contextlib.suppress(Exception):
                        new_node.inputs[idx].default_value = inp.default_value
            copies[n] = new_node

        for lnk in inner.links:
            fn, fs = lnk.from_node, lnk.from_socket
            tn, ts = lnk.to_node, lnk.to_socket

            if fn.bl_idname == "NodeGroupInput" and tn.bl_idname == "NodeGroupOutput":
                ext_in = _find_matching_socket(group_node.inputs, fs, fn.outputs)
                ext_out = _find_matching_socket(group_node.outputs, ts, tn.inputs)
                if ext_in is not None and ext_out is not None and ext_in.is_linked:
                    src_sock = ext_in.links[0].from_socket
                    for ext_link in list(ext_out.links):
                        parent_tree.links.new(src_sock, ext_link.to_socket)
                continue

            if fn.bl_idname == "NodeGroupInput":
                dst_node = copies.get(tn)
                if dst_node is None:
                    continue
                dst_sock = _find_matching_socket(dst_node.inputs, ts, tn.inputs)
                ext_in = _find_matching_socket(group_node.inputs, fs, fn.outputs)
                if dst_sock is None or ext_in is None:
                    continue
                if ext_in.is_linked:
                    parent_tree.links.new(ext_in.links[0].from_socket, dst_sock)
                else:
                    with contextlib.suppress(Exception):
                        dst_sock.default_value = ext_in.default_value

            elif tn.bl_idname == "NodeGroupOutput":
                src_node = copies.get(fn)
                if src_node is None:
                    continue
                src_sock = _find_matching_socket(src_node.outputs, fs, fn.outputs)
                ext_out = _find_matching_socket(group_node.outputs, ts, tn.inputs)
                if src_sock is None or ext_out is None:
                    continue
                for ext_link in list(ext_out.links):
                    parent_tree.links.new(src_sock, ext_link.to_socket)

            else:
                src_node = copies.get(fn)
                dst_node = copies.get(tn)
                if src_node is None or dst_node is None:
                    continue
                src_sock = _find_matching_socket(src_node.outputs, fs, fn.outputs)
                dst_sock = _find_matching_socket(dst_node.inputs, ts, tn.inputs)
                if src_sock is not None and dst_sock is not None:
                    parent_tree.links.new(src_sock, dst_sock)

        parent_tree.nodes.remove(group_node)
        return True
    except Exception as exc:
        log_info(f"manual inline failed for {group_node.name!r}: {exc}")
        return False


def _find_active_material_output(node_tree: Any) -> Optional[Any]:
    for n in node_tree.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial" and getattr(n, "is_active_output", False):
            return n
    for n in node_tree.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial":
            return n
    return None


def _build_duck_graph_from_bpy(output_node: Any, mat: Any = None) -> shader_graph.GraphNode:
    """Convert a Blender shader node tree rooted at *output_node* into :class:`GraphNode`s."""
    memo: Dict[Any, shader_graph.GraphNode] = {}

    def _convert(bnode: Any) -> shader_graph.GraphNode:
        if bnode in memo:
            return memo[bnode]
        props: Dict[str, Any] = {}
        ntype = shader_graph.canonical_node_type(bnode)

        if ntype == "TEX_IMAGE":
            img = getattr(bnode, "image", None)
            if img is not None:
                props["image_path"] = str(img.get("kitbridge_rel_path") or "")
                props["is_udim"] = bool(img.get("kitbridge_is_udim", False))
            props["projection"] = str(getattr(bnode, "projection", "FLAT") or "FLAT")
        elif ntype == "UVMAP":
            props["uv_map"] = str(getattr(bnode, "uv_map", "") or "")
        elif ntype in ("SEPARATE_COLOR", "SEPARATE_RGB"):
            props["mode"] = str(getattr(bnode, "mode", "RGB") or "RGB")
        elif ntype == "NORMAL_MAP":
            props["space"] = str(getattr(bnode, "space", "TANGENT") or "TANGENT")
        elif ntype == "BUMP":
            props["invert"] = bool(getattr(bnode, "invert", False))
        elif ntype == "MATH":
            props["operation"] = str(getattr(bnode, "operation", "ADD") or "ADD")
            props["use_clamp"] = bool(getattr(bnode, "use_clamp", False))
        elif ntype in ("MIX", "MIX_RGB"):
            props["data_type"] = str(getattr(bnode, "data_type", "RGBA") or "RGBA")
            props["blend_type"] = str(getattr(bnode, "blend_type", "MIX") or "MIX")
            props["clamp_factor"] = bool(getattr(bnode, "clamp_factor", True))
        elif ntype == "VALUE":
            if len(bnode.outputs) > 0:
                with contextlib.suppress(Exception):
                    props["value"] = float(bnode.outputs[0].default_value)
        elif ntype == "RGB":
            if len(bnode.outputs) > 0:
                with contextlib.suppress(Exception):
                    props["color"] = [float(c) for c in list(bnode.outputs[0].default_value)[:3]]

        gnode = shader_graph.GraphNode(
            node_type=bnode.bl_idname,
            name=bnode.name,
            properties=props,
        )
        memo[bnode] = gnode

        for idx, inp in enumerate(getattr(bnode, "inputs", ())):
            if ntype == "MIX" and not getattr(inp, "enabled", True) and not inp.is_linked:
                continue
            dv: Any = None
            with contextlib.suppress(Exception):
                raw_dv = inp.default_value
                if isinstance(raw_dv, (int, float, str, bool)):
                    dv = raw_dv
                else:
                    dv = [float(x) for x in list(raw_dv)]
            gsock = shader_graph.GraphSocket(name=inp.name, default_value=dv)
            if inp.is_linked and len(inp.links) > 0:
                lnk = inp.links[0]
                if getattr(lnk, "is_valid", True) and lnk.from_node is not None:
                    up_gnode = _convert(lnk.from_node)
                    from_sname = str(getattr(lnk.from_socket, "name", "Color") or "Color")
                    glink = shader_graph.GraphLink(from_node=up_gnode, from_socket=from_sname)
                    gsock.link = glink
                    gsock.links = [glink]
            if inp.name not in gnode.inputs:
                gnode.inputs[inp.name] = gsock
            else:
                ident = str(getattr(inp, "identifier", "") or "")
                key = ident if (ident and ident not in gnode.inputs) else f"{inp.name}_{idx:03d}"
                gnode.inputs[key] = gsock

        return gnode

    root = _convert(output_node)
    if mat is not None:
        raw_bm = str(getattr(mat, "blend_method", "") or "")
        if raw_bm:
            root.properties["blend_method"] = raw_bm
    return root


# ── Step 4: Cycles Baking (`EMIT` & `NORMAL`) ────────────────────────────────


def _configure_cycles_for_baking(scene: Any, bake_resolution: int) -> Dict[str, Any]:
    """Configure Cycles (GPU if available, else CPU) and save original scene settings."""
    saved: Dict[str, Any] = {
        "engine": scene.render.engine,
        "view_transform": scene.view_settings.view_transform,
        "look": scene.view_settings.look,
        "exposure": scene.view_settings.exposure,
        "gamma": scene.view_settings.gamma,
    }
    scene.render.engine = "CYCLES"
    with contextlib.suppress(Exception):
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0

    gpu_enabled = False
    with contextlib.suppress(Exception):
        cycles_addon = bpy.context.preferences.addons.get("cycles")
        if cycles_addon is not None:
            cprefs = cycles_addon.preferences
            for dev_type in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
                try:
                    cprefs.compute_device_type = dev_type
                    cprefs.get_devices()
                    non_cpu = [d for d in cprefs.devices if getattr(d, "type", "CPU") != "CPU"]
                    if non_cpu:
                        for d in cprefs.devices:
                            d.use = True
                        scene.cycles.device = "GPU"
                        gpu_enabled = True
                        log_info(f"cycles: using GPU ({dev_type})")
                        break
                except Exception:
                    continue
    if not gpu_enabled:
        with contextlib.suppress(Exception):
            scene.cycles.device = "CPU"
        log_info("cycles: using CPU")

    with contextlib.suppress(Exception):
        scene.cycles.samples = 16
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.use_denoising = False

    bake_cfg = scene.render.bake
    with contextlib.suppress(Exception):
        bake_cfg.use_clear = True
        bake_cfg.use_selected_to_active = False
        bake_cfg.margin = max(2, min(16, bake_resolution // 256))
        bake_cfg.target = "IMAGE_TEXTURES"

    return saved


def _restore_scene_settings(scene: Any, saved: Dict[str, Any]) -> None:
    for key in ("view_transform", "look", "exposure", "gamma"):
        if key in saved:
            with contextlib.suppress(Exception):
                setattr(scene.view_settings, key, saved[key])
    if "engine" in saved:
        with contextlib.suppress(Exception):
            scene.render.engine = saved["engine"]


def _mesh_world_area(obj: Any) -> float:
    if obj.type != "MESH" or obj.data is None:
        return 0.0
    sx, sy, sz = obj.matrix_world.to_scale()
    scale_factor = abs(float(sx * sy * sz)) ** (2.0 / 3.0) or 1.0
    return sum(float(p.area) for p in obj.data.polygons) * scale_factor


def _objects_using_material(scene: Any, mat: Any) -> List[Any]:
    users: List[Any] = []
    for obj in scene.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        if any(slot.material == mat for slot in obj.material_slots):
            users.append(obj)
    return users


def _has_usable_uv_layer(obj: Any) -> bool:
    mesh = obj.data
    if mesh is None or len(mesh.uv_layers) == 0 or len(mesh.polygons) == 0:
        return False
    layer = mesh.uv_layers.active or mesh.uv_layers[0]
    data = layer.data
    if len(data) == 0:
        return False
    for poly in mesh.polygons:
        start = poly.loop_start
        n = poly.loop_total
        if n < 3:
            continue
        u0, v0 = data[start].uv
        area = 0.0
        for i in range(1, n - 1):
            u1, v1 = data[start + i].uv
            u2, v2 = data[start + i + 1].uv
            area += abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0))
        if area > 1e-10:
            return True
    return False


def _ensure_uvs_on_material_objects(mat: Any, objs: List[Any]) -> Tuple[str, Optional[str]]:
    """Ensure representative and peer objects using *mat* have a valid UV map.

    If the representative object has no usable UV map, runs Smart UV Project on
    every object using *mat* and returns a warning string.
    """
    if not objs:
        return "UVMap", None

    rep = max(objs, key=_mesh_world_area)
    if _has_usable_uv_layer(rep):
        active_uv = rep.data.uv_layers.active or rep.data.uv_layers[0]
        return active_uv.name, None

    warn_msg = (
        f"Representative object '{rep.name}' for material '{mat.name}' had no usable UV map; "
        f"generated 'UVMap' via Smart UV Project."
    )
    emit_warning(warn_msg)

    view_layer = bpy.context.view_layer
    for obj in objs:
        try:
            mesh = obj.data
            if mesh is None:
                continue
            if "UVMap" not in mesh.uv_layers:
                uv_layer = mesh.uv_layers.new(name="UVMap")
            else:
                uv_layer = mesh.uv_layers["UVMap"]
            mesh.uv_layers.active = uv_layer
            uv_layer.active_render = True

            if bpy.context.mode != "OBJECT" and view_layer.objects.active is not None:
                with contextlib.suppress(Exception):
                    bpy.ops.object.mode_set(mode="OBJECT")
            for o in view_layer.objects:
                o.select_set(False)
            obj.hide_viewport = False
            obj.hide_select = False
            obj.select_set(True)
            view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception as exc:
            log_info(f"smart_project failed on {obj.name!r}: {exc}")
            with contextlib.suppress(Exception):
                bpy.ops.object.mode_set(mode="OBJECT")

    return "UVMap", warn_msg


def _save_baked_image(image: Any, out_path: str, is_non_color: bool, scene: Any) -> None:
    """Save *image* as 8-bit sRGB PNG or 16-bit Non-Color PNG without a view transform."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img_settings = scene.render.image_settings
    saved_fmt = img_settings.file_format
    saved_depth = img_settings.color_depth
    saved_mode = img_settings.color_mode
    try:
        img_settings.file_format = "PNG"
        img_settings.color_depth = "16" if is_non_color else "8"
        img_settings.color_mode = "RGB"
        image.filepath_raw = out_path
        image.file_format = "PNG"
        saved_view_transform = scene.view_settings.view_transform
        if is_non_color:
            with contextlib.suppress(Exception):
                scene.view_settings.view_transform = "Raw"
        try:
            image.save_render(filepath=out_path, scene=scene)
        except Exception:
            image.save()
        finally:
            with contextlib.suppress(Exception):
                scene.view_settings.view_transform = saved_view_transform
    finally:
        with contextlib.suppress(Exception):
            img_settings.file_format = saved_fmt
            img_settings.color_depth = saved_depth
            img_settings.color_mode = saved_mode


def _find_bpy_principled_socket(bsdf_node: Any, channel_name: str) -> Optional[Any]:
    if bsdf_node is None:
        return None
    if channel_name == "base_color":
        return shader_graph.get_input_socket(bsdf_node, "Base Color", "Color")
    if channel_name == "emission":
        return shader_graph.get_input_socket(bsdf_node, "Emission Color", "Emission", "Color")
    if channel_name in ("normal", "bump"):
        return shader_graph.get_input_socket(bsdf_node, "Normal")
    for ch, names, _, _ in shader_graph.PRINCIPLED_CHANNEL_SPECS:
        if ch == channel_name:
            return shader_graph.get_input_socket(bsdf_node, *names)
    return shader_graph.get_input_socket(bsdf_node, channel_name)


def _fallback_leaf_channel_rgba(bsdf_node: Any, channel_name: str) -> Tuple[float, float, float, float]:
    stype = shader_graph.canonical_node_type(bsdf_node)
    if channel_name == "metallic":
        val = 1.0 if stype in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_METALLIC") else 0.0
        return (val, val, val, 1.0)
    if channel_name == "roughness":
        if stype in ("BSDF_GLASS", "BSDF_REFRACTION"):
            val = 0.0
        elif stype in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_METALLIC"):
            val = 0.2
        else:
            val = 0.5
        return (val, val, val, 1.0)
    if channel_name == "alpha":
        val = 0.0 if stype == "BSDF_TRANSPARENT" else 1.0
        return (val, val, val, 1.0)
    if channel_name == "transmission":
        val = 1.0 if stype in ("BSDF_GLASS", "BSDF_REFRACTION") else 0.0
        return (val, val, val, 1.0)
    if channel_name == "base_color":
        return (0.8, 0.8, 0.8, 1.0)
    return (0.0, 0.0, 0.0, 1.0)


def _srgb_to_linear(c: float) -> float:
    if c <= 0.0:
        return 0.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _detect_constant_bake(
    bake_img: Any,
    ch_name: str,
    *,
    is_color: bool,
    tol: float = 0.008,
) -> Tuple[bool, Optional[Any]]:
    """Inspect baked pixel buffer and return ``(True, constant_value)`` if uniform across UV islands."""
    if ch_name in ("normal", "bump", "displacement"):
        # Fine relief (brushed metal: bump strength 0.02) varies by far less than a colour step
        # yet carries the whole look; only drop a truly flat map.
        tol = min(tol, 0.0005)
    try:
        px = list(bake_img.pixels)
    except Exception:
        return False, None
    if len(px) < 4:
        return False, None

    r_all = px[0::4]
    g_all = px[1::4]
    b_all = px[2::4]
    a_all = px[3::4]

    # When baked with use_clear=False onto an alpha=0 image, UV island pixels have alpha >= 0.99
    if min(a_all) < 0.99 and max(a_all) >= 0.99:
        rs = [r for r, a in zip(r_all, a_all) if a >= 0.99]
        if not rs:
            return False, None
        r_min, r_max = min(rs), max(rs)
        if (r_max - r_min) > tol:
            return False, None
        if is_color or ch_name == "normal":
            gs = [g for g, a in zip(g_all, a_all) if a >= 0.99]
            bs = [b for b, a in zip(b_all, a_all) if a >= 0.99]
            g_min, g_max = min(gs), max(gs)
            b_min, b_max = min(bs), max(bs)
        else:
            g_min = g_max = b_min = b_max = 0.0
    else:
        r_min, r_max = min(r_all), max(r_all)
        if (r_max - r_min) > tol:
            return False, None
        if is_color or ch_name == "normal":
            g_min, g_max = min(g_all), max(g_all)
            b_min, b_max = min(b_all), max(b_all)
        else:
            g_min = g_max = b_min = b_max = 0.0

    if ch_name == "normal":
        if (g_max - g_min) <= tol and (b_max - b_min) <= tol:
            return True, None
        return False, None

    if is_color:
        if (g_max - g_min) > tol or (b_max - b_min) > tol:
            return False, None
        r_mid = (r_min + r_max) * 0.5
        g_mid = (g_min + g_max) * 0.5
        b_mid = (b_min + b_max) * 0.5
        # Color bake buffers use sRGB color management; convert back to linear RGB for manifest
        rgb = [
            round(_srgb_to_linear(r_mid), 6),
            round(_srgb_to_linear(g_mid), 6),
            round(_srgb_to_linear(b_mid), 6),
        ]
        return True, rgb

    val = round((r_min + r_max) * 0.5, 6)
    if ch_name in (
        "metallic",
        "roughness",
        "alpha",
        "transmission",
        "specular",
        "coat",
        "coat_roughness",
        "sheen",
        "subsurface",
    ):
        val = max(0.0, min(1.0, val))
    if abs(val) < 1e-4:
        val = 0.0
    elif abs(val - 1.0) < 1e-4:
        val = 1.0
    return True, val


def _build_channel_emission_branch(
    node_tree: Any,
    shader_node: Any,
    channel_name: str,
    temp_nodes: List[Any],
    depth: int = 0,
) -> Any:
    """Recursively build an Emission/MixShader tree representing *channel_name* for *shader_node*."""
    stype = shader_graph.canonical_node_type(shader_node) if shader_node is not None else ""
    if depth < 8 and shader_node is not None and stype in ("MIX_SHADER", "ADD_SHADER"):
        shader_inputs = [s for s in shader_node.inputs if s.type == "SHADER"]
        sub1 = (
            shader_inputs[0].links[0].from_node
            if len(shader_inputs) > 0 and shader_inputs[0].is_linked
            else None
        )
        sub2 = (
            shader_inputs[1].links[0].from_node
            if len(shader_inputs) > 1 and shader_inputs[1].is_linked
            else None
        )
        t1 = shader_graph.canonical_node_type(sub1) if sub1 is not None else ""
        t2 = shader_graph.canonical_node_type(sub2) if sub2 is not None else ""
        if stype == "MIX_SHADER" and channel_name != "alpha":
            if t1 == "BSDF_TRANSPARENT" and sub2 is not None:
                return _build_channel_emission_branch(node_tree, sub2, channel_name, temp_nodes, depth + 1)
            if t2 == "BSDF_TRANSPARENT" and sub1 is not None:
                return _build_channel_emission_branch(node_tree, sub1, channel_name, temp_nodes, depth + 1)

        out1 = _build_channel_emission_branch(node_tree, sub1, channel_name, temp_nodes, depth + 1)
        out2 = _build_channel_emission_branch(node_tree, sub2, channel_name, temp_nodes, depth + 1)
        if stype == "ADD_SHADER":
            comb = node_tree.nodes.new("ShaderNodeAddShader")
            temp_nodes.append(comb)
            node_tree.links.new(out1, comb.inputs[0])
            node_tree.links.new(out2, comb.inputs[1])
            return comb.outputs["Shader"]

        mix_sh = node_tree.nodes.new("ShaderNodeMixShader")
        temp_nodes.append(mix_sh)
        fac_in = shader_graph.get_input_socket(shader_node, "Factor", "Fac")
        mix_fac_in = shader_graph.get_input_socket(mix_sh, "Factor", "Fac") or mix_sh.inputs[0]
        if fac_in is not None and mix_fac_in is not None:
            if fac_in.is_linked:
                node_tree.links.new(fac_in.links[0].from_socket, mix_fac_in)
            else:
                mix_fac_in.default_value = fac_in.default_value
        node_tree.links.new(out1, mix_sh.inputs[1])
        node_tree.links.new(out2, mix_sh.inputs[2])
        return mix_sh.outputs["Shader"]

    em = node_tree.nodes.new("ShaderNodeEmission")
    temp_nodes.append(em)
    sock = _find_bpy_principled_socket(shader_node, channel_name)
    if sock is not None and sock.is_linked:
        node_tree.links.new(sock.links[0].from_socket, em.inputs["Color"])
    elif sock is not None:
        with contextlib.suppress(Exception):
            dv = sock.default_value
            if isinstance(dv, (int, float)):
                em.inputs["Color"].default_value = (float(dv), float(dv), float(dv), 1.0)
            else:
                seq = list(dv)
                em.inputs["Color"].default_value = (
                    float(seq[0]),
                    float(seq[1]),
                    float(seq[2]),
                    1.0,
                )
    else:
        em.inputs["Color"].default_value = _fallback_leaf_channel_rgba(shader_node, channel_name)

    if channel_name == "emission" and shader_node is not None:
        em_str_sock = shader_graph.get_input_socket(shader_node, "Emission Strength", "Strength")
        if em_str_sock is not None and em_str_sock.is_linked:
            node_tree.links.new(em_str_sock.links[0].from_socket, em.inputs["Strength"])

    return em.outputs["Emission"]


def _wire_channel_to_emission(
    node_tree: Any,
    output_node: Any,
    surf_node: Any,
    channel_name: str,
) -> Tuple[List[Any], Optional[Any]]:
    """Temporarily route *channel_name*'s value into an Emission shader on *output_node*.

    Returns ``(temp_nodes_created, original_surface_from_socket)``.
    """
    surf_in = output_node.inputs.get("Surface")
    orig_from_socket = surf_in.links[0].from_socket if (surf_in and surf_in.is_linked) else None
    temp_nodes: List[Any] = []

    if channel_name == "displacement":
        disp_in = output_node.inputs.get("Displacement")
        if disp_in and disp_in.is_linked:
            # Bake the final displacement along the normal, in metres: dot(displacement vector,
            # normal). It already includes Height, Midlevel, Scale and anything upstream, so the
            # manifest needs no reconstruction of Blender's node maths. The bake object has an
            # identity transform, so object and world space agree.
            dot = node_tree.nodes.new("ShaderNodeVectorMath")
            dot.operation = "DOT_PRODUCT"
            geo = node_tree.nodes.new("ShaderNodeNewGeometry")
            em = node_tree.nodes.new("ShaderNodeEmission")
            temp_nodes.extend((dot, geo, em))
            node_tree.links.new(disp_in.links[0].from_socket, dot.inputs[0])
            node_tree.links.new(geo.outputs["Normal"], dot.inputs[1])
            node_tree.links.new(dot.outputs["Value"], em.inputs["Color"])
            node_tree.links.new(em.outputs["Emission"], surf_in)
            return temp_nodes, orig_from_socket

    out_sock = _build_channel_emission_branch(node_tree, surf_node, channel_name, temp_nodes)
    node_tree.links.new(out_sock, surf_in)
    return temp_nodes, orig_from_socket


def _wire_normal_for_bake(
    node_tree: Any,
    output_node: Any,
    surf_node: Any,
) -> Tuple[List[Any], Optional[Any]]:
    """Temporarily route ``Normal`` into a pure Diffuse BSDF so transparency/transmission cannot zero normals."""
    surf_in = output_node.inputs.get("Surface")
    orig_from_socket = surf_in.links[0].from_socket if (surf_in and surf_in.is_linked) else None
    temp_nodes: List[Any] = []

    stype = shader_graph.canonical_node_type(surf_node)
    if stype in ("MIX_SHADER", "ADD_SHADER"):
        return temp_nodes, orig_from_socket

    nrm_sock = _find_bpy_principled_socket(surf_node, "normal")
    if nrm_sock is not None and nrm_sock.is_linked:
        diff = node_tree.nodes.new("ShaderNodeBsdfDiffuse")
        temp_nodes.append(diff)
        node_tree.links.new(nrm_sock.links[0].from_socket, diff.inputs["Normal"])
        node_tree.links.new(diff.outputs["BSDF"], surf_in)

    return temp_nodes, orig_from_socket


def _bake_material_channels(
    mat: Any,
    cls: shader_graph.MaterialClassification,
    output_dir: str,
    bake_resolution: int,
    progress_base: float,
    progress_span: float,
    pristine: Optional[Any] = None,
) -> List[str]:
    """Bake all channels in ``cls.sockets_to_bake`` for *mat* using Cycles.

    *pristine* is an un-flattened copy of *mat*. It is baked instead whenever its surface shader
    sits at the top level, so groups are evaluated by Cycles itself rather than by our inliner.
    """
    if not cls.sockets_to_bake or not mat.use_nodes or mat.node_tree is None:
        return []
    bake_mat = mat
    if pristine is not None:
        p_out = _find_active_material_output(pristine.node_tree)
        p_surf = p_out.inputs.get("Surface") if p_out is not None else None
        if p_surf is not None and p_surf.is_linked and p_surf.links[0].from_node.bl_idname != "ShaderNodeGroup":
            bake_mat = pristine

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    objs = _objects_using_material(scene, mat)
    if not objs:
        log_info(f"bake: skipping {mat.name!r} (no mesh objects use it)")
        return []

    uv_name, uv_warn = _ensure_uvs_on_material_objects(mat, objs)
    if uv_warn and uv_warn not in cls.warnings:
        cls.warnings.append(uv_warn)
    cls.uv["map"] = uv_name

    rep_obj = max(objs, key=_mesh_world_area)

    # Create a temporary single-material duplicate of rep_obj so multi-material slots
    # never interfere with Cycles baking and rep_obj's original slots stay untouched.
    bake_mesh = rep_obj.data.copy()
    bake_mesh.materials.clear()
    bake_mesh.materials.append(bake_mat)
    for poly in bake_mesh.polygons:
        poly.material_index = 0
    if uv_name in bake_mesh.uv_layers:
        bake_mesh.uv_layers.active = bake_mesh.uv_layers[uv_name]
        bake_mesh.uv_layers[uv_name].active_render = True

    bake_obj = bpy.data.objects.new(f"_kb_bake_{_safe_filename(mat.name)}", bake_mesh)
    bake_obj.matrix_world = rep_obj.matrix_world.copy()
    scene.collection.objects.link(bake_obj)

    orig_rep_hide_render = rep_obj.hide_render
    orig_rep_hide_vp = rep_obj.hide_viewport
    rep_obj.hide_render = True
    rep_obj.hide_viewport = True

    nt = bake_mat.node_tree
    output_node = _find_active_material_output(nt)
    if output_node is None:
        return []
    surf_in = output_node.inputs.get("Surface")
    surf_node = surf_in.links[0].from_node if (surf_in and surf_in.is_linked) else None
    if surf_node is None:
        return []

    baked_channels: List[str] = []
    tex_dir = os.path.join(output_dir, "textures")
    os.makedirs(tex_dir, exist_ok=True)
    safe_mat = _safe_filename(mat.name, "mat")

    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    try:
        total_ch = len(cls.sockets_to_bake)
        for idx, ch_name in enumerate(cls.sockets_to_bake):
            frac = progress_base + progress_span * (idx / max(1, total_ch))
            emit_progress(frac, f"Baking {mat.name}: {ch_name}")

            is_color = ch_name in ("base_color", "emission")
            is_non_color = not is_color
            img_name = f"{safe_mat}_{ch_name}"
            out_png = os.path.join(tex_dir, f"{img_name}.png")
            rel_png = f"textures/{img_name}.png"

            bake_img = bpy.data.images.new(
                name=f"_kb_{img_name}",
                width=bake_resolution,
                height=bake_resolution,
                alpha=True,
                float_buffer=is_non_color,
            )
            with contextlib.suppress(Exception):
                bake_img.colorspace_settings.name = "Non-Color" if is_non_color else "sRGB"
                bake_img.generated_color = (0.0, 0.0, 0.0, 0.0)

            tex_node = nt.nodes.new("ShaderNodeTexImage")
            tex_node.image = bake_img
            temp_nodes: List[Any] = []
            orig_surf_sock: Optional[Any] = None

            try:
                if ch_name == "normal":
                    temp_nodes, orig_surf_sock = _wire_normal_for_bake(nt, output_node, surf_node)
                    bake_type = "NORMAL"
                else:
                    temp_nodes, orig_surf_sock = _wire_channel_to_emission(
                        nt, output_node, surf_node, ch_name
                    )
                    bake_type = "EMIT"

                for n in nt.nodes:
                    n.select = False
                tex_node.select = True
                nt.nodes.active = tex_node

                bpy.ops.object.select_all(action="DESELECT")
                bake_obj.select_set(True)
                view_layer.objects.active = bake_obj

                if bake_type == "NORMAL":
                    bpy.ops.object.bake(
                        type="NORMAL",
                        normal_space="TANGENT",
                        normal_r="POS_X",
                        normal_g="POS_Y",
                        normal_b="POS_Z",
                        use_clear=False,
                    )
                else:
                    bpy.ops.object.bake(type="EMIT", use_clear=False)

                raw_midlevel = raw_scale = None
                if ch_name == "displacement":
                    disp_node = (
                        output_node.inputs["Displacement"].links[0].from_node
                        if output_node.inputs.get("Displacement")
                        and output_node.inputs["Displacement"].is_linked
                        else None
                    )
                    raw_midlevel = float(
                        shader_graph.get_input_socket(disp_node, "Midlevel").default_value
                    ) if disp_node is not None and disp_node.bl_idname == "ShaderNodeDisplacement" else 0.5
                    raw_scale = float(
                        shader_graph.get_input_socket(disp_node, "Scale").default_value
                    ) if disp_node is not None and disp_node.bl_idname == "ShaderNodeDisplacement" else 0.05
                    method = _displacement_method(mat)
                    log_info(
                        f"bake: displacement stats before normalization for {mat.name!r}: "
                        f"midlevel={raw_midlevel:.9g}, scale={raw_scale:.9g}, method={method!r}"
                    )

                is_const_bake, const_bake_val = _detect_constant_bake(
                    bake_img, ch_name, is_color=is_color
                )
                if is_const_bake:
                    if os.path.isfile(out_png):
                        with contextlib.suppress(OSError):
                            os.remove(out_png)
                    log_info(
                        f"bake: {mat.name!r} channel {ch_name!r} is constant ({const_bake_val}); "
                        f"replacing texture with constant value"
                    )
                    if ch_name in ("normal", "bump", "displacement"):
                        cls.channels.pop(ch_name, None)
                    elif ch_name == "emission":
                        rgb_val = const_bake_val or [0.0, 0.0, 0.0]
                        if any(float(c) > 1e-5 for c in rgb_val):
                            meta = cls.bake_metadata.get("emission") or {"strength": 1.0}
                            cls.channels["emission"] = {
                                "value": rgb_val,
                                "strength": float(meta.get("strength", 1.0)),
                            }
                        else:
                            cls.channels.pop("emission", None)
                    elif (
                        ch_name in shader_graph._OPTIONAL_ZERO_CHANNELS
                        and const_bake_val == shader_graph._OPTIONAL_ZERO_CHANNELS[ch_name]
                    ):
                        cls.channels.pop(ch_name, None)
                    elif const_bake_val is not None:
                        cls.channels[ch_name] = {"value": const_bake_val}
                    continue

                manifest_midlevel = manifest_scale = None
                if ch_name == "displacement":
                    pixels = list(bake_img.pixels)
                    valid = [
                        float(pixels[i])
                        for i in range(0, len(pixels), 4)
                        if len(pixels) > i + 3 and float(pixels[i + 3]) >= 0.99
                    ]
                    if not valid:
                        valid = [float(pixels[i]) for i in range(0, len(pixels), 4)]
                    if not valid:
                        raise RuntimeError("displacement bake produced no pixels")
                    raw_min, raw_max = min(valid), max(valid)
                    raw_mean = sum(valid) / len(valid)
                    log_info(
                        f"bake: displacement float stats for {mat.name!r}: "
                        f"min={raw_min:.9g}, max={raw_max:.9g}, mean={raw_mean:.9g}"
                    )
                    span = raw_max - raw_min
                    if span <= 1e-12:
                        raise RuntimeError("displacement bake is constant but was not detected")
                    for pixel_index in range(0, len(pixels), 4):
                        value = (float(pixels[pixel_index]) - raw_min) / span
                        pixels[pixel_index] = value
                        pixels[pixel_index + 1] = value
                        pixels[pixel_index + 2] = value
                    bake_img.pixels = pixels
                    # Texels are the baked displacement in metres, normalised: d = min + t * span.
                    # The importer computes scale * (t - midlevel), so scale = span and
                    # midlevel = -min / span reproduce d exactly.
                    manifest_scale = span
                    manifest_midlevel = -raw_min / span
                    log_info(
                        f"bake: displacement normalized stats for {mat.name!r}: "
                        f"min=0, max=1, mean={(raw_mean - raw_min) / span:.9g}"
                    )

                _save_baked_image(bake_img, out_png, is_non_color=is_non_color, scene=scene)

                if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
                    baked_channels.append(ch_name)
                    if ch_name == "normal":
                        cls.channels["normal"] = {
                            "texture": rel_png,
                            "colorspace": "raw",
                            "strength": 1.0,
                            "flip_green": False,
                        }
                    elif ch_name == "displacement":
                        meta = cls.bake_metadata.get("displacement") or {"midlevel": 0.5, "scale": 0.05}
                        # C4D computes scale * (texel - midlevel). For normalized texels,
                        # this preserves (raw_height - raw_midlevel) * raw_scale.
                        cls.channels["displacement"] = {
                            "texture": rel_png,
                            "colorspace": "raw",
                            "midlevel": float(
                                manifest_midlevel
                                if manifest_midlevel is not None
                                else meta.get("midlevel", 0.5)
                            ),
                            "scale": float(
                                manifest_scale
                                if manifest_scale is not None
                                else meta.get("scale", 0.05)
                            ),
                        }
                    elif ch_name == "emission":
                        meta = cls.bake_metadata.get("emission") or {"strength": 1.0}
                        cls.channels["emission"] = {
                            "texture": rel_png,
                            "colorspace": "srgb",
                            "channel": "rgb",
                            "strength": float(meta.get("strength", 1.0)),
                        }
                    elif ch_name == "roughness":
                        cls.channels["roughness"] = {
                            "texture": rel_png,
                            "colorspace": "raw",
                            "channel": "r",
                            "invert": False,
                        }
                    elif ch_name == "alpha":
                        cls.channels["alpha"] = {
                            "texture": rel_png,
                            "channel": "r",
                        }
                    elif is_color:
                        cls.channels[ch_name] = {
                            "texture": rel_png,
                            "colorspace": "srgb",
                            "channel": "rgb",
                        }
                    else:
                        cls.channels[ch_name] = {
                            "texture": rel_png,
                            "colorspace": "raw",
                            "channel": "r",
                        }
            except Exception as exc:
                warn = f"Failed to bake channel '{ch_name}' on material '{mat.name}': {exc}"
                emit_warning(warn)
                if warn not in cls.warnings:
                    cls.warnings.append(warn)
            finally:
                if orig_surf_sock is not None and surf_in is not None:
                    with contextlib.suppress(Exception):
                        nt.links.new(orig_surf_sock, surf_in)
                for tn in temp_nodes:
                    with contextlib.suppress(Exception):
                        nt.nodes.remove(tn)
                with contextlib.suppress(Exception):
                    nt.nodes.remove(tex_node)
                with contextlib.suppress(Exception):
                    bpy.data.images.remove(bake_img)
    finally:
        rep_obj.hide_render = orig_rep_hide_render
        rep_obj.hide_viewport = orig_rep_hide_vp
        with contextlib.suppress(Exception):
            bpy.data.objects.remove(bake_obj, do_unlink=True)
        with contextlib.suppress(Exception):
            bpy.data.meshes.remove(bake_mesh, do_unlink=True)
        with contextlib.suppress(Exception):
            view_layer.objects.active = rep_obj

    # When all textured channels were produced by baking, UV mapping is already baked into 0..1
    has_unbaked_tex = any(
        isinstance(v, dict) and "texture" in v and k not in baked_channels
        for k, v in cls.channels.items()
    )
    if baked_channels and not has_unbaked_tex:
        cls.uv["scale"] = [1.0, 1.0]
        cls.uv["offset"] = [0.0, 0.0]
        cls.uv["rotation"] = 0.0

    return baked_channels


# ── Step 5: FBX Export ───────────────────────────────────────────────────────


def _ensure_fbx_addon() -> None:
    if hasattr(bpy.ops.export_scene, "fbx"):
        return
    import addon_utils  # type: ignore[import-not-found]

    for mod_name in (
        "io_scene_fbx",
        "bl_ext.system.io_scene_fbx",
        "bl_ext.blender_org.io_scene_fbx",
        "bl_ext.user_default.io_scene_fbx",
    ):
        with contextlib.suppress(Exception):
            addon_utils.enable(mod_name, default_set=True, persistent=False)
            if hasattr(bpy.ops.export_scene, "fbx"):
                return
    raise RuntimeError("Blender FBX exporter add-on (io_scene_fbx) is unavailable.")


def export_geometry_fbx(out_fbx_path: str, *, only_lights: bool = False) -> None:
    """Export scene geometry to ``geometry.fbx`` (Y-up, -Z forward, meters, face smoothing)."""
    _ensure_fbx_addon()
    os.makedirs(os.path.dirname(out_fbx_path) or ".", exist_ok=True)

    obj_types = {"EMPTY", "MESH", "OTHER"}

    desired: Dict[str, Any] = {
        "filepath": out_fbx_path,
        "use_selection": False,
        "use_visible": True,
        "use_active_collection": False,
        "global_scale": 1.0,
        "apply_unit_scale": True,
        "apply_scale_options": "FBX_SCALE_NONE",
        "use_space_transform": True,
        "axis_forward": "-Z",
        "axis_up": "Y",
        "object_types": obj_types,
        "use_mesh_modifiers": True,
        "mesh_smooth_type": "FACE",
        "use_subsurf": False,
        "use_mesh_edges": False,
        "use_tspace": False,
        "use_triangles": False,
        "use_custom_props": False,
        "add_leaf_bones": False,
        "bake_anim": False,
        "path_mode": "STRIP",
        "embed_textures": False,
    }

    try:
        rna_keys = set(bpy.ops.export_scene.fbx.get_rna_type().properties.keys())
        accepted = {k: v for k, v in desired.items() if k in rna_keys}
    except Exception:
        accepted = desired

    bpy.ops.export_scene.fbx(**accepted)


# ── Step 6: Manifest Assembly ────────────────────────────────────────────────


def _translucency_weight(mat: Any) -> float:
    """Share of the surface going through a Translucent BSDF; baking a colour cannot express it."""
    tree = getattr(mat, "node_tree", None)
    if tree is None:
        return 0.0
    output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)

    surface = output.inputs.get("Surface") if output is not None else None
    # Light passing through the sheet is what light/shadow rays see, which Light Path tricks often
    # route differently from what the camera sees: take whichever lets more light through.
    return min(1.0, max(
        sum(w for node, w in _walk_mixed_shaders(surface, rays=rays) if node.type == "BSDF_TRANSLUCENT")
        for rays in ("camera", "light")
    ))


def _mix_factor(mix_node: Any, rays: str = "camera") -> float:
    """Factor of a Mix Shader for camera rays or for light/shadow rays.

    Light Path tricks are resolved: "Is Camera Ray" is 1 only for camera rays, "Is Shadow Ray" only
    for light rays. Any other driven factor (Fresnel, textures...) counts as half.
    """
    fac_in = mix_node.inputs[0]
    if not fac_in.is_linked:
        return float(fac_in.default_value)
    link = fac_in.links[0]
    if link.from_node.type == "LIGHT_PATH":
        if link.from_socket.name == "Is Camera Ray":
            return 1.0 if rays == "camera" else 0.0
        if link.from_socket.name == "Is Shadow Ray":
            return 1.0 if rays == "light" else 0.0
        return 0.0
    return 0.5


def _walk_mixed_shaders(socket: Any, weight: float = 1.0, depth: int = 0,
                        rays: str = "camera") -> List[Tuple[Any, float]]:
    """Leaf shader nodes behind Mix/Add Shaders with the share of the surface each one gets."""
    if socket is None or not socket.is_linked or depth > 8:
        return []
    node = socket.links[0].from_node
    if node.type == "ADD_SHADER":
        return (_walk_mixed_shaders(node.inputs[0], weight, depth + 1, rays)
                + _walk_mixed_shaders(node.inputs[1], weight, depth + 1, rays))
    if node.type == "MIX_SHADER":
        fac = _mix_factor(node, rays)
        return (_walk_mixed_shaders(node.inputs[1], weight * (1.0 - fac), depth + 1, rays)
                + _walk_mixed_shaders(node.inputs[2], weight * fac, depth + 1, rays))
    return [(node, weight)]


def _subsurface_from_mixes(mat: Any) -> Optional[Dict[str, float]]:
    """Subsurface of Principled BSDFs reached through Mix Shaders, which the classifier drops.

    Returns ``{"value": share-weighted weight, "scale": metres}`` or None.
    """
    tree = getattr(mat, "node_tree", None)
    output = _find_active_material_output(tree) if tree is not None else None
    if output is None:
        return None
    total, scale = 0.0, 0.0
    for node, share in _walk_mixed_shaders(output.inputs.get("Surface")):
        if node.type != "BSDF_PRINCIPLED":
            continue
        w_in = node.inputs.get("Subsurface Weight") or node.inputs.get("Subsurface")
        if w_in is None or w_in.is_linked or float(w_in.default_value) <= 0.0:
            continue
        total += share * float(w_in.default_value)
        s_in = node.inputs.get("Subsurface Scale")
        if s_in is not None and not s_in.is_linked:
            scale = max(scale, float(s_in.default_value))
    return {"value": round(min(1.0, total), 4), "scale": scale} if total > 0.01 else None


def _displacement_method(mat: Any) -> str:
    """Cycles displacement method: BUMP (default), DISPLACEMENT or BOTH.

    Blender 4.1+ keeps it on the material; older versions on ``mat.cycles``.
    """
    for owner in (mat, getattr(mat, "cycles", None)):
        raw = str(getattr(owner, "displacement_method", "") or "").upper() if owner is not None else ""
        if raw in ("BUMP", "DISPLACEMENT", "BOTH"):
            return raw
    return "BUMP"


def _resolve_blend_method(
    mat: Any,
    channels: Dict[str, Dict[str, Any]],
    cls_blend_method: str = "",
) -> str:
    raw_bm = str(getattr(mat, "blend_method", "") or "").upper()
    if raw_bm == "CLIP" or cls_blend_method == "clip":
        return "clip"

    alpha_ch = channels.get("alpha")
    has_non_opaque_alpha = False
    if isinstance(alpha_ch, dict):
        if "texture" in alpha_ch:
            has_non_opaque_alpha = True
        else:
            val = alpha_ch.get("value")
            if isinstance(val, (int, float)) and float(val) < 0.999:
                has_non_opaque_alpha = True

    trans_ch = channels.get("transmission")
    has_transmission = False
    if isinstance(trans_ch, dict):
        if "texture" in trans_ch:
            has_transmission = True
        else:
            val = trans_ch.get("value")
            if isinstance(val, (int, float)) and float(val) > 0.001:
                has_transmission = True

    if has_non_opaque_alpha or has_transmission or cls_blend_method == "blend":
        return "blend"

    surf_rm = str(getattr(mat, "surface_render_method", "") or "").upper()
    if surf_rm == "BLENDED" and "alpha" not in channels:
        return "blend"

    return "opaque"


def _collect_scene_objects_and_materials(scene: Any) -> Tuple[List[Dict[str, Any]], List[Any]]:
    objects_manifest: List[Dict[str, Any]] = []
    used_materials: List[Any] = []
    seen_mats: Set[str] = set()

    for obj in scene.objects:
        if obj.type != "MESH" or obj.data is None or len(obj.data.polygons) == 0:
            continue
        slot_names: List[str] = []
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None:
                slot_names.append(mat.name)
                if mat.name not in seen_mats:
                    seen_mats.add(mat.name)
                    used_materials.append(mat)
        objects_manifest.append({"name": obj.name, "materials": slot_names})

    if not used_materials:
        for mat in bpy.data.materials:
            if not mat.name.startswith("Dots Stroke") and mat.name not in seen_mats:
                seen_mats.add(mat.name)
                used_materials.append(mat)

    return objects_manifest, used_materials


def _default_uv_for_material(scene: Any, mat: Any) -> str:
    for obj in _objects_using_material(scene, mat):
        mesh = obj.data
        if mesh is not None and len(mesh.uv_layers) > 0:
            active = mesh.uv_layers.active or mesh.uv_layers[0]
            if active and active.name:
                return active.name
    return "UVMap"


def _light_manifest_entry(obj: Any) -> Dict[str, Any]:
    """Serialize a renderable light using the same Y-up/-Z-forward convention as FBX.

    Blender's Z-up matrix is converted to the FBX export convention
    (axis_up='Y', axis_forward='-Z') just as the FBX exporter converts meshes.
    """
    data = obj.data
    light_type = str(getattr(data, "type", "")).lower()
    if light_type not in ("point", "spot", "area", "sun"):
        raise ValueError(f"Unsupported Blender light type: {light_type!r}")
    matrix = [[float(v) for v in row] for row in obj.matrix_world]
    kwargs: Dict[str, Any] = {
        "name": obj.name,
        "light_type": light_type,
        "color": tuple(getattr(data, "color", (1.0, 1.0, 1.0))),
        "power": float(getattr(data, "energy", 0.0)),
        "radius": float(getattr(data, "shadow_soft_size", 0.0)),
        "matrix": matrix,
        "parent": getattr(getattr(obj, "parent", None), "name", None),
    }
    if light_type == "area":
        kwargs.update(
            shape=getattr(data, "shape", None),
            size=getattr(data, "size", None),
            size_y=getattr(data, "size_y", None),
        )
    if light_type == "spot":
        kwargs.update(
            spot_size=getattr(data, "spot_size", None),
            spot_blend=getattr(data, "spot_blend", None),
        )
    entry = shader_graph.build_light_manifest_entry(**kwargs)
    if light_type == "sun":
        entry["strength"] = float(getattr(data, "energy", 0.0))
    return entry


def _collect_scene_lights(scene: Any) -> List[Dict[str, Any]]:
    lights: List[Dict[str, Any]] = []
    for obj in scene.objects:
        if obj.type != "LIGHT" or getattr(obj, "hide_render", False):
            continue
        with contextlib.suppress(Exception):
            if obj.hide_get():
                continue
        try:
            lights.append(_light_manifest_entry(obj))
        except ValueError as exc:
            emit_warning(str(exc))
    return lights


def run_export(params: Dict[str, Any]) -> str:
    """Execute the full Blender export & bake pipeline and return the manifest path."""
    output_dir = os.path.abspath(str(params.get("output_dir") or "."))
    os.makedirs(output_dir, exist_ok=True)

    blend_path = str(params.get("blend_path") or "").strip()
    if blend_path and os.path.abspath(blend_path) != os.path.abspath(bpy.data.filepath or ""):
        emit_progress(0.02, "Opening .blend file")
        bpy.ops.wm.open_mainfile(filepath=blend_path)

    raw_asset_meta = params.get("asset") or params.get("asset_meta")
    asset_meta = raw_asset_meta if isinstance(raw_asset_meta, dict) else {}
    asset_type = str(
        params.get("asset_type") or asset_meta.get("type") or "model"
    ).strip().lower()
    asset_name = str(params.get("asset_name") or asset_meta.get("name") or "").strip()
    asset_id = str(params.get("asset_id") or asset_meta.get("id") or "").strip()
    asset_base_id = str(
        params.get("asset_base_id") or asset_meta.get("base_id") or asset_id
    ).strip()
    resolution_label = str(
        params.get("resolution") or asset_meta.get("resolution") or "2048"
    ).strip()
    bake_resolution = int(params.get("bake_resolution") or 1024)
    max_texture_size = int(params.get("max_texture_size") or 0)

    emit_progress(0.08, "Preparing scene and geometry")
    only_lights = prepare_scene(asset_type, asset_name=asset_name, asset_id=asset_id)

    emit_progress(0.20, "Unpacking and collecting textures")
    unpack_and_collect_textures(output_dir, max_texture_size=max_texture_size)

    scene = bpy.context.scene
    objects_manifest, materials_list = _collect_scene_objects_and_materials(scene)
    lights_manifest = _collect_scene_lights(scene)

    # Untouched copies for baking: the pure-Python inliner (the ungroup operator needs a node editor,
    # unavailable in background mode) can't reproduce every node exactly, which skewed bakes of
    # procedural groups (Wood Fluted Panel: height 0..0.06 instead of 0..1).
    pristine: Dict[str, Any] = {}
    for mat in materials_list:
        if mat.use_nodes and mat.node_tree is not None and any(
            n.bl_idname == "ShaderNodeGroup" for n in mat.node_tree.nodes
        ):
            pristine[mat.name] = mat.copy()

    emit_progress(0.32, "Flattening shader node groups")
    for mat in materials_list:
        if mat.use_nodes and mat.node_tree is not None:
            _ungroup_all(mat.node_tree, mat)

    emit_progress(0.40, "Analysing material shader networks")
    classified_materials: List[Tuple[Any, shader_graph.MaterialClassification]] = []
    any_bakes_needed = False

    for mat in materials_list:
        uv_default = _default_uv_for_material(scene, mat)
        if not mat.use_nodes or mat.node_tree is None:
            cls = shader_graph.MaterialClassification(
                channels={
                    "base_color": {
                        "value": [
                            round(float(mat.diffuse_color[0]), 6),
                            round(float(mat.diffuse_color[1]), 6),
                            round(float(mat.diffuse_color[2]), 6),
                        ]
                    },
                    "metallic": {"value": round(float(getattr(mat, "metallic", 0.0)), 6)},
                    "roughness": {"value": round(float(getattr(mat, "roughness", 0.5)), 6)},
                },
                uv=shader_graph.UVTransform(map_name=uv_default).to_dict(),
            )
        else:
            out_node = _find_active_material_output(mat.node_tree)
            if out_node is None:
                cls = shader_graph.MaterialClassification(
                    channels={
                        "base_color": {"value": [0.8, 0.8, 0.8]},
                        "metallic": {"value": 0.0},
                        "roughness": {"value": 0.5},
                    },
                    uv=shader_graph.UVTransform(map_name=uv_default).to_dict(),
                    warnings=["Material has no Material Output node; using default values."],
                )
            else:
                duck_out = _build_duck_graph_from_bpy(out_node, mat=mat)
                cls = shader_graph.classify_material_output(duck_out, default_uv_map=uv_default)

        for w in cls.warnings:
            emit_warning(f"[{mat.name}] {w}")
        if cls.sockets_to_bake:
            any_bakes_needed = True
        classified_materials.append((mat, cls))

    # Bake procedural sockets via Cycles if needed
    saved_cycles: Optional[Dict[str, Any]] = None
    if any_bakes_needed:
        saved_cycles = _configure_cycles_for_baking(scene, bake_resolution=bake_resolution)

    materials_manifest: List[Dict[str, Any]] = []
    try:
        total_mats = max(1, len(classified_materials))
        for idx, (mat, cls) in enumerate(classified_materials):
            p_base = 0.45 + 0.40 * (idx / total_mats)
            p_span = 0.40 / total_mats
            baked_list = _bake_material_channels(
                mat,
                cls,
                output_dir=output_dir,
                bake_resolution=bake_resolution,
                progress_base=p_base,
                progress_span=p_span,
                pristine=pristine.get(mat.name),
            )
            translucency = _translucency_weight(mat)
            if translucency > 0.01:
                cls.channels["translucency"] = {"value": round(translucency, 4)}
            if "subsurface" not in cls.channels:
                subsurface = _subsurface_from_mixes(mat)
                if subsurface is not None:
                    cls.channels["subsurface"] = subsurface
            disp_entry = cls.channels.get("displacement")
            if isinstance(disp_entry, dict):
                # Cycles' default "BUMP" never moves geometry; true displacement there is wrong.
                disp_entry["method"] = _displacement_method(mat)
            blend_method =_resolve_blend_method(mat, cls.channels, cls.blend_method)
            materials_manifest.append(
                {
                    "name": mat.name,
                    "blend_method": blend_method,
                    "channels": cls.channels,
                    "uv": cls.uv,
                    "baked_channels": baked_list,
                    "warnings": cls.warnings,
                }
            )
    finally:
        if saved_cycles is not None:
            _restore_scene_settings(scene, saved_cycles)

    emit_progress(0.88, "Exporting geometry.fbx")
    fbx_path = os.path.join(output_dir, "geometry.fbx")
    export_geometry_fbx(fbx_path, only_lights=only_lights)

    emit_progress(0.96, "Writing kitbridge.json manifest")
    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "asset": {
            "id": asset_id or asset_base_id or "unknown",
            "base_id": asset_base_id or asset_id or "unknown",
            "name": asset_name or os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "Asset",
            "type": asset_type,
            "resolution": resolution_label,
            "author": str(asset_meta.get("author") or ""),
            "license": str(asset_meta.get("license") or "royalty_free"),
            "source_url": str(asset_meta.get("source_url") or ""),
        },
        "units": {
            "meters_per_unit": 1.0,
            "up_axis": "Y",
        },
        "geometry": "geometry.fbx",
        "objects": objects_manifest,
        "materials": materials_manifest,
        "lights": lights_manifest,
    }

    manifest_path = os.path.join(output_dir, "kitbridge.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    emit_progress(1.0, "Export finished")
    emit_done(manifest_path)
    return manifest_path


def _parse_cli_params() -> Dict[str, Any]:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("Missing '--' separator in Blender command line arguments.")
    tail = argv[argv.index("--") + 1 :]
    if not tail:
        raise RuntimeError("Missing params.json path after '--'.")
    params_path = tail[-1]
    with open(params_path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError("params.json root must be a JSON object.")
    return data


def main() -> int:
    try:
        params = _parse_cli_params()
        run_export(params)
        return 0
    except Exception as exc:
        traceback.print_exc(file=sys.stdout)
        emit_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
