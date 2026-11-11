"""Gerber (RS-274X / X2) and Excellon drill parsing into shapely geometry.

Supports the subset that KiCad and most modern CAD tools emit. Anything outside that subset raises
``UnsupportedInput`` instead of being silently skipped: a checker that ignores what it does not
understand will report a broken board as clean.

All returned coordinates are millimetres in the Gerber frame (y up).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from shapely.affinity import rotate, scale, translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

ARC_STEP = math.pi / 32


class UnsupportedInput(ValueError):
    """The input uses a feature this tool does not implement. Never treat it as a pass."""


# ----------------------------------------------------------------------------- helpers
_SAFE_EXPR = re.compile(r"^[0-9.+\-*/() ]*$")


def _ev(expr: str, params: dict[int, float]) -> float:
    e = re.sub(r"\$(\d+)", lambda m: repr(params.get(int(m.group(1)), 0.0)), re.sub(r"\s+", "", expr))
    e = e.replace("x", "*").replace("X", "*")
    if not _SAFE_EXPR.match(e):
        raise UnsupportedInput(f"aperture macro expression not understood: {expr!r}")
    return float(eval(e, {"__builtins__": {}}))  # noqa: S307 - restricted to arithmetic above


def _macro_shape(body: list[str], params: list[float]):
    P = {i + 1: v for i, v in enumerate(params)}
    add, clear = [], []
    for prim in body:
        prim = prim.strip()
        if not prim or prim.startswith("0"):
            continue
        if "=" in prim:
            k, v = prim.split("=", 1)
            P[int(k.strip()[1:])] = _ev(v, P)
            continue
        f = [_ev(x, P) for x in prim.split(",")]
        code, exposure = int(f[0]), f[1]
        if code == 1:  # circle
            d, x, y = f[2], f[3], f[4]
            g = rotate(Point(x, y).buffer(d / 2, 32), f[5] if len(f) > 5 else 0, origin=(0, 0))
        elif code == 4:  # outline
            n = int(f[2])
            pts = [(f[3 + 2 * i], f[4 + 2 * i]) for i in range(n + 1)]
            g = rotate(Polygon(pts).buffer(0), f[3 + 2 * (n + 1)], origin=(0, 0))
        elif code == 5:  # polygon
            n, x, y, d, rot = int(f[2]), f[3], f[4], f[5], f[6]
            g = Polygon([(x + d / 2 * math.cos(2 * math.pi * k / n), y + d / 2 * math.sin(2 * math.pi * k / n)) for k in range(n)])
            g = rotate(g, rot, origin=(0, 0))
        elif code == 20:  # vector line
            w, x1, y1, x2, y2, rot = f[2:8]
            g = rotate(LineString([(x1, y1), (x2, y2)]).buffer(w / 2, cap_style="flat"), rot, origin=(0, 0))
        elif code == 21:  # centre line (rectangle)
            w, h, x, y, rot = f[2:7]
            g = rotate(box(x - w / 2, y - h / 2, x + w / 2, y + h / 2), rot, origin=(0, 0))
        else:
            raise UnsupportedInput(f"aperture macro primitive {code} is not supported")
        (add if exposure >= 1 else clear).append(g)
    g = unary_union(add)
    return g.difference(unary_union(clear)) if clear else g


def _aperture_shape(kind: str, p: list[float], macros: dict[str, list[str]]):
    if kind == "C":
        return Point(0, 0).buffer(p[0] / 2, 32)
    if kind == "R":
        return box(-p[0] / 2, -p[1] / 2, p[0] / 2, p[1] / 2)
    if kind == "O":
        w, h = p[0], p[1]
        if w >= h:
            return LineString([(-(w - h) / 2, 0), ((w - h) / 2, 0)]).buffer(h / 2, 32)
        return LineString([(0, -(h - w) / 2), (0, (h - w) / 2)]).buffer(w / 2, 32)
    if kind == "P":
        d, n = p[0], int(p[1])
        r0 = math.radians(p[2]) if len(p) > 2 else 0.0
        return Polygon([(d / 2 * math.cos(r0 + 2 * math.pi * k / n), d / 2 * math.sin(r0 + 2 * math.pi * k / n)) for k in range(n)])
    if kind in macros:
        return _macro_shape(macros[kind], p)
    raise UnsupportedInput(f"aperture type {kind!r} is not defined")


def _arc_points(x, y, nx, ny, i, j, ccw):
    cx, cy = x + i, y + j
    r = math.hypot(x - cx, y - cy)
    a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
    if ccw:
        if a1 <= a0:
            a1 += 2 * math.pi
    elif a1 >= a0:
        a1 -= 2 * math.pi
    if abs(nx - x) < 1e-9 and abs(ny - y) < 1e-9:  # full circle
        a1 = a0 + (2 * math.pi if ccw else -2 * math.pi)
    k = max(8, int(abs(a1 - a0) / ARC_STEP))
    return [(cx + r * math.cos(a0 + (a1 - a0) * t / k), cy + r * math.sin(a0 + (a1 - a0) * t / k)) for t in range(k + 1)]


def file_function(text: str) -> tuple[str, ...] | None:
    """X2 ``TF.FileFunction`` (in a %TF% command or KiCad's ``G04 #@! TF`` comment form)."""
    m = re.search(r"TF\.FileFunction,([^*%\n]+)", text)
    return tuple(s.strip() for s in m.group(1).split(",")) if m else None


# ----------------------------------------------------------------------------- Gerber
@dataclass
class GerberLayer:
    path: Path
    function: tuple[str, ...] | None
    objects: list = field(default_factory=list)          # shapely geometries, one per flash / stroke / region
    stroke_widths: dict[float, int] = field(default_factory=dict)  # circular-aperture D01 strokes: width -> count


def parse_gerber(path: str | Path) -> GerberLayer:
    path = Path(path)
    txt = path.read_text(errors="replace")
    tokens = re.findall(r"%[^%]*%|[^%*]+\*", txt, re.S)
    macros: dict[str, list[str]] = {}
    aps: dict[int, tuple[str, list[float]]] = {}
    dec, unit = 6, 1.0
    cur = None
    x = y = 0.0
    interp = "G01"
    region, reg_pts, reg_polys = False, [], []
    objs, widths, cache = [], {}, {}   # objs: (dark: bool, geometry) in file order
    dark = True

    def num(s):
        return int(s) / 10 ** dec

    for tok in tokens:
        tok = tok.strip()
        if tok.startswith("%"):
            for body in (b for b in tok[1:-1].split("*") if b.strip()) if not tok.startswith("%AM") else [tok[1:-1]]:
                body = body.strip()
                if body.startswith("FS"):
                    m = re.match(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)", body)
                    if not m or m.group(1) != "L" or m.group(2) != "A":
                        raise UnsupportedInput(f"{path.name}: only %FSLA...% (leading-zero omission, absolute) is supported")
                    dec = int(m.group(4))
                elif body.startswith("MO"):
                    unit = 25.4 if body.startswith("MOIN") else 1.0
                elif body.startswith("AM"):
                    parts = body[2:].split("*")
                    macros[parts[0].strip()] = [p for p in parts[1:] if p.strip()]
                elif body.startswith("AD"):
                    m = re.match(r"ADD(\d+)([A-Za-z_$][A-Za-z0-9_.$]*),?(.*)", body.replace("\n", ""))
                    d, kind, ps = int(m.group(1)), m.group(2), m.group(3)
                    aps[d] = (kind, [float(v) for v in ps.split("X")] if ps else [])
                elif body.startswith("LP"):
                    dark = body[2] == "D"
                elif body.startswith("SR"):
                    if body not in ("SR", "SRX1Y1I0J0", "SRX1Y1I0.0J0.0"):
                        raise UnsupportedInput(f"{path.name}: step-and-repeat (%SR%) is not supported")
                elif body.startswith(("LM", "LR", "LS")):
                    raise UnsupportedInput(f"{path.name}: %{body[:2]}% (mirror/rotate/scale) is not supported")
            continue
        c = tok[:-1].replace("\n", "").replace("\r", "")
        if not c or c.startswith("G04"):
            continue
        if c == "G36":
            region, reg_pts = True, []
            continue
        if c == "G37":
            if len(reg_pts) >= 3:
                reg_polys.append(reg_pts)
            objs.extend((dark, Polygon(p).buffer(0)) for p in reg_polys)
            region, reg_polys, reg_pts = False, [], []
            continue
        if re.fullmatch(r"(G54)?D(\d+)", c) and int(re.fullmatch(r"(G54)?D(\d+)", c).group(2)) >= 10:
            cur = int(re.fullmatch(r"(G54)?D(\d+)", c).group(2))
            continue
        if c in ("G01", "G02", "G03"):
            interp = c
            continue
        if c == "G74":
            raise UnsupportedInput(f"{path.name}: single-quadrant arcs (G74) are not supported")
        if c in ("G75", "M02", "M00", "G90", "G71", "G70"):
            continue
        m = re.match(r"^(G0[123])?(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?(D0?[123])?$", c)
        if not m:
            if c.startswith(("TF", "TA", "TD", "TO")):
                continue
            raise UnsupportedInput(f"{path.name}: command {c!r} is not supported")
        g, xs, ys, is_, js, dcode = m.groups()
        if g:
            interp = g
        nx = num(xs) if xs is not None else x
        ny = num(ys) if ys is not None else y
        if dcode is None:
            x, y = nx, ny
            continue
        dcode = dcode.replace("D0", "D")
        if dcode == "D2":
            if region:
                if len(reg_pts) >= 3:
                    reg_polys.append(reg_pts)
                reg_pts = [(nx, ny)]
            x, y = nx, ny
            continue
        if dcode == "D1":
            if interp == "G01":
                seg = [(x, y), (nx, ny)]
            else:
                seg = _arc_points(x, y, nx, ny, num(is_) if is_ else 0.0, num(js) if js else 0.0, interp == "G03")
            if region:
                if not reg_pts:
                    reg_pts = [(x, y)]
                reg_pts.extend(seg[1:])
            else:
                if cur not in aps:
                    raise UnsupportedInput(f"{path.name}: draw with undefined aperture D{cur}")
                kind, p = aps[cur]
                if kind != "C":
                    raise UnsupportedInput(f"{path.name}: drawing with a non-circular aperture (D{cur} {kind}) is not supported")
                w = p[0]
                widths[round(w * unit, 4)] = widths.get(round(w * unit, 4), 0) + 1
                line = LineString(seg) if len(seg) > 1 and (seg[0] != seg[-1] or len(seg) > 2) else None
                objs.append((dark, line.buffer(w / 2, 16) if line is not None else Point(seg[0]).buffer(w / 2, 16)))
            x, y = nx, ny
            continue
        if dcode == "D3":
            if cur not in aps:
                raise UnsupportedInput(f"{path.name}: flash with undefined aperture D{cur}")
            if cur not in cache:
                cache[cur] = _aperture_shape(*aps[cur], macros)
            objs.append((dark, translate(cache[cur], nx, ny)))
            x, y = nx, ny
    if all(d for d, _ in objs):
        objs = [g for _, g in objs if not g.is_empty]
    else:  # clear polarity: apply dark and clear runs in file order, then split into connected polygons
        acc = None
        i = 0
        while i < len(objs):
            j = i
            while j < len(objs) and objs[j][0] == objs[i][0]:
                j += 1
            run = unary_union([g for _, g in objs[i:j]])
            if objs[i][0]:
                acc = run if acc is None else acc.union(run)
            elif acc is not None:
                acc = acc.difference(run)
            i = j
        objs = [] if acc is None or acc.is_empty else list(getattr(acc, "geoms", [acc]))
        objs = [g for g in objs if g.area > 0]
    if unit != 1.0:
        objs = [scale(o, unit, unit, origin=(0, 0)) for o in objs]
    return GerberLayer(path, file_function(txt), objs, widths)


# ----------------------------------------------------------------------------- Excellon
@dataclass
class Hole:
    geom: object
    diameter: float
    plated: bool
    xy: tuple[float, float]


def parse_drill(path: str | Path) -> list[Hole]:
    """Excellon with decimal coordinates (KiCad's default). Plating comes from X2 ``TA.AperFunction``
    comments per tool, or from the file function (Plated / NonPlated) when the whole file is one kind."""
    path = Path(path)
    lines = path.read_text(errors="replace").splitlines()
    ff = file_function("\n".join(lines[:40]))
    file_plated = {"Plated": True, "NonPlated": False}.get(ff[0]) if ff else None
    unit = 1.0
    tools: dict[int, tuple[float, bool | None]] = {}
    holes: list[Hole] = []
    cur, pending, routing, start = None, None, False, None

    def tool_plated(t):
        p = tools[t][1]
        if p is None:
            p = file_plated
        if p is None:
            raise UnsupportedInput(f"{path.name}: cannot tell whether tool T{t} is plated (no TA.AperFunction or file function)")
        return p

    def add(geom, xy):
        d, _ = tools[cur]
        holes.append(Hole(geom, d, tool_plated(cur), xy))

    for raw in lines:
        s = raw.strip()
        m = re.match(r";\s*#@!\s*TA\.AperFunction,(Plated|NonPlated)", s)
        if m:
            pending = m.group(1) == "Plated"
            continue
        if not s or s.startswith(";"):
            continue
        if s.startswith("INCH"):
            unit = 25.4
            continue
        if s.startswith("METRIC"):
            unit = 1.0
            continue
        m = re.match(r"^T(\d+)(?:[FSB][\d.]+)*C([\d.]+)", s)
        if m:
            tools[int(m.group(1))] = (float(m.group(2)) * unit, pending)
            pending = None
            continue
        m = re.match(r"^T(\d+)$", s)
        if m:
            cur = int(m.group(1)) if int(m.group(1)) else None
            continue
        if s in ("M48", "%", "M30", "FMAT,2", "G90", "G05", "M71", "M72") or s.startswith(("FMAT", "ICI")):
            continue
        if s == "M15":
            routing = True
            continue
        if s in ("M16", "M17"):
            routing = False
            continue
        m = re.match(r"^(G0[01])?X(-?[\d.]+)Y(-?[\d.]+)(?:G85X(-?[\d.]+)Y(-?[\d.]+))?$", s)
        if m and cur:
            if "." not in m.group(2):
                raise UnsupportedInput(f"{path.name}: only decimal Excellon coordinates are supported")
            x1, y1 = float(m.group(2)) * unit, float(m.group(3)) * unit
            r = tools[cur][0] / 2
            if m.group(4):  # G85 slot
                x2, y2 = float(m.group(4)) * unit, float(m.group(5)) * unit
                add(LineString([(x1, y1), (x2, y2)]).buffer(r, 32), (x1, y1))
            elif m.group(1) == "G00":
                start = (x1, y1)
            elif m.group(1) == "G01" and routing and start:
                add(LineString([start, (x1, y1)]).buffer(r, 32), start)
                start = (x1, y1)
            else:
                add(Point(x1, y1).buffer(r, 32), (x1, y1))
            continue
        if re.match(r"^[XYGM]", s):
            raise UnsupportedInput(f"{path.name}: drill command {s!r} is not supported")
    return holes


# ----------------------------------------------------------------------------- file discovery
@dataclass
class FabFiles:
    copper: list[tuple[int, str, Path]]   # (layer number 1..n, side 'Top'/'Inr'/'Bot', path), top first
    profile: Path | None
    legend: dict[str, Path]               # 'Top' / 'Bot'
    drills: list[Path]


_EXT_COPPER = {".gtl": (1, "Top"), ".gbl": (99, "Bot")}


def discover(gerber_dir: str | Path) -> FabFiles:
    """Classify files by X2 file function; fall back to Protel/KiCad extensions when attributes are missing."""
    d = Path(gerber_dir)
    if not d.is_dir():
        raise UnsupportedInput(f"{d} is not a directory")
    copper, legend, drills, profile = [], {}, [], None
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        head = p.read_text(errors="replace")[:6000]
        if head.lstrip().startswith("M48") or re.search(r"^M48\s*$", head, re.M):
            drills.append(p)
            continue
        ff = file_function(head)
        ext = p.suffix.lower()
        if ff:
            if ff[0] == "Copper":
                copper.append((int(ff[1][1:]), ff[2], p))
            elif ff[0] == "Profile":
                profile = p
            elif ff[0] == "Legend":
                legend[ff[1]] = p
            continue
        if ext in _EXT_COPPER:
            copper.append((*_EXT_COPPER[ext], p))
        elif re.fullmatch(r"\.g(\d+)", ext):
            copper.append((int(ext[2:]) + 1, "Inr", p))
        elif ext in (".gm1", ".gko"):
            profile = profile or p
        elif ext in (".gto", ".gbo"):
            legend["Top" if ext == ".gto" else "Bot"] = p
    copper.sort()
    if copper and copper[-1][0] == 99:  # extension fallback: renumber bottom
        n = len(copper)
        copper[-1] = (n, "Bot", copper[-1][2])
    nums = [c[0] for c in copper]
    if nums != list(range(1, len(nums) + 1)):
        raise UnsupportedInput(f"copper layers are not contiguous: {nums} in {d}")
    return FabFiles(copper, profile, legend, drills)
