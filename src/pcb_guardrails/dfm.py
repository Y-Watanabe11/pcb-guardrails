"""Measure the manufacturing-relevant minimums of the fab files, and optionally compare them with your fab's rules.

This module deliberately ships **no built-in fab rules**. Fab capabilities change and differ by layer count,
copper weight and service tier; a wrong number baked into a checker is worse than none. Copy
``examples/rules.example.json``, fill it from your fab's current capabilities page, and pass it with ``--rules``.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

from shapely import prepare
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, polygonize, unary_union
from shapely.strtree import STRtree

from .netcheck import build_copper

from .gerber import FabFiles, UnsupportedInput, parse_drill, parse_gerber
from .kicad import Pad, Via, copper_name


# The board edge is the centre line of the profile strokes: "the profile is the center line of the stroked line"
# (K. Tavernier, Ucamco, "The Gerber Guide, Chapter 3: The PCB Profile", The PCB Design Magazine, Oct. 2015).
OUTLINE_ARC_ERR_MM = 0.001   # arcs of the outline are followed to within 1 um
# ASSUMPTION: copper reaching up to 5 um past the edge is "at the edge", not outside it. This absorbs the polygon
# approximation of arcs by the CAD tool and by this parser. On the 20 public KiCad boards, pours stop within
# 0.5 um of the edge; real overhangs found there were 0.9 mm (castellated pads) and parts placed off the board.
EDGE_TOL_MM = 0.005
MIN_AREA_MM2 = 1e-6
# Outline segments whose end points are closer than half the stroke width meet: their drawn ends overlap, so the
# outline in the file is closed. CAD tools round an arc's end and the next line's start separately (gaps of 0.04 um
# and 3.5 um on two public KiCad boards). ASSUMPTION: 1 um for outlines drawn without width (regions).
JOIN_TOL_MM = 0.001


def _join_ends(lines, tol):
    """Move end points that lie within ``tol`` of each other onto one point, so the outline closes."""
    ends = [Point(c) for l in lines for c in (l.coords[0], l.coords[-1])]
    tree = STRtree(ends)
    parent = list(range(len(ends)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i, e in enumerate(ends):
        for j in tree.query(e.buffer(tol)):
            if e.distance(ends[j]) <= tol:
                parent[find(i)] = find(j)
    out = []
    for k, l in enumerate(lines):
        c = list(l.coords)
        c[0], c[-1] = ends[find(2 * k)].coords[0], ends[find(2 * k + 1)].coords[0]
        if len(c) > 2 or c[0] != c[-1]:
            out.append(LineString(c))
    return out


def board_outline(profile_path):
    """The board as a (multi)polygon: the area enclosed by the centre lines of the profile, minus cut-outs.

    Closed loops nested inside another loop alternate between board and cut-out (an island inside a cut-out is
    board again). A stray line splitting the board into two faces leaves both of them board."""
    layer = parse_gerber(profile_path, arc_max_err=OUTLINE_ARC_ERR_MM)
    lines = layer.lines
    if not lines:
        raise UnsupportedInput(f"{profile_path}: the profile has no lines or regions")
    tol = max([JOIN_TOL_MM] + [min(layer.stroke_widths) / 2] if layer.stroke_widths else [JOIN_TOL_MM])
    faces = list(polygonize(unary_union(_join_ends(lines, tol))))
    if not faces:
        raise UnsupportedInput(f"{profile_path}: could not build a closed board outline from its centre lines")
    shells = [Polygon(f.exterior) for f in faces]
    keep = []
    for i, f in enumerate(faces):
        rp = f.representative_point()
        depth = sum(1 for j, sh in enumerate(shells) if j != i and sh.contains(rp))
        if depth % 2 == 0:
            keep.append(f)
    return unary_union(keep)


def crossing_holes(board, holes) -> list:
    """Plated holes cut by the board edge (castellated holes)."""
    out = []
    for h in holes:
        if not h.plated or h.geom.is_empty:
            continue
        if h.geom.difference(board).area > MIN_AREA_MM2 and h.geom.intersection(board).area > MIN_AREA_MM2:
            out.append(h)
    return out


def edge_findings(board, pieces, cast_holes) -> tuple[list[dict], list]:
    """Copper of one layer beyond the board edge, as findings:

    off-board      a piece of copper with nothing on the board: it will not exist (a part placed off the board,
                   an outline drawn too small)
    over-edge      copper crossing the edge: the fab cuts through it
    castellated    copper beyond the edge around a plated hole the edge cuts: a castellated pad, intended
                   (copper beyond the edge in one piece with such a pad is taken as part of it)

    Also returns, per castellated pad, the disc around its hole that reaches as far as the pad goes beyond the edge:
    copper there sits on the edge by design and is left out of the copper-to-edge minimum."""
    inside = board.buffer(EDGE_TOL_MM)
    prepare(inside)
    tree = STRtree([h.geom for h in cast_holes]) if cast_holes else None
    out, zones = [], []
    for piece in pieces:
        if inside.contains(piece):
            continue
        beyond = piece.difference(inside)
        if beyond.area <= MIN_AREA_MM2:
            continue
        on_board = piece.intersection(board).area > MIN_AREA_MM2
        for part in getattr(beyond, "geoms", [beyond]):
            if part.area <= MIN_AREA_MM2:
                continue
            kind = "over-edge" if on_board else "off-board"
            hit = tree.query(part, predicate="intersects") if on_board and tree is not None else []
            if len(hit):
                kind = "castellated"
                for j in hit:
                    centre = cast_holes[j].geom.centroid
                    reach = max(centre.distance(Point(c)) for c in part.exterior.coords)
                    zones.append(centre.buffer(reach + EDGE_TOL_MM))
            far = max(board.distance(Point(c)) for c in part.exterior.coords)
            c = part.representative_point()
            out.append({"kind": kind, "area_mm2": round(part.area, 4), "beyond_edge_mm": round(far, 4),
                        "at_kicad": [round(c.x, 3), round(-c.y, 3)]})
            if not on_board:
                break   # the whole piece is off the board: one finding is enough
    return out, zones


@dataclass
class Measure:
    name: str
    value: float | None
    unit: str = "mm"
    where: object = None


def run_dfm(files: FabFiles, pads: list[Pad] | None = None, vias: list[Via] | None = None, gap_window: float = 0.5) -> dict:
    """Copper gaps are searched up to ``gap_window`` mm; ``None`` means no gap that small exists."""
    out: dict = {"layers": {}, "holes": {}, "vias": {}, "pads": {}, "silk": {}}
    board = None
    holes = [h for p in files.drills for h in parse_drill(p)]
    cast = []
    if files.profile:
        board = board_outline(files.profile)
        cast = crossing_holes(board, holes)
        out["board"] = {"outline_bbox_mm": [round(v, 3) for v in board.bounds], "area_mm2": round(board.area, 2)}
        out["edge"] = {"castellated_holes": len(cast), "layers": {}}
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
        if board is not None and allcu is not None:
            pieces = list(getattr(allcu, "geoms", [allcu]))
            found, zones = edge_findings(board, pieces, cast)
            out["edge"]["layers"][name] = found
            rest = allcu.difference(unary_union(zones)) if zones else allcu   # castellated pads excepted
            info["min_copper_to_edge_mm"] = round(rest.distance(board.boundary), 4) if not rest.is_empty else None
        out["layers"][name] = info

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
        if board is not None:
            cut = {id(h) for h in cast}
            rest = [h for h in holes if id(h) not in cut]
            out["holes"]["min_hole_to_edge_mm"] = round(min(h.geom.distance(board.boundary) for h in rest), 4) if rest else None
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
RULE_KEYS = {"min_trace_width_mm", "min_spacing_mm", "min_spacing_same_net_mm", "min_copper_to_edge_mm", "min_drill_mm",
             "min_hole_to_hole_mm", "min_hole_to_edge_mm", "min_via_annular_ring_mm", "min_pth_annular_ring_mm",
             "min_silk_line_mm"}
SPACING_KEYS = ("min_spacing_mm", "min_spacing_same_net_mm")


def active_rules(rules: dict) -> dict:
    """The rules that ask for a judgment: non-null values of non-comment keys."""
    return {k: v for k, v in rules.items() if not k.startswith("_") and v is not None}


def unjudgeable(rules: dict, files: FabFiles, have_pcb: bool) -> list[dict]:
    """Requested rules that cannot be judged with the inputs given. Skipping them silently would turn a requested
    judgment into a pass, so each is reported (and the check exits 2 unless it already failed)."""
    out = []
    for k in sorted(active_rules(rules)):
        need = None
        if k not in RULE_KEYS:
            need = f"unknown rule name (known: {', '.join(sorted(RULE_KEYS))})"
        elif k in ("min_copper_to_edge_mm", "min_hole_to_edge_mm") and not files.profile:
            need = "no board outline (Profile / Edge_Cuts) file"
        elif k in ("min_drill_mm", "min_hole_to_hole_mm", "min_hole_to_edge_mm") and not files.drills:
            need = "no drill file"
        elif k in ("min_via_annular_ring_mm", "min_pth_annular_ring_mm") and not have_pcb:
            need = "needs --pcb (via and pad sizes come from the board file)"
        elif k == "min_silk_line_mm" and not files.legend:
            need = "no silkscreen (Legend) file; set the rule to null if the board has none"
        if need:
            out.append({"rule": k, "reason": need})
    return out


def edge_problems(m: dict) -> list[dict]:
    """Copper outside the board: judged without fab rules, since it is wrong whatever the fab can do."""
    return [dict(f, layer=l) for l, fs in m.get("edge", {}).get("layers", {}).items()
            for f in fs if f["kind"] != "castellated"]


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
