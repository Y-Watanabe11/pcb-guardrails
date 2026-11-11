"""Rebuild copper connectivity from Gerber + drill only, and compare it with the schematic netlist.

    OPEN   one schematic net whose pads sit on two or more separate pieces of copper
    SHORT  one piece of copper carrying pads of two or more schematic nets
    NO-COPPER  a pad with no copper under it in the fab files (missing layer, shifted export, deleted pad)

Plated holes join copper on every layer they pass through. The CAD tool's own connectivity engine is never
consulted: the point is to check what the fab will build, not what the design database believes.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .gerber import FabFiles, Hole, parse_drill, parse_gerber
from .kicad import Netlist, Pad, copper_name


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[a] = b


@dataclass
class Copper:
    """All copper objects of a board, clustered into electrically connected pieces."""
    layers: list[str]                       # KiCad names, top first
    items: list[tuple[str, object]]         # (layer, geometry)
    holes: list[Hole]
    uf: _UF
    trees: dict[str, tuple[STRtree, list[int], list]] = field(default_factory=dict)

    @property
    def hole_base(self) -> int:
        return len(self.items)

    def cluster(self, i: int) -> int:
        return self.uf.find(i)


def build_copper(files: FabFiles) -> Copper:
    n = len(files.copper)
    items: list[tuple[str, object]] = []
    for num, _side, path in files.copper:
        name = copper_name(num, n)
        items.extend((name, g) for g in parse_gerber(path).objects)
    holes = [h for p in files.drills for h in parse_drill(p)]
    uf = _UF(len(items) + len(holes))
    by_layer = collections.defaultdict(list)
    for i, (l, _) in enumerate(items):
        by_layer[l].append(i)
    cu = Copper([copper_name(c[0], n) for c in files.copper], items, holes, uf)
    for l, idx in by_layer.items():
        geoms = [items[i][1] for i in idx]
        tree = STRtree(geoms)
        cu.trees[l] = (tree, idx, geoms)
        for k, gk in enumerate(geoms):
            for j in tree.query(gk, predicate="intersects"):
                if j != k:
                    uf.union(idx[k], idx[j])
    for h_i, h in enumerate(holes):
        if not h.plated:
            continue
        for tree, idx, _ in cu.trees.values():
            for j in tree.query(h.geom, predicate="intersects"):
                uf.union(cu.hole_base + h_i, idx[j])
    return cu


def _pad_cluster(cu: Copper, pad: Pad) -> int | None:
    pt = Point(pad.xy)
    if pad.kind == "thru_hole":
        best = None
        for h_i, h in enumerate(cu.holes):
            if h.plated:
                d = h.geom.distance(pt)
                if d < 0.05 and (best is None or d < best[0]):
                    best = (d, h_i)
        return cu.cluster(cu.hole_base + best[1]) if best else None
    layer = next((l for l in pad.layers if l in cu.trees), None)
    if layer is None:
        return None
    tree, idx, _ = cu.trees[layer]
    hits = [idx[j] for j in tree.query(pt, predicate="intersects")]
    if not hits:
        hits = [idx[j] for j in tree.query(pt.buffer(0.05), predicate="intersects")]
    return cu.cluster(hits[0]) if hits else None


@dataclass
class NetcheckResult:
    pads: int
    nets: int
    opens: list[dict]
    shorts: list[dict]
    no_copper: list[dict]
    not_in_netlist: list[str]
    islands: list[dict]
    per_net: list[dict]
    clearance: dict[str, dict]

    @property
    def ok(self) -> bool:
        return not (self.opens or self.shorts or self.no_copper)

    def to_dict(self) -> dict:
        return {"ok": self.ok, **{k: v for k, v in self.__dict__.items()}}


def run_netcheck(files: FabFiles, pads: list[Pad], netlist: Netlist,
                 clearance_report: float = 0.2) -> NetcheckResult:
    cu = build_copper(files)
    for p in pads:
        p.net = netlist.pins.get((p.ref, p.pin))
        if p.net is None and p.ref in netlist.refs:
            p.net = f"unconnected-({p.name})"  # a no-connect pin of a schematic part: must sit alone on its copper
    clus = collections.defaultdict(list)
    no_copper, pad_c = [], {}
    for p in pads:
        c = _pad_cluster(cu, p)
        if c is None:
            no_copper.append({"pad": p.name, "net": p.net, "layers": list(p.layers), "xy_kicad": [round(p.xy[0], 3), round(-p.xy[1], 3)]})
            continue
        pad_c[id(p)] = c
        clus[c].append(p)

    def real(net):
        return net and not net.startswith("unconnected-")

    net_clusters = collections.defaultdict(set)
    for p in pads:
        if id(p) in pad_c and real(p.net):
            net_clusters[p.net].add(pad_c[id(p)])
    opens = []
    for net, cs in sorted(net_clusters.items()):
        if len(cs) > 1:
            groups = [sorted(q.name for q in clus[c] if q.net == net) for c in cs]
            opens.append({"net": net, "groups": sorted(groups)})
    shorts = []
    for c, pl in clus.items():
        nets = set()
        for q in pl:
            if q.net is None:
                continue  # parts absent from the schematic (logos, PCB-only mounting pads): reported separately
            nets.add(q.net if real(q.net) else f"unconnected:{q.name}")
        if len(nets) > 1:
            shorts.append({"nets": sorted(nets), "pads": sorted(q.name for q in pl)})
    not_in_netlist = sorted({p.name for p in pads if p.net is None})

    # copper islands without any pad (often harmless; reported, not failed)
    pad_clusters = set(clus)
    area = collections.defaultdict(float)
    lay = collections.defaultdict(set)
    for i, (l, g) in enumerate(cu.items):
        c = cu.cluster(i)
        if c not in pad_clusters:
            area[c] += g.area
            lay[c].add(l)
    islands = []
    for c, a in sorted(area.items(), key=lambda kv: -kv[1])[:20]:
        b = unary_union([g for i, (_, g) in enumerate(cu.items) if cu.cluster(i) == c]).bounds
        islands.append({"area_mm2": round(a, 3), "layers": sorted(lay[c]),
                        "bbox_kicad": [round(b[0], 2), round(-b[3], 2), round(b[2], 2), round(-b[1], 2)]})

    # per-net table
    layer_of = collections.defaultdict(set)
    for i, (l, _) in enumerate(cu.items):
        layer_of[cu.cluster(i)].add(l)
    per_net = []
    for net in sorted({p.net for p in pads if real(p.net)}):
        pl = [p for p in pads if p.net == net]
        cs = {pad_c.get(id(p)) for p in pl}
        others = {q.net for c in cs for q in clus.get(c, []) if q.net != net and q.net is not None}
        status = ("NO-COPPER" if None in cs else "OPEN" if len(cs) > 1 else "SHORT" if others else "OK")
        per_net.append({"net": net, "pads": len(pl), "copper_pieces": len(cs - {None}),
                        "layers": sorted(set().union(*[layer_of.get(c, set()) for c in cs if c is not None])),
                        "status": status, "pad_list": sorted(p.name for p in pl)})

    # smallest gap between different copper pieces, per layer (informational)
    net_of = {c: "/".join(sorted({q.net or "?" for q in pl})) for c, pl in clus.items()}
    clearance = {}
    for l, (tree, idx, geoms) in cu.trees.items():
        best, below = None, 0
        seen = set()
        for k, gk in enumerate(geoms):
            rk = cu.cluster(idx[k])
            for j in tree.query(gk.buffer(clearance_report + 0.05)):
                if j <= k:
                    continue
                rj = cu.cluster(idx[j])
                if rj == rk:
                    continue
                d = gk.distance(geoms[j])
                if best is None or d < best["mm"]:
                    pz = gk.boundary.interpolate(gk.boundary.project(geoms[j].centroid))
                    best = {"mm": round(d, 4), "between": sorted([net_of.get(rk, "island"), net_of.get(rj, "island")]),
                            "at_kicad": [round(pz.x, 2), round(-pz.y, 2)]}
                key = (rk, rj) if rk < rj else (rj, rk)
                if d < clearance_report - 1e-6 and key not in seen:
                    seen.add(key)
                    below += 1
        clearance[l] = {"min": best, "pairs_below_report_threshold": below, "report_threshold_mm": clearance_report}

    return NetcheckResult(len(pads), len(net_clusters), opens, shorts, no_copper, not_in_netlist, islands, per_net, clearance)
