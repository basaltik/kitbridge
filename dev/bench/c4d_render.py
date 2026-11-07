"""Kitbridge fidelity benchmark — the Cinema 4D side. Runs INSIDE Cinema 4D (Script Manager or MCP).

For each package: new document → Redshift, linear Rec.709 / un-tone-mapped sRGB output → the bench
HDRI as a dome that lights but is invisible to the camera → import the package with Kitbridge →
the same camera as blender_render.py → render with alpha → c4d_<renderer>.png. compare.py puts both
images on the same grey background, so the background never has to match here.

    import sys; sys.path[:0] = [r"<repo>\\plugin\\kitbridge", r"<repo>\\dev\\bench"]
    import c4d_render; c4d_render.main([pkg_dir], out_root, hdr_package)

Python 3.9 syntax (C4D 2023).
"""

from __future__ import annotations

import json
import math
import os
import struct
import traceback
import zlib
from typing import List, Optional, Tuple

import c4d
from c4d import documents

HFOV_DEG = 39.6          # 50 mm on a 36 mm sensor, same as blender_render.py
FILL = 0.9               # bounding sphere fills 90 % of the frame
SIZE = 512

# Document colour management (C4D 2024+, OCIO). Values are indices of the cycles in that version;
# they are looked up by label below so a reordered list in another version still works.
_OCIO = {
    "DOCUMENT_OCIO_RENDER_COLORSPACE": "scene-linear Rec.709-sRGB",
    "DOCUMENT_OCIO_VIEW_TRANSFORM": "Un-tone-mapped",
    "DOCUMENT_OCIO_DISPLAY_COLORSPACE": "sRGB",
}
RS_VIDEOPOST = 1036219
RS_LIGHT = 1036751
RS_LIGHT_CAMERA_CONTRIBUTION = 10042   # REDSHIFT_LIGHT_CAMERA_RAY_CONTRIBUTION_SCALE
RS_DENOISE_ENABLED = 2006              # REDSHIFT_RENDERER_DENOISE_ENABLED
RS_DENOISE_ENGINE = 2007               # REDSHIFT_RENDERER_DENOISE_ENGINE (4 = OIDN)


def frame_direction() -> c4d.Vector:
    """Blender (1, -1, 0.6) mapped into C4D. Keep identical to blender_render.frame_direction.

    Kitbridge's FBX import puts Blender (x, y, z) at C4D (x, z, y): Blender's front (-Y) becomes
    C4D's front (-Z). Checked on an asymmetric asset (whisky bottle + glass): (x, z, -y) showed
    the back of the scene.
    """
    return blender_to_c4d(c4d.Vector(1.0, -1.0, 0.6)).GetNormalized()


def blender_to_c4d(v: c4d.Vector) -> c4d.Vector:
    return c4d.Vector(v.x, v.z, v.y)


def _walk(obj):
    while obj:
        yield obj
        for child in _walk(obj.GetDown()):
            yield child
        obj = obj.GetNext()


def _bbox(doc) -> Tuple[c4d.Vector, c4d.Vector]:
    lo = c4d.Vector(1e30)
    hi = c4d.Vector(-1e30)
    found = False
    for obj in _walk(doc.GetFirstObject()):
        if not isinstance(obj, c4d.PolygonObject) or obj.GetPointCount() == 0:
            continue
        found = True
        mg = obj.GetMg()
        for p in obj.GetAllPoints():
            w = mg * p
            lo = c4d.Vector(min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z))
            hi = c4d.Vector(max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z))
    if not found:
        raise RuntimeError("the import produced no polygon objects")
    return lo, hi


def _set_cycle_by_label(node, ident: str, label: str) -> Optional[str]:
    """Set a cycle parameter by its visible label; return a note when it could not be set."""
    for bc, did, _ in node.GetDescription(c4d.DESCFLAGS_DESC_NONE):
        if str(bc[c4d.DESC_IDENT] or "") != ident:
            continue
        cycle = bc[c4d.DESC_CYCLE]
        for value, name in (cycle or []):
            if str(name) == label:
                node[did] = value
                return None
        return "%s has no option %r" % (ident, label)
    return "%s not found (older Cinema 4D without OCIO)" % ident


def _setup_document(doc) -> List[str]:
    notes = []
    for ident, label in _OCIO.items():
        note = _set_cycle_by_label(doc, ident, label)
        if note:
            notes.append(note)
    rd = doc.GetActiveRenderData()
    rd[c4d.RDATA_RENDERENGINE] = RS_VIDEOPOST
    vp = rd.GetFirstVideoPost()
    while vp is not None and vp.GetType() != RS_VIDEOPOST:
        vp = vp.GetNext()
    if vp is None:
        vp = documents.BaseVideoPost(RS_VIDEOPOST)
        rd.InsertVideoPost(vp)
    vp[RS_DENOISE_ENABLED] = True
    vp[RS_DENOISE_ENGINE] = 4
    rd[c4d.RDATA_XRES] = SIZE
    rd[c4d.RDATA_YRES] = SIZE
    rd[c4d.RDATA_FILMASPECT] = 1.0
    rd[c4d.RDATA_ALPHACHANNEL] = True
    rd[c4d.RDATA_SAVEIMAGE] = False
    return notes


DOME_HEADING_DEG = 0.0   # calibrated against Blender, see README ("HDRI orientation")


def _add_light(doc, hdr_package: str, heading_deg: float = DOME_HEADING_DEG) -> None:
    from kitbridge.c4d_import import import_package

    result = import_package(doc, hdr_package, renderer="redshift", options={"replace_existing_hdr": True})
    if result.errors:
        raise RuntimeError("HDR import failed: %s" % result.errors[0].get("error"))
    for obj in _walk(doc.GetFirstObject()):
        if obj.GetType() == RS_LIGHT:
            obj[RS_LIGHT_CAMERA_CONTRIBUTION] = 0.0   # lights the asset, never seen by the camera
            obj.SetAbsRot(c4d.Vector(math.radians(heading_deg), 0, 0))


def _add_camera(doc, lo: c4d.Vector, hi: c4d.Vector) -> c4d.BaseObject:
    centre = (lo + hi) * 0.5
    radius = (hi - lo).GetLength() * 0.5     # half diagonal, as in blender_render.py
    distance = radius / math.tan(math.radians(HFOV_DEG) / 2.0) / FILL
    cam = c4d.BaseObject(c4d.Ocamera)
    cam.SetName("Kitbridge bench camera")
    cam[c4d.CAMERA_FOCUS] = 36.0 / (2.0 * math.tan(math.radians(HFOV_DEG) / 2.0))  # 50 mm
    cam[c4d.CAMERAOBJECT_APERTURE] = 36.0
    cam.SetAbsPos(centre + frame_direction() * distance)
    cam.SetAbsRot(c4d.utils.VectorToHPB(centre - cam.GetAbsPos()))
    doc.InsertObject(cam)
    doc.GetActiveBaseDraw().SetSceneCamera(cam)
    return cam


def _srgb8(v: float) -> int:
    v = 0.0 if v <= 0.0 else (1.0 if v >= 1.0 else v)
    v = v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1.0 / 2.4) - 0.055
    return int(v * 255.0 + 0.5)


def _save_srgb_png(bmp, path: str) -> None:
    """Write the float render as an 8-bit sRGB RGBA PNG.

    RenderDocument hands back scene-linear pixels and BaseBitmap.Save writes them unconverted, so
    the display encoding (same formula as Blender's "Standard" view) is applied here. Pure stdlib:
    C4D's Python has no Pillow.
    """
    w, h = bmp.GetBw(), bmp.GetBh()
    alpha = bmp.GetInternalChannel()
    rgb_buf = bytearray(w * 12)
    raw = bytearray()
    for y in range(h):
        bmp.GetPixelCnt(0, y, w, memoryview(rgb_buf), 12, c4d.COLORMODE_RGBf, c4d.PIXELCNT_0)
        rgb = struct.unpack("<%df" % (w * 3), bytes(rgb_buf))
        raw.append(0)  # PNG filter type "None" for this scanline
        for x in range(w):
            a = bmp.GetAlphaPixel(alpha, x, y) if alpha is not None else 255
            raw += bytes((_srgb8(rgb[3 * x]), _srgb8(rgb[3 * x + 1]), _srgb8(rgb[3 * x + 2]), a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def render_one(package_dir: str, out_png: str, hdr_package: str, renderer: str = "redshift",
               dome_heading_deg: float = DOME_HEADING_DEG) -> dict:
    from kitbridge.c4d_import import import_package

    rec = {"package": package_dir, "out": out_png, "renderer": renderer}
    doc = documents.BaseDocument()
    try:
        rec["notes"] = _setup_document(doc)
        _add_light(doc, hdr_package, dome_heading_deg)
        result = import_package(doc, package_dir, renderer=renderer, options={"apply_to_selection": False})
        if result.errors:
            raise RuntimeError("import failed at %s: %s" % (result.errors[0].get("step"), result.errors[0].get("error")))
        rec["warnings"] = list(result.warnings)[:10]
        lo, hi = _bbox(doc)
        rec["bbox_c4d_cm"] = [[round(lo.x, 2), round(lo.y, 2), round(lo.z, 2)], [round(hi.x, 2), round(hi.y, 2), round(hi.z, 2)]]
        _add_camera(doc, lo, hi)
        rd = doc.GetActiveRenderData()
        rd[c4d.RDATA_FORMATDEPTH] = c4d.RDATA_FORMATDEPTH_32
        bmp = c4d.bitmaps.MultipassBitmap(SIZE, SIZE, c4d.COLORMODE_RGBf)
        bmp.AddChannel(True, True)   # alpha
        code = documents.RenderDocument(doc, rd.GetDataInstance(), bmp, c4d.RENDERFLAGS_EXTERNAL)
        if code != c4d.RENDERRESULT_OK:
            raise RuntimeError("RenderDocument returned %s" % code)
        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        _save_srgb_png(bmp, out_png)
        rec["ok"] = True
    except Exception as exc:
        rec.update({"ok": False, "error": str(exc), "traceback": traceback.format_exc()[-1500:]})
    finally:
        documents.KillDocument(doc)
    return rec


def main(package_dirs: List[str], out_root: str, hdr_package: str, renderer: str = "redshift") -> List[dict]:
    """Render each package to <out_root>/<base_id>/c4d_<renderer>.png; print one JSON line each."""
    records = []
    for package_dir in package_dirs:
        base_id = os.path.basename(os.path.normpath(package_dir)).rsplit("_", 1)[0]
        out_png = os.path.join(out_root, base_id, "c4d_%s.png" % renderer)
        rec = render_one(package_dir, out_png, hdr_package, renderer)
        print(json.dumps(rec))
        records.append(rec)
    return records
