"""Tracks and vias whose copper touches a pad of a different net, read from the ``.kicad_pcb`` itself.

``netcheck`` finds the resulting short in the Gerbers. This check finds it earlier, in the design database, and
names the track and pad: the typical case is a track end placed on the *neighbouring* pad of the intended part
(for example after a footprint was mirrored onto the back side), which DRC does not always flag.

Everything here is in KiCad's frame (mm, y down). Pad angles in the file are absolute (they include the
footprint's rotation); pad positions are relative to the footprint.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import sexpdata
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .gerber import UnsupportedInput
from .kicad import _S, _kids

EPS_AREA = 1e-9   # mm2; copper overlap below this is treated as no contact


@dataclass
class BoardPad:
    ref: str
    number: str
    net: str
    layers: frozenset[str] | None      # None = every copper layer (plated through hole)
    shape: object                      # shapely polygon, board frame
    net_tie_group: frozenset[str] = frozenset()   # pad numbers joined by a net-tie footprint

    @property
    def name(self) -> str:
        return f"{self.ref}.{self.number}"


@dataclass
class Track:
    kind: str                          # segment / arc / via
    net: str
    layers: frozenset[str]
    shape: object
    ends: list[tuple[float, float]]
    width: float


@dataclass
class WrongNetResult:
    pads: int
    tracks: int
    vias: int
    hits: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict:
        return {"ok": self.ok, "pads": self.pads, "tracks": self.tracks, "vias": self.vias, "hits": self.hits}


def _num(v) -> float:
    return float(_S(v))


def _xy(node) -> tuple[float, float]:
    return _num(node[1]), _num(node[2])


def _net_resolver(tree):
    decl = {}
    for n in _kids(tree, "net"):
        if len(n) >= 3 and isinstance(_S(n[1]), int):
            decl[_S(n[1])] = str(n[2])

    def net_of(node) -> str:
        ns = _kids(node, "net")
        if not ns or len(ns[0]) < 2:
            return ""
        a = _S(ns[0][1])
        if isinstance(a, int):  # KiCad <= 9: (net 3 "GND") or (net 3)
            return str(ns[0][2]) if len(ns[0]) >= 3 else decl.get(a, "")
        return str(a)            # KiCad 10: (net "GND")
    return net_of


def copper_order(tree) -> list[str]:
    """Copper layer names top to bottom, from the board's layer table."""
    names = []
    for lay in _kids(tree, "layers"):
        for entry in lay[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and str(_S(entry[1])).endswith(".Cu"):
                names.append(str(_S(entry[1])))
    inner = sorted((n for n in names if n.startswith("In")), key=lambda n: int(n[2:-3]))
    return ["F.Cu", *inner, "B.Cu"] if names else ["F.Cu", "B.Cu"]


def _expand_layers(names: list[str]) -> frozenset[str] | None:
    out = set()
    for n in names:
        if n == "*.Cu":
            return None
        if n == "F&B.Cu":
            out |= {"F.Cu", "B.Cu"}
        elif n.endswith(".Cu"):
            out.add(n)
    return frozenset(out)


def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad rotation (counter-clockwise on screen, y down)."""
    t = math.radians(deg)
    return x * math.cos(t) + y * math.sin(t), -x * math.sin(t) + y * math.cos(t)


def _local_shape(p, where: str):
    """Pad copper in the pad's own frame (unrotated, centred on the pad position)."""
    shape = _S(p[3])
    size = _kids(p, "size")
    w, h = (_num(size[0][1]), _num(size[0][2])) if size else (0.0, 0.0)
    if shape == "circle":
        return Point(0, 0).buffer(w / 2, 32)
    if shape in ("rect", "chamfered_rect"):   # chamfers only remove copper; the full rectangle is conservative
        return box(-w / 2, -h / 2, w / 2, h / 2)
    if shape == "roundrect":
        rr = _kids(p, "roundrect_rratio")
        r = min(w, h) * (_num(rr[0][1]) if rr else 0.25)
        core = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r) if r > 0 else box(-w / 2, -h / 2, w / 2, h / 2)
        return core.buffer(r, 16) if r > 0 else core
    if shape == "oval":
        if w >= h:
            return LineString([(-(w - h) / 2, 0), ((w - h) / 2, 0)]).buffer(h / 2, 32)
        return LineString([(0, -(h - w) / 2), (0, (h - w) / 2)]).buffer(w / 2, 32)
    if shape == "trapezoid":
        # ASSUMPTION: not modelled exactly; the rectangle grown by the deltas contains the trapezoid, so this can only
        # over-report contact, never miss it.
        d = _kids(p, "rect_delta")
        dx, dy = (abs(_num(d[0][1])), abs(_num(d[0][2]))) if d else (0.0, 0.0)
        return box(-(w + dy) / 2, -(h + dx) / 2, (w + dy) / 2, (h + dx) / 2)
    if shape == "custom":
        opts = _kids(p, "options")
        anchor = "circle"
        for o in opts:
            for a in _kids(o, "anchor"):
                anchor = str(_S(a[1]))
        parts = [box(-w / 2, -h / 2, w / 2, h / 2) if anchor == "rect" else Point(0, 0).buffer(w / 2, 32)]
        for prims in _kids(p, "primitives"):
            for g in prims[1:]:
                parts.append(_primitive(g, where))
        return unary_union(parts)
    raise UnsupportedInput(f"{where}: pad shape {shape!r} is not supported")


def _primitive(g, where: str):
    kind = _S(g[0])
    wd = _kids(g, "width") or _kids(g, "stroke")
    width = 0.0
    if wd:
        width = _num(wd[0][1]) if _S(wd[0][0]) == "width" else _num(_kids(wd[0], "width")[0][1])
    fill = any(str(_S(f[1])) in ("yes", "solid") for f in _kids(g, "fill"))
    if kind == "gr_poly":
        poly = Polygon([_xy(pt) for pt in _kids(_kids(g, "pts")[0], "xy")]).buffer(0)
        return poly.buffer(width / 2, 16) if width > 0 else poly
    if kind == "gr_line":
        return LineString([_xy(_kids(g, "start")[0]), _xy(_kids(g, "end")[0])]).buffer(max(width, 1e-6) / 2, 16)
    if kind == "gr_circle":
        c, e = _xy(_kids(g, "center")[0]), _xy(_kids(g, "end")[0])
        r = math.dist(c, e)
        disk = Point(c).buffer(r + width / 2, 32)
        return disk if fill or r <= width / 2 else disk.difference(Point(c).buffer(r - width / 2, 32))
    if kind == "gr_rect":
        (x1, y1), (x2, y2) = _xy(_kids(g, "start")[0]), _xy(_kids(g, "end")[0])
        r = box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        return r.buffer(width / 2, 16) if width > 0 else r
    raise UnsupportedInput(f"{where}: custom pad primitive {kind!r} is not supported")


def load_board(path: str | Path) -> tuple[list[BoardPad], list[Track], list[str]]:
    tree = sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    net_of = _net_resolver(tree)
    copper = copper_order(tree)
    pads: list[BoardPad] = []
    for fp in _kids(tree, "footprint") + _kids(tree, "module"):
        ref = next((str(p[2]) for p in _kids(fp, "property") if p[1] == "Reference"), None)
        if ref is None:
            ref = next((str(t[2]) for t in _kids(fp, "fp_text") if _S(t[1]) == "reference"), "?")
        at = _kids(fp, "at")[0]
        fx, fy = _num(at[1]), _num(at[2])
        fa = _num(at[3]) if len(at) > 3 else 0.0
        groups = [frozenset(str(s).split(",")) for t in _kids(fp, "net_tie_pad_groups") for s in t[1:]]
        for p in _kids(fp, "pad"):
            number, kind = str(p[1]), _S(p[2])
            if kind == "np_thru_hole":
                continue
            where = f"{ref}.{number}"
            for dr in _kids(p, "drill"):
                for off in _kids(dr, "offset"):
                    if _num(off[1]) or _num(off[2]):
                        raise UnsupportedInput(f"{where}: pad with a drill offset is not supported")
            pa = _kids(p, "at")[0]
            ox, oy = _rot(_num(pa[1]), _num(pa[2]), fa)
            ang = _num(pa[3]) if len(pa) > 3 else 0.0
            local = _local_shape(p, where)
            # KiCad's angle is counter-clockwise on screen with y down, i.e. clockwise in shapely's y-up sense
            shape = translate(rotate(local, -ang, origin=(0, 0)), fx + ox, fy + oy)
            names = [str(_S(v)) for v in _kids(p, "layers")[0][1:]] if _kids(p, "layers") else []
            layers = None if kind == "thru_hole" else _expand_layers(names)
            group = next((g for g in groups if number in g), frozenset())
            pads.append(BoardPad(ref, number, net_of(p), layers, shape, group))
    tracks: list[Track] = []
    for s in _kids(tree, "segment"):
        a, b = _xy(_kids(s, "start")[0]), _xy(_kids(s, "end")[0])
        w = _num(_kids(s, "width")[0][1])
        shape = LineString([a, b]).buffer(w / 2, 16) if a != b else Point(a).buffer(w / 2, 16)
        tracks.append(Track("segment", net_of(s), frozenset([str(_S(_kids(s, "layer")[0][1]))]), shape, [a, b], w))
    for s in _kids(tree, "arc"):
        a, m, b = (_xy(_kids(s, k)[0]) for k in ("start", "mid", "end"))
        w = _num(_kids(s, "width")[0][1])
        tracks.append(Track("arc", net_of(s), frozenset([str(_S(_kids(s, "layer")[0][1]))]),
                            LineString(_arc3(a, m, b)).buffer(w / 2, 16), [a, b], w))
    for v in _kids(tree, "via"):
        c = _xy(_kids(v, "at")[0])
        size = _num(_kids(v, "size")[0][1])
        ends = [str(_S(x)) for x in _kids(v, "layers")[0][1:]] if _kids(v, "layers") else ["F.Cu", "B.Cu"]
        span = [x for x in ends if x in copper]
        if len(span) == 2:
            i, j = sorted(copper.index(x) for x in span)
            span = copper[i:j + 1]
        tracks.append(Track("via", net_of(v), frozenset(span), Point(c).buffer(size / 2, 32), [c], size))
    return pads, tracks, copper


def _arc3(a, m, b, step=math.pi / 32):
    (x1, y1), (x2, y2), (x3, y3) = a, m, b
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [a, b]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.dist((ux, uy), a)
    t1, t2, t3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))
    span = (t3 - t1) % (2 * math.pi)
    if (t2 - t1) % (2 * math.pi) > span:   # the arc goes the other way round
        span -= 2 * math.pi
    k = max(4, int(abs(span) / step))
    return [(ux + r * math.cos(t1 + span * i / k), uy + r * math.sin(t1 + span * i / k)) for i in range(k + 1)]


def run_wrongnet(path: str | Path) -> WrongNetResult:
    pads, tracks, copper = load_board(path)
    tree = STRtree([p.shape for p in pads])
    res = WrongNetResult(len(pads), sum(t.kind != "via" for t in tracks), sum(t.kind == "via" for t in tracks))
    seen = set()
    for t in tracks:
        for i in tree.query(t.shape):
            pad = pads[int(i)]
            if pad.net == t.net:
                continue
            if pad.net_tie_group and any(q.ref == pad.ref and q.number in pad.net_tie_group and q.net == t.net for q in pads):
                continue   # a net-tie footprint joins these nets on purpose
            shared = t.layers if pad.layers is None else t.layers & pad.layers
            if not shared:
                continue
            overlap = t.shape.intersection(pad.shape)
            if overlap.area <= EPS_AREA:
                continue
            at_end = [e for e in t.ends if Point(e).buffer(t.width / 2, 16).intersection(pad.shape).area > EPS_AREA]
            c = overlap.representative_point()
            where = at_end[0] if at_end else (c.x, c.y)
            key = (t.kind, t.net, pad.name, round(where[0], 4), round(where[1], 4))
            if key in seen:
                continue
            seen.add(key)
            res.hits.append({"item": t.kind, "item_net": t.net or "(no net)", "pad": pad.name, "pad_net": pad.net or "(no net)",
                             "layers": sorted(shared, key=copper.index) if all(x in copper for x in shared) else sorted(shared),
                             "where": "end" if at_end or t.kind == "via" else "along",
                             "at_kicad": [round(where[0], 4), round(where[1], 4)]})
    res.hits.sort(key=lambda h: (h["pad"], h["item_net"], h["at_kicad"]))
    return res
