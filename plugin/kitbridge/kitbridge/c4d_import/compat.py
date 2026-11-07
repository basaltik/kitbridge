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

"""Cinema 4D / Maxon API compatibility and runtime description resolver (Milestone 2b).

Targets Cinema 4D 2023.0+ (Python 3.9+) using only the low-level ``maxon``
node graph API (no ``maxon.GraphDescription``).

Also centralizes runtime parameter & string resolution by walking
``obj.GetDescription(c4d.DESCFLAGS_DESC_NONE)`` and Maxon port metadata so
renderer- or version-specific parameter IDs (FBX importer prefs, Redshift
colorspace string, Redshift Dome texture param, Redshift Object tag
displacement params, Arnold Parameters tag opaque param, Arnold Skydome
type/texture params) are resolved and logged at runtime rather than guessed.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

try:
    import c4d  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - outside Cinema 4D
    c4d = None  # type: ignore[assignment]

try:
    import maxon  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - outside Cinema 4D
    maxon = None  # type: ignore[assignment]

log = logging.getLogger("kitbridge")

# In-memory audit log of runtime parameter resolutions for ImportResult & c4d_selftest.py
_RESOLUTION_LOG: List[Dict[str, Any]] = []


def clear_resolution_log() -> None:
    """Clear the runtime parameter resolution log before an import."""
    _RESOLUTION_LOG.clear()


def get_resolution_log() -> List[Dict[str, Any]]:
    """Return a copy of all runtime parameter resolution entries recorded so far."""
    return list(_RESOLUTION_LOG)


def document_render_space(doc: Any = None) -> str:
    """Name of the document's OCIO render space, or "" under basic colour management."""
    if c4d is None:
        return ""
    try:
        target = doc if doc is not None else c4d.documents.GetActiveDocument()
        if target is None or target[c4d.DOCUMENT_COLOR_MANAGEMENT] != c4d.DOCUMENT_COLOR_MANAGEMENT_OCIO:
            return ""
        name = target[c4d.DOCUMENT_OCIO_RENDER_COLORSPACE_NAME]
        if name:
            return str(name)
        # A fresh document can report no name yet; these are the indices of the built-in config.
        builtin = ("ACEScg", "ACES2065-1", "scene-linear Rec.709-sRGB", "scene-linear DCI-P3 D65", "scene-linear Rec.2020")
        index = target[c4d.DOCUMENT_OCIO_RENDER_COLORSPACE]
        return builtin[index] if isinstance(index, int) and 0 <= index < len(builtin) else ""
    except (AttributeError, TypeError):
        return ""


def record_resolution(context: str, details: Dict[str, Any]) -> None:
    """Log and store a runtime parameter/port resolution record."""
    entry: Dict[str, Any] = {"context": context}
    entry.update(details)
    _RESOLUTION_LOG.append(entry)
    log.info("[runtime-resolve] %s: %s", context, details)


# ── Version Helpers ──────────────────────────────────────────────────────────


def has_c4d() -> bool:
    """Return ``True`` when running inside Cinema 4D with ``c4d`` available."""
    return c4d is not None


def has_maxon() -> bool:
    """Return ``True`` when the ``maxon`` module is available."""
    return maxon is not None


def get_c4d_version() -> int:
    """Return ``c4d.GetC4DVersion()`` (e.g. ``2027000`` or ``2023100``), or ``0`` outside C4D."""
    if c4d is None:
        return 0
    try:
        return int(c4d.GetC4DVersion())
    except Exception:
        return 0


def get_c4d_major_version() -> int:
    """Return the 4-digit Cinema 4D release year (e.g. ``2023``..``2027``)."""
    ver = get_c4d_version()
    if ver <= 0:
        return 0
    if ver >= 2000000:
        return ver // 1000
    if ver >= 20000:
        return ver // 10
    return ver


def get_python_version() -> Tuple[int, int, int]:
    """Return ``(major, minor, micro)`` Python version tuple."""
    vi = sys.version_info
    return (int(vi[0]), int(vi[1]), int(vi[2]))


# ── Runtime Description Lookup Helper ────────────────────────────────────────


@dataclass
class ResolvedParam:
    """A Cinema 4D parameter resolved at runtime from ``obj.GetDescription()``."""

    context: str
    param_id: Any
    int_id: Optional[int] = None
    ident: str = ""
    name: str = ""
    dtype: Optional[int] = None
    source: str = "description"
    cycle: Dict[int, str] = field(default_factory=dict)
    cycle_symbols: Dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context,
            "int_id": self.int_id,
            "param_id": str(self.param_id),
            "ident": self.ident,
            "name": self.name,
            "dtype": self.dtype,
            "source": self.source,
            "cycle": dict(self.cycle) if self.cycle else {},
            "cycle_symbols": dict(self.cycle_symbols) if self.cycle_symbols else {},
        }


def _extract_desc_cycle(bc: Any, key: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if c4d is None or bc is None:
        return out
    try:
        sub = bc.GetContainer(key)
        if sub is not None:
            for cid, val in sub:
                if isinstance(cid, int):
                    out[int(cid)] = str(val)
    except Exception:
        pass
    return out


def iter_description_entries(obj: Any) -> List[Dict[str, Any]]:
    """Walk ``obj.GetDescription(c4d.DESCFLAGS_DESC_NONE)`` and return structured entries."""
    entries: List[Dict[str, Any]] = []
    if c4d is None or obj is None or not hasattr(obj, "GetDescription"):
        return entries
    try:
        desc = obj.GetDescription(c4d.DESCFLAGS_DESC_NONE)
        if desc is None:
            return entries
        desc_name_id = getattr(c4d, "DESC_NAME", 1)
        desc_ident_id = getattr(c4d, "DESC_IDENT", 999)
        desc_cycle_id = getattr(c4d, "DESC_CYCLE", 12)
        desc_cyclesym_id = getattr(c4d, "DESC_CYCLESYMBOLS", 29)

        for bc, paramid, groupid in desc:
            try:
                name = str(bc[desc_name_id] or "")
            except Exception:
                name = ""
            try:
                ident = str(bc[desc_ident_id] or "")
            except Exception:
                ident = ""

            int_id: Optional[int] = None
            dtype: Optional[int] = None
            depth = 0
            sub_ids: List[int] = []
            try:
                depth = int(paramid.GetDepth())
                if depth > 0:
                    int_id = int(paramid[0].id)
                    dtype = int(paramid[0].dtype)
                    sub_ids = [int(paramid[i].id) for i in range(depth)]
            except Exception:
                pass

            cycle = _extract_desc_cycle(bc, desc_cycle_id)
            cycle_syms = _extract_desc_cycle(bc, desc_cyclesym_id)
            entries.append(
                {
                    "param_id": paramid,
                    "group_id": groupid,
                    "int_id": int_id,
                    "sub_ids": sub_ids,
                    "depth": depth,
                    "dtype": dtype,
                    "name": name,
                    "ident": ident,
                    "cycle": cycle,
                    "cycle_symbols": cycle_syms,
                }
            )
    except Exception as exc:
        log.debug("GetDescription traversal failed on %r: %s", obj, exc)
    return entries


def dump_object_description(
    obj: Any,
    *,
    filter_keywords: Sequence[str] = (),
    max_entries: int = 80,
) -> List[Dict[str, Any]]:
    """Return a JSON-serializable summary of ``obj.GetDescription()`` for debugging."""
    raw_entries = iter_description_entries(obj)
    kw_lower = [k.lower() for k in filter_keywords if k]
    out: List[Dict[str, Any]] = []
    for item in raw_entries:
        ident = item["ident"]
        name = item["name"]
        if kw_lower:
            hay = f"{ident} {name}".lower()
            if not any(k in hay for k in kw_lower):
                continue
        out.append(
            {
                "int_id": item["int_id"],
                "sub_ids": item["sub_ids"],
                "dtype": item["dtype"],
                "ident": ident,
                "name": name,
                "cycle": item["cycle"] if item["cycle"] else None,
            }
        )
        if len(out) >= max_entries:
            break
    return out


def resolve_description_param(
    obj: Any,
    *,
    context: str,
    idents: Sequence[str] = (),
    names: Sequence[str] = (),
    fallback_c4d_symbols: Sequence[str] = (),
    fallback_id: Optional[int] = None,
    exclude_groups: bool = True,
    prefer_dtypes: Sequence[int] = (),
    id_range: Optional[Tuple[int, int]] = None,
) -> Optional[ResolvedParam]:
    """Resolve a parameter on *obj* at runtime by walking ``obj.GetDescription()``.

    Matching priority:
    1. Exact case-insensitive match on ``bc[c4d.DESC_IDENT]`` against *idents*.
    2. Exact case-insensitive match on ``bc[c4d.DESC_NAME]`` against *names*.
    3. Substring case-insensitive match on ``bc[c4d.DESC_IDENT]`` against *idents*.
    4. Substring case-insensitive match on ``bc[c4d.DESC_NAME]`` against *names*.
    5. Fallback symbol on ``c4d`` module (*fallback_c4d_symbols*), then *fallback_id*.
    """
    dtype_group = getattr(c4d, "DTYPE_GROUP", 1) if c4d is not None else 1
    entries = iter_description_entries(obj)

    def _is_candidate(e: Dict[str, Any]) -> bool:
        if exclude_groups and e["dtype"] == dtype_group:
            return False
        if id_range is not None and e["int_id"] is not None:
            lo, hi = id_range
            if not (lo <= e["int_id"] <= hi):
                return False
        return True

    candidates = [e for e in entries if _is_candidate(e)]
    idents_l = [i.strip().lower() for i in idents if i and i.strip()]
    names_l = [n.strip().lower() for n in names if n and n.strip()]
    pref_set = set(prefer_dtypes)

    def _pick_best(matches: List[Dict[str, Any]], source_tag: str) -> Optional[ResolvedParam]:
        if not matches:
            return None
        if pref_set:
            preferred = [m for m in matches if m["dtype"] in pref_set]
            if preferred:
                matches = preferred
        # Prefer top-level (depth 1) parameters unless only compound sub-params matched
        depth1 = [m for m in matches if m["depth"] == 1]
        chosen = depth1[0] if depth1 else matches[0]
        res = ResolvedParam(
            context=context,
            param_id=chosen["param_id"],
            int_id=chosen["int_id"],
            ident=chosen["ident"],
            name=chosen["name"],
            dtype=chosen["dtype"],
            source=source_tag,
            cycle=chosen["cycle"],
            cycle_symbols=chosen["cycle_symbols"],
        )
        record_resolution(context, res.to_dict())
        return res

    # Pass 1: exact DESC_IDENT match
    for target_ident in idents_l:
        matched = [e for e in candidates if e["ident"].strip().lower() == target_ident]
        res = _pick_best(matched, "description:exact_ident")
        if res is not None:
            return res

    # Pass 2: exact DESC_NAME match
    for target_name in names_l:
        matched = [e for e in candidates if e["name"].strip().lower() == target_name]
        res = _pick_best(matched, "description:exact_name")
        if res is not None:
            return res

    # Pass 3: substring DESC_IDENT match
    for target_ident in idents_l:
        matched = [e for e in candidates if target_ident in e["ident"].strip().lower()]
        res = _pick_best(matched, "description:substr_ident")
        if res is not None:
            return res

    # Pass 4: substring DESC_NAME match
    for target_name in names_l:
        matched = [e for e in candidates if target_name in e["name"].strip().lower()]
        res = _pick_best(matched, "description:substr_name")
        if res is not None:
            return res

    # Pass 5: fallback symbols on c4d module
    if c4d is not None:
        for sym in fallback_c4d_symbols:
            val = getattr(c4d, sym, None)
            if isinstance(val, int):
                # Check if that int_id was actually in entries (even if group)
                by_id = [e for e in entries if e["int_id"] == val]
                if by_id:
                    chosen = by_id[0]
                    res = ResolvedParam(
                        context=context,
                        param_id=chosen["param_id"],
                        int_id=val,
                        ident=chosen["ident"] or sym,
                        name=chosen["name"],
                        dtype=chosen["dtype"],
                        source="c4d_symbol+description",
                        cycle=chosen["cycle"],
                        cycle_symbols=chosen["cycle_symbols"],
                    )
                else:
                    res = ResolvedParam(
                        context=context,
                        param_id=val,
                        int_id=val,
                        ident=sym,
                        name="",
                        dtype=None,
                        source="c4d_symbol",
                    )
                record_resolution(context, res.to_dict())
                return res

    if fallback_id is not None:
        by_id = [e for e in entries if e["int_id"] == fallback_id]
        if by_id:
            chosen = by_id[0]
            res = ResolvedParam(
                context=context,
                param_id=chosen["param_id"],
                int_id=fallback_id,
                ident=chosen["ident"],
                name=chosen["name"],
                dtype=chosen["dtype"],
                source="fallback_id+description",
                cycle=chosen["cycle"],
                cycle_symbols=chosen["cycle_symbols"],
            )
        else:
            res = ResolvedParam(
                context=context,
                param_id=fallback_id,
                int_id=fallback_id,
                ident="",
                name="",
                dtype=None,
                source="fallback_id",
            )
        record_resolution(context, res.to_dict())
        return res

    record_resolution(
        context,
        {
            "found": False,
            "searched_idents": list(idents),
            "searched_names": list(names),
            "available_sample": dump_object_description(obj, max_entries=25),
        },
    )
    return None


def resolve_cycle_enum(
    resolved: Optional[ResolvedParam],
    *,
    context: str,
    symbols: Sequence[str] = (),
    labels: Sequence[str] = (),
    fallback_c4d_symbols: Sequence[str] = (),
    fallback_value: Optional[int] = None,
) -> Optional[int]:
    """Resolve an integer enum choice from a parameter's ``DESC_CYCLE`` / ``DESC_CYCLESYMBOLS``."""
    syms_l = [s.strip().lower() for s in symbols if s and s.strip()]
    lbls_l = [l.strip().lower() for l in labels if l and l.strip()]

    if resolved is not None:
        # 1. Exact match in cycle_symbols or cycle labels
        for target in syms_l:
            for enum_int, sym_str in resolved.cycle_symbols.items():
                if sym_str.strip().lower() == target:
                    record_resolution(
                        context,
                        {"enum_value": enum_int, "symbol": sym_str, "source": "cycle_symbols:exact"},
                    )
                    return enum_int
        for target in lbls_l:
            for enum_int, lbl_str in resolved.cycle.items():
                if lbl_str.strip().lower() == target:
                    record_resolution(
                        context,
                        {"enum_value": enum_int, "label": lbl_str, "source": "cycle:exact"},
                    )
                    return enum_int

        # 2. Substring match in cycle_symbols or cycle labels
        for target in syms_l:
            for enum_int, sym_str in resolved.cycle_symbols.items():
                if target in sym_str.strip().lower():
                    record_resolution(
                        context,
                        {"enum_value": enum_int, "symbol": sym_str, "source": "cycle_symbols:substr"},
                    )
                    return enum_int
        for target in lbls_l:
            for enum_int, lbl_str in resolved.cycle.items():
                if target in lbl_str.strip().lower():
                    record_resolution(
                        context,
                        {"enum_value": enum_int, "label": lbl_str, "source": "cycle:substr"},
                    )
                    return enum_int

    if c4d is not None:
        for csym in fallback_c4d_symbols:
            val = getattr(c4d, csym, None)
            if isinstance(val, int):
                record_resolution(
                    context,
                    {"enum_value": int(val), "symbol": csym, "source": "c4d_symbol"},
                )
                return int(val)

    if fallback_value is not None:
        record_resolution(
            context,
            {
                "enum_value": int(fallback_value),
                "source": "fallback_value",
                "available_cycle": dict(resolved.cycle) if resolved else {},
                "available_symbols": dict(resolved.cycle_symbols) if resolved else {},
            },
        )
        return int(fallback_value)

    record_resolution(
        context,
        {
            "enum_value": None,
            "available_cycle": dict(resolved.cycle) if resolved else {},
            "available_symbols": dict(resolved.cycle_symbols) if resolved else {},
        },
    )
    return None


def set_resolved_parameter(obj: Any, resolved: Optional[ResolvedParam], value: Any) -> bool:
    """Safely set *value* on *obj* for a :class:`ResolvedParam`."""
    if c4d is None or obj is None or resolved is None:
        return False
    flags = getattr(c4d, "DESCFLAGS_SET_NONE", getattr(c4d, "DESCFLAGS_SET_0", 0))
    for key in (resolved.param_id, resolved.int_id):
        if key is None:
            continue
        try:
            if hasattr(obj, "SetParameter"):
                ok = obj.SetParameter(key, value, flags)
                if ok is not False:
                    return True
        except Exception:
            pass
        try:
            obj[key] = value
            return True
        except Exception:
            pass
    return False


# ── Low-Level Maxon Node Graph Helpers (C4D 2023.0 – 2027) ───────────────────


def is_valid_graph_obj(obj: Any) -> bool:
    """Return ``True`` if a ``maxon`` GraphModelRef or GraphNode is non-null and valid."""
    if obj is None:
        return False
    for method_name in ("IsValid", "IsPopulated"):
        fn = getattr(obj, method_name, None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                pass
    fn_null = getattr(obj, "IsNullValue", None)
    if callable(fn_null):
        try:
            return not bool(fn_null())
        except Exception:
            pass
    return True


def create_or_get_default_graph(mat: Any, nodespace_id: str) -> Any:
    """Create (or fetch) the default node graph for *nodespace_id* on *mat*.

    Uses only ``GetNodeMaterialReference()`` and ``CreateDefaultGraph`` / ``GetGraph``
    (compatible with C4D 2023.0+; never uses ``maxon.GraphDescription``).
    """
    if c4d is None or maxon is None or mat is None:
        raise RuntimeError("Cinema 4D / maxon API is unavailable.")

    node_mat = mat.GetNodeMaterialReference()
    if node_mat is None:
        raise RuntimeError(f"Material {mat.GetName()!r} returned None for GetNodeMaterialReference().")

    ns_id = maxon.Id(nodespace_id)
    graph: Any = None

    has_space = False
    if hasattr(node_mat, "HasSpace"):
        try:
            has_space = bool(node_mat.HasSpace(ns_id))
        except Exception:
            has_space = False

    if not has_space and hasattr(node_mat, "CreateDefaultGraph"):
        try:
            graph = node_mat.CreateDefaultGraph(ns_id)
        except Exception as exc:
            log.debug("CreateDefaultGraph(%s) raised: %s", nodespace_id, exc)

    if not is_valid_graph_obj(graph) and hasattr(node_mat, "GetGraph"):
        try:
            graph = node_mat.GetGraph(ns_id)
        except Exception as exc:
            log.debug("GetGraph(%s) raised: %s", nodespace_id, exc)

    if not is_valid_graph_obj(graph) and hasattr(node_mat, "CreateEmptyGraph"):
        try:
            graph = node_mat.CreateEmptyGraph(ns_id)
        except Exception as exc:
            log.debug("CreateEmptyGraph(%s) raised: %s", nodespace_id, exc)
            if hasattr(node_mat, "GetGraph"):
                try:
                    graph = node_mat.GetGraph(ns_id)
                except Exception:
                    pass

    if not is_valid_graph_obj(graph):
        raise RuntimeError(f"Failed to create or retrieve node graph for nodespace {nodespace_id!r}.")
    return graph


def get_material_graph(mat: Any, nodespace_id: str) -> Any:
    """Retrieve an existing node graph for *nodespace_id* on *mat* without resetting it."""
    if c4d is None or maxon is None or mat is None:
        return None
    try:
        node_mat = mat.GetNodeMaterialReference()
        if node_mat is None:
            return None
        ns_id = maxon.Id(nodespace_id)
        if hasattr(node_mat, "HasSpace") and not node_mat.HasSpace(ns_id):
            return None
        if hasattr(node_mat, "GetGraph"):
            graph = node_mat.GetGraph(ns_id)
            if is_valid_graph_obj(graph):
                return graph
    except Exception:
        pass
    return None


# Name used by ``c4d_import.__init__`` when inspecting reused materials.
get_existing_graph = get_material_graph


def list_graph_nodes(graph: Any) -> List[Any]:
    """Return all top-level shader ``GraphNode`` children in *graph*."""
    nodes: List[Any] = []
    if maxon is None or not is_valid_graph_obj(graph):
        return nodes
    try:
        root = graph.GetViewRoot() if hasattr(graph, "GetViewRoot") else graph.GetRoot()
        node_kind = getattr(getattr(maxon, "NODE_KIND", None), "NODE", 1)
        try:
            root.GetChildren(nodes, node_kind)
        except TypeError:
            res = root.GetChildren(node_kind)
            if res:
                nodes = list(res)
    except Exception as exc:
        log.debug("list_graph_nodes failed: %s", exc)
    return nodes


def get_node_asset_id_str(node: Any) -> str:
    """Extract the asset/type identifier string for a ``GraphNode``."""
    if not is_valid_graph_obj(node):
        return ""
    for attr_key in ("net.maxon.node.attribute.assetid",):
        try:
            val = node.GetValue(attr_key)
            if val is not None:
                return str(val)
        except Exception:
            pass
    try:
        return str(node.GetId())
    except Exception:
        return ""


def find_nodes_by_asset_id(graph: Any, asset_id_str: str) -> List[Any]:
    """Find all nodes in *graph* matching *asset_id_str* (C4D 2023–2027 compatible)."""
    if maxon is None or not is_valid_graph_obj(graph):
        return []

    found: List[Any] = []
    helper = getattr(maxon, "GraphModelHelper", None)
    if helper is not None and hasattr(helper, "FindNodesByAssetId"):
        try:
            aid = maxon.Id(asset_id_str)
            ret = helper.FindNodesByAssetId(graph, aid, True, found)
            if isinstance(ret, list) and ret and not found:
                found = list(ret)
            if found:
                return found
        except Exception:
            pass

    # Fallback: manual walk over graph children checking assetid / node id
    target_lower = asset_id_str.strip().lower()
    for n in list_graph_nodes(graph):
        aid_str = get_node_asset_id_str(n).lower()
        if target_lower in aid_str:
            found.append(n)
    return found


def add_shader_node(graph: Any, asset_id_str: str, node_id_str: str = "") -> Any:
    """Add a shader node with *asset_id_str* to *graph* inside an active transaction."""
    if maxon is None:
        raise RuntimeError("maxon API is unavailable.")
    child_id = maxon.Id(node_id_str) if node_id_str else maxon.Id()
    node = graph.AddChild(child_id, maxon.Id(asset_id_str))
    if not is_valid_graph_obj(node):
        raise RuntimeError(f"AddChild failed for node asset ID {asset_id_str!r}.")
    return node


def find_port(
    node: Any,
    port_path: Union[str, Sequence[str]],
    *,
    is_input: bool = True,
) -> Any:
    """Locate an input or output port (including nested bundle sub-ports) on *node*."""
    if maxon is None or not is_valid_graph_obj(node):
        return None

    ports_root = node.GetInputs() if is_input else node.GetOutputs()
    if not is_valid_graph_obj(ports_root):
        return None

    if isinstance(port_path, str):
        # 1. Direct lookup on ports_root (handles both flat IDs and "output.r")
        try:
            direct = ports_root.FindChild(maxon.Id(port_path))
            if is_valid_graph_obj(direct):
                return direct
        except Exception:
            pass
        try:
            direct = ports_root.FindChild(port_path)
            if is_valid_graph_obj(direct):
                return direct
        except Exception:
            pass

        # 2. If port_path contains '.', try splitting the last segment (e.g. "output" -> "r")
        if "." in port_path:
            parent_str, child_str = port_path.rsplit(".", 1)
            try:
                parent_port = ports_root.FindChild(maxon.Id(parent_str))
                if is_valid_graph_obj(parent_port):
                    sub = parent_port.FindChild(maxon.Id(child_str))
                    if is_valid_graph_obj(sub):
                        return sub
            except Exception:
                pass
        return None

    # Sequence of nested port IDs, e.g. ("com.redshift3d...tex0", "path")
    cur = ports_root
    for part in port_path:
        if not is_valid_graph_obj(cur):
            return None
        next_port = None
        try:
            next_port = cur.FindChild(maxon.Id(part))
        except Exception:
            pass
        if not is_valid_graph_obj(next_port):
            try:
                next_port = cur.FindChild(part)
            except Exception:
                next_port = None
        cur = next_port
    return cur if is_valid_graph_obj(cur) else None


def _port_value_methods(port: Any) -> Tuple[str, str]:
    """Return ``(setter_name, getter_name)`` for *port* on this Cinema 4D build.

    2024.4 introduced ``GraphNode.SetPortValue`` / ``GetPortValue`` and deprecated
    ``SetDefaultValue`` / ``GetDefaultValue`` (which overwrite the port *default* rather
    than its value on newer builds). Prefer the new pair whenever the port exposes it;
    2023.x - 2024.3 only have the legacy pair.
    """
    if callable(getattr(port, "SetPortValue", None)):
        return "SetPortValue", "GetPortValue"
    return "SetDefaultValue", "GetDefaultValue"


def port_set_value(port: Any, val: Any) -> bool:
    """Write *val* to *port* via ``SetPortValue`` (2024.4+) or ``SetDefaultValue`` (older).

    Returns ``False`` (instead of raising) when the port is invalid, no setter exists,
    or the setter rejects the value type, so callers can retry with another wrapper type.
    """
    if port is None:
        return False
    setter_name, _getter_name = _port_value_methods(port)
    fn = getattr(port, setter_name, None)
    if not callable(fn):
        return False
    try:
        fn(val)
        return True
    except Exception as exc:
        log.debug("%s(%r) rejected on %r: %s", setter_name, val, port, exc)
        return False


def port_get_value(port: Any, default: Any = None) -> Any:
    """Read *port*'s value via ``GetPortValue`` (2024.4+) or ``GetDefaultValue`` (older)."""
    if port is None:
        return default
    _setter_name, getter_name = _port_value_methods(port)
    fn = getattr(port, getter_name, None)
    if not callable(fn):
        return default
    try:
        return fn()
    except Exception as exc:
        log.debug("%s() failed on %r: %s", getter_name, port, exc)
        return default


def set_port_value(port: Any, value: Any) -> bool:
    """Set a scalar, boolean, string, 2D vector, or RGB color on *port*."""
    if not is_valid_graph_obj(port):
        return False

    # RGB tuple/list -> maxon.Color / ColorA / Vector
    if isinstance(value, (list, tuple)) and len(value) == 3:
        r, g, b = float(value[0]), float(value[1]), float(value[2])
        if maxon is not None:
            for ctor_name in ("Color", "Color32", "Vector"):
                ctor = getattr(maxon, ctor_name, None)
                if callable(ctor):
                    try:
                        if port_set_value(port, ctor(r, g, b)):
                            return True
                    except Exception:
                        pass
            ctor_a = getattr(maxon, "ColorA", None)
            if callable(ctor_a):
                try:
                    if port_set_value(port, ctor_a(r, g, b, 1.0)):
                        return True
                except Exception:
                    pass
        if c4d is not None and hasattr(c4d, "Vector"):
            try:
                if port_set_value(port, c4d.Vector(r, g, b)):
                    return True
            except Exception:
                pass
        return port_set_value(port, (r, g, b))

    # 2D tuple/list -> maxon.Vector2d / Vector
    if isinstance(value, (list, tuple)) and len(value) == 2:
        u, v = float(value[0]), float(value[1])
        if maxon is not None:
            for ctor_name in ("Vector2d", "Vector2d32"):
                ctor = getattr(maxon, ctor_name, None)
                if callable(ctor):
                    try:
                        if port_set_value(port, ctor(u, v)):
                            return True
                    except Exception:
                        pass
            ctor_v3 = getattr(maxon, "Vector", None)
            if callable(ctor_v3):
                try:
                    if port_set_value(port, ctor_v3(u, v, 0.0)):
                        return True
                except Exception:
                    pass
        return port_set_value(port, (u, v))

    if port_set_value(port, value):
        return True

    if isinstance(value, str) and maxon is not None:
        for ctor_name in ("String", "Id"):
            ctor = getattr(maxon, ctor_name, None)
            if callable(ctor):
                try:
                    if port_set_value(port, ctor(value)):
                        return True
                except Exception:
                    pass
    return False


def set_port_file_path(port: Any, abs_path: str) -> bool:
    """Set a texture file path on *port*, trying ``maxon.Url`` first then ``str``."""
    if not is_valid_graph_obj(port) or not abs_path:
        return False

    if maxon is not None and hasattr(maxon, "Url"):
        try:
            url_obj = maxon.Url(abs_path)
            if port_set_value(port, url_obj):
                return True
        except Exception:
            pass

    if port_set_value(port, str(abs_path)):
        return True

    if maxon is not None and hasattr(maxon, "String"):
        try:
            if port_set_value(port, maxon.String(str(abs_path))):
                return True
        except Exception:
            pass
    return False


def connect_ports(src_out_port: Any, dst_in_port: Any) -> bool:
    """Connect *src_out_port* -> *dst_in_port* inside a graph transaction."""
    if not is_valid_graph_obj(src_out_port) or not is_valid_graph_obj(dst_in_port):
        return False
    try:
        src_out_port.Connect(dst_in_port)
        return True
    except Exception as exc:
        log.debug("Port Connect(%r -> %r) failed: %s", src_out_port, dst_in_port, exc)
        return False


def resolve_redshift_colorspace(
    cs_port: Any,
    desired: str,
) -> str:
    """Resolve and log the Redshift colorspace string for *desired* (``'srgb'`` or ``'raw'``).

    Inspects *cs_port*'s current/default value and ``c4d`` module constants at
    runtime, logging what was found before returning the resolved identifier.
    """
    norm_desired = (desired or "srgb").strip().lower()
    port_default: Any = None
    if is_valid_graph_obj(cs_port):
        port_default = port_get_value(cs_port)

    c4d_rs_constants: Dict[str, Any] = {}
    if c4d is not None:
        for attr in dir(c4d):
            if "COLORSPACE" in attr.upper() and ("RS_" in attr.upper() or "REDSHIFT" in attr.upper()):
                try:
                    c4d_rs_constants[attr] = getattr(c4d, attr)
                except Exception:
                    pass

    if norm_desired == "raw":
        resolved = str(c4d_rs_constants.get("RS_INPUT_COLORSPACE_RAW") or "RS_INPUT_COLORSPACE_RAW")
    else:
        resolved = str(c4d_rs_constants.get("RS_INPUT_COLORSPACE_SRGB") or "sRGB")

    record_resolution(
        f"redshift.colorspace.{norm_desired}",
        {
            "desired": norm_desired,
            "resolved": resolved,
            "port_default": str(port_default) if port_default is not None else None,
            "c4d_rs_constants": c4d_rs_constants,
        },
    )
    return resolved
