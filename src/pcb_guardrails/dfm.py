"""Measure the manufacturing-relevant minimums of the fab files, and optionally compare them with your fab's rules.

This module deliberately ships **no built-in fab rules**. Fab capabilities change and differ by layer count,
copper weight and service tier; a wrong number baked into a checker is worse than none. Copy
``examples/rules.example.json``, fill it from your fab's current capabilities page, and pass it with ``--rules``.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

from shapely.geometry import LineString
from shapely.ops import nearest_points, polygonize, unary_union
from shapely.strtree import STRtree

from .netcheck import build_copper

from .gerber import FabFiles, UnsupportedInput, parse_drill, parse_gerber
from .kicad import Pad, Via, copper_name


def board_outline(profile_path):
    edge = unary_union(parse_gerber(profile_path).objects)
    geoms = edge.geoms if hasattr(edge, "geoms") else [edge]
    rings = []
    for g in geoms:
        rings.append(LineString(list(g.exterior.coords)))
        rings.extend(LineString(list(i.coords)) for i in g.interiors)
    polys = list(polygonize(unary_union(rings)))
    if not polys:
        raise UnsupportedInput(f"{profile_path}: could not build a closed board outline")
    outer = max(polys, key=lambda p: p.area)
    # cut-outs: polygons fully inside the outer one that are not the stroke itself
    holes = [p for p in polys if p is not outer and outer.contains(p.representative_point())]
    # each stroked line yields thin slivers; keep only cut-outs larger than 1 mm^2
    holes = [h for h in holes if h.area > 1.0]
    board = outer
    for h in holes:
        board = board.difference(h)
    return outer, board


@dataclass
class Measure:
    name: str
    value: float | None
    unit: str = "mm"
    where: object = None


def run_dfm(files: FabFiles, pads: list[Pad] | None = None, vias: list[Via] | None = None, gap_window: float = 0.5) -> dict:
    """Copper gaps are searched up to ``gap_window`` mm; ``None`` means no gap that small exists."""
    out: dict = {"layers": {}, "holes": {}, "vias": {}, "pads": {}, "silk": {}}
    outer = board = None
    if files.profile:
        outer, board = board_outline(files.profile)
        out["board"] = {"outline_bbox_mm": [round(v, 3) for v in outer.bounds], "area_mm2": round(board.area, 2)}
    n = len(files.copper)
    cu = build_copper(files)  # connectivity through plated holes tells "different conductor" from "same net elsewhere"
    for num, _side, path in files.copper:
        layer = parse_gerber(path)
        name = copper_name(num, n)
        tree, idx, objs = cu.trees.get(name, (None, [], []))
        local = list(range(len(objs)))

        def find(a):
            while local[a] != a:
                local[a] = local[local[a]]
                a = local[a]
            return a

        for k, gk in enumerate(objs):
            for j in tree.query(gk, predicate="intersects"):
                a, b = find(k), find(j)
                if a != b:
                    local[a] = b
        diff = same = None
        for k, gk in enumerate(objs):
            for j in tree.query(gk.buffer(gap_window)):
                if j <= k or find(j) == find(k):
                    continue
                d = gk.distance(objs[j])
                other = cu.cluster(idx[k]) != cu.cluster(idx[j])
                if other and (diff is None or d < diff[0]):
                    diff = (d, k, j)
                elif not other and (same is None or d < same[0]):
                    same = (d, k, j)

        def where(t):
            if t is None:
                return None
            p1, p2 = nearest_points(objs[t[1]], objs[t[2]])
            return [round((p1.x + p2.x) / 2, 3), round(-(p1.y + p2.y) / 2, 3)]

        allcu = unary_union(objs) if objs else None
        info = {
            "min_trace_width_mm": min(layer.stroke_widths) if layer.stroke_widths else None,
            "trace_widths_mm": {str(w): c for w, c in sorted(layer.stroke_widths.items())},
            "min_gap_between_conductors_mm": round(diff[0], 4) if diff else None,
            "min_gap_between_conductors_at_kicad": where(diff),
            "min_gap_within_conductor_mm": round(same[0], 4) if same else None,
            "min_gap_within_conductor_at_kicad": where(same),
            "gap_search_window_mm": gap_window,
        }
        if outer is not None and allcu is not None:
            info["min_copper_to_edge_mm"] = round(allcu.distance(outer.exterior), 4)
            info["copper_outside_outline_mm2"] = round(allcu.difference(outer.buffer(1e-3)).area, 4)
        out["layers"][name] = info

    holes = [h for p in files.drills for h in parse_drill(p)]
    sizes = collections.Counter((round(h.diameter, 3), "PTH" if h.plated else "NPTH") for h in holes)
    out["holes"]["sizes"] = [{"d_mm": d, "type": t, "count": c} for (d, t), c in sorted(sizes.items())]
    if holes:
        tree = STRtree([h.geom for h in holes])
        hh = None
        for i, h in enumerate(holes):
            for j in tree.query(h.geom.buffer(1.0)):
                if j <= i:
                    continue
                d = h.geom.distance(holes[j].geom)
                if hh is None or d < hh[0]:
                    hh = (d, [round(v, 3) for v in (*h.xy, *holes[j].xy)])
        out["holes"]["min_hole_to_hole_mm"] = round(hh[0], 4) if hh else None
        out["holes"]["min_hole_to_hole_at"] = hh[1] if hh else None
        out["holes"]["min_drill_mm"] = min(h.diameter for h in holes)
        if outer is not None:
            out["holes"]["min_hole_to_edge_mm"] = round(min(h.geom.distance(outer.exterior) for h in holes), 4)
    if vias:
        vc = collections.Counter((v.size, v.drill) for v in vias)
        out["vias"] = [{"size_mm": s, "drill_mm": d, "annular_ring_mm": round((s - d) / 2, 4), "count": c}
                       for (s, d), c in sorted(vc.items())]
    if pads:
        rings = collections.defaultdict(set)
        for p in pads:
            if p.kind != "thru_hole" or not p.size or not p.drill:
                continue
            ring = min(p.size[0] - p.drill[0], p.size[1] - p.drill[1]) / 2
            rings[round(ring, 4)].add(p.ref)
        out["pads"]["thru_hole_annular_rings"] = [{"ring_mm": r, "refs": sorted(v)} for r, v in sorted(rings.items())]
    for side, path in files.legend.items():
        w = parse_gerber(path).stroke_widths
        out["silk"][side] = {"min_line_width_mm": min(w) if w else None}
    return out


# ----------------------------------------------------------------------------- rules
def apply_rules(m: dict, rules: dict) -> list[dict]:
    """Compare measurements with user-supplied minimums. Returns a list of violations."""
    v = []

    def chk(what, val, key):
        lim = rules.get(key)
        if lim is not None and val is not None and val < lim - 1e-9:
            v.append({"check": what, "value_mm": val, "rule": key, "min_mm": lim})

    for l, info in m["layers"].items():
        chk(f"{l} trace width", info.get("min_trace_width_mm"), "min_trace_width_mm")
        chk(f"{l} gap between conductors", info.get("min_gap_between_conductors_mm"), "min_spacing_mm")
        chk(f"{l} gap within a conductor", info.get("min_gap_within_conductor_mm"), "min_spacing_same_net_mm")
        chk(f"{l} copper to edge", info.get("min_copper_to_edge_mm"), "min_copper_to_edge_mm")
        if info.get("copper_outside_outline_mm2", 0) > 0.01:
            v.append({"check": f"{l} copper outside outline", "value_mm2": info["copper_outside_outline_mm2"]})
    h = m.get("holes", {})
    chk("drill", h.get("min_drill_mm"), "min_drill_mm")
    chk("hole to hole", h.get("min_hole_to_hole_mm"), "min_hole_to_hole_mm")
    chk("hole to edge", h.get("min_hole_to_edge_mm"), "min_hole_to_edge_mm")
    for via in m.get("vias") or []:
        chk(f"via {via['size_mm']}/{via['drill_mm']} ring", via["annular_ring_mm"], "min_via_annular_ring_mm")
    for r in m.get("pads", {}).get("thru_hole_annular_rings", []):
        chk(f"PTH pad ring ({', '.join(r['refs'][:6])})", r["ring_mm"], "min_pth_annular_ring_mm")
    for side, s in m.get("silk", {}).items():
        chk(f"silk {side} line width", s.get("min_line_width_mm"), "min_silk_line_mm")
    return v
