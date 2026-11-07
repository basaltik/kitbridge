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

"""Cinema 4D active renderer detection and configuration (Milestone 2b)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import compat
from .compat import c4d

log = logging.getLogger("kitbridge")

RENDERER_REDSHIFT = "redshift"
RENDERER_ARNOLD = "arnold"
RENDERER_STANDARD = "standard"
VALID_RENDERERS = (RENDERER_REDSHIFT, RENDERER_ARNOLD, RENDERER_STANDARD)

# Known Cinema 4D VideoPost / Render Engine plugin IDs
VP_STANDARD_ID = 0
VP_PHYSICAL_ID = 1023342
VP_REDSHIFT_ID = 1036219
VP_ARNOLD_ID = 1029988
ARNOLD_GI_SPECULAR_DEPTH_ID = 2005875169  # C4DAIP_OPTIONS_GI_SPECULAR_DEPTH

RS_NODESPACE_ID = "com.redshift3d.redshift4c4d.class.nodespace"
ARNOLD_NODESPACE_ID = "com.autodesk.arnold.nodespace"


def normalise_renderer(renderer: Optional[str]) -> Optional[str]:
    """Normalise a user-supplied renderer name to ``'redshift'|'arnold'|'standard'`` or ``None``."""
    if renderer is None:
        return None
    clean = str(renderer).strip().lower()
    if not clean:
        return None
    if clean in ("rs", "redshift", "redshift4c4d"):
        return RENDERER_REDSHIFT
    if clean in ("arnold", "c4dtoa", "mtoa"):
        return RENDERER_ARNOLD
    if clean in ("standard", "physical", "legacy", "default"):
        return RENDERER_STANDARD
    return None


def renderer_installed(renderer: str) -> bool:
    """Whether Cinema 4D has the render engine loaded (Standard always is)."""
    vp_id = {RENDERER_REDSHIFT: VP_REDSHIFT_ID, RENDERER_ARNOLD: VP_ARNOLD_ID}.get(renderer)
    if vp_id is None:
        return renderer == RENDERER_STANDARD
    if c4d is None:
        return False
    return c4d.plugins.FindPlugin(vp_id, getattr(c4d, "PLUGINTYPE_VIDEOPOST", 6)) is not None


def detect_renderer(doc: Any = None) -> str:
    """Detect the active renderer for *doc*: ``'redshift'``, ``'arnold'``, or ``'standard'``."""
    if c4d is None:
        return RENDERER_STANDARD

    if doc is None:
        try:
            doc = c4d.documents.GetActiveDocument()
        except Exception:
            doc = None

    if doc is None:
        return RENDERER_STANDARD

    engine_id: Optional[int] = None
    plugin_name = ""
    try:
        rd = doc.GetActiveRenderData()
        if rd is not None:
            rdata_key = getattr(c4d, "RDATA_RENDERENGINE", 5300)
            raw_eng = rd[rdata_key]
            if isinstance(raw_eng, int):
                engine_id = int(raw_eng)
    except Exception as exc:
        log.debug("Failed to read RDATA_RENDERENGINE: %s", exc)

    if engine_id == VP_REDSHIFT_ID:
        return RENDERER_REDSHIFT
    if engine_id == VP_ARNOLD_ID:
        return RENDERER_ARNOLD

    if engine_id is not None and engine_id not in (VP_STANDARD_ID, VP_PHYSICAL_ID):
        try:
            plug_type = getattr(c4d, "PLUGINTYPE_VIDEOPOST", 6)
            plug = c4d.plugins.FindPlugin(engine_id, plug_type)
            if plug is not None and hasattr(plug, "GetName"):
                plugin_name = str(plug.GetName() or "").lower()
                if "redshift" in plugin_name:
                    return RENDERER_REDSHIFT
                if "arnold" in plugin_name:
                    return RENDERER_ARNOLD
        except Exception:
            pass

    return RENDERER_STANDARD


def set_document_renderer(doc: Any, renderer: str) -> str:
    """Configure *doc*'s active RenderData to use *renderer* (used by selftest)."""
    target = normalise_renderer(renderer) or RENDERER_STANDARD
    if c4d is None or doc is None:
        return target

    try:
        rd = doc.GetActiveRenderData()
        if rd is None:
            return RENDERER_STANDARD

        rdata_key = getattr(c4d, "RDATA_RENDERENGINE", 5300)
        if target == RENDERER_REDSHIFT:
            vp_id = VP_REDSHIFT_ID
        elif target == RENDERER_ARNOLD:
            vp_id = VP_ARNOLD_ID
        else:
            rd[rdata_key] = VP_STANDARD_ID
            return RENDERER_STANDARD

        rd[rdata_key] = vp_id

        # Ensure corresponding VideoPost node exists on the active RenderData
        has_vp = False
        vp = rd.GetFirstVideoPost() if hasattr(rd, "GetFirstVideoPost") else None
        while vp is not None:
            if vp.GetType() == vp_id:
                has_vp = True
                break
            vp = vp.GetNext()

        if not has_vp and hasattr(c4d, "documents") and hasattr(c4d.documents, "BaseVideoPost"):
            try:
                new_vp = c4d.documents.BaseVideoPost(vp_id)
                if new_vp is not None:
                    rd.InsertVideoPost(new_vp)
            except Exception as exc:
                log.debug("InsertVideoPost(%d) raised: %s", vp_id, exc)

        compat.record_resolution(
            "renderer.set_document_renderer",
            {
                "requested": target,
                "vp_id": vp_id,
                "detected_after": detect_renderer(doc),
            },
        )
    except Exception as exc:
        log.warning("set_document_renderer(%r) failed: %s", renderer, exc)

    return detect_renderer(doc)


def arnold_specular_depth(doc: Any) -> Optional[int]:
    """GI Specular depth of the active render settings, or ``None`` when Arnold is not set up."""
    try:
        rd = doc.GetActiveRenderData()
        vp = rd.GetFirstVideoPost()
        while vp is not None:
            if vp.GetType() == VP_ARNOLD_ID:
                return int(vp[ARNOLD_GI_SPECULAR_DEPTH_ID])
            vp = vp.GetNext()
    except Exception:
        pass
    return None
