"""Read an IPC-D-356 netlist: pads, vias and the net of each, from any CAD tool that exports one.

The normative IPC-D-356A/B documents are sold by IPC and were not available when this was written.
# ASSUMPTION: the format below is taken from two exporters and the files they write, not from the standard:
#   KiCad pcbnew/exporters/export_d356.cpp (current source) and KiCad 10.0.4 output on 19 public demo boards;
#   the EAGLE user language program ipc-d-356.ulp (github.com/butterwick/EagleLibrary).
# They agree on: records starting with an operation code in columns 1-3 (317 through-hole pad or via, 327 surface
# pad, 367 non-plated hole), the net name in columns 4-17, the reference in 21-26, "-" and the pin in 27-31,
# "UNITS CUST 0" meaning coordinates in 0.0001 inch, and "999" ending the file. They do not agree on the columns
# after 32 (KiCad 10.0.4 and the current KiCad source place the solder-mask field one column apart) or on the
# unit of the drill size (KiCad 0.0001 inch, the EAGLE program 0.001 inch). So the fields after column 32 are read
# by their letters (D, A, X, Y), and the drill size is not used.
#
# Anything else (other units, blind / buried via records 307 / 027, conductor records, unknown codes) stops the
# reader (exit 2) rather than being skipped.

Positions are returned in the IPC file's own frame (y up). ``align`` finds the offset to the drill files.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point
from shapely.strtree import STRtree

from .gerber import UnsupportedInput
from .kicad import Netlist, Pad, Via

UNIT_MM = 0.0001 * 25.4          # UNITS CUST 0: 0.0001 inch
NAME_FIELD = 14                  # columns 4-17
_FIELDS = re.compile(r"^\s*(?:D\s*\d+\s*([PU])?)?\s*A\s*(\d+)\s*X\s*([+-]?)\s*(\d+)\s*Y\s*([+-]?)\s*(\d+)")


@dataclass
class D356Record:
    op: int
    net: str | None              # None for N/C
    ref: str
    pin: str
    plated: bool | None          # None: no hole
    access: int
    xy: tuple[float, float]      # mm, the file's frame
    line: int


@dataclass
class D356:
    path: Path
    records: list[D356Record]
    aliases: dict[str, str] = field(default_factory=dict)
    full_names: set[str] = field(default_factory=set)   # net names that fill the 14-column field: maybe cut short


def read_ipc356(path: str | Path) -> D356:
    path = Path(path)
    text = path.read_text(encoding="ascii", errors="replace")
    units = None
    aliases = {}
    records = []
    ended = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        if not line.strip() or ended:
            continue
        if line.startswith("C"):
            continue
        if line.startswith("P"):
            body = line[1:].strip()
            m = re.match(r"UNITS\s+(.*\S)", body)
            if m:
                units = re.sub(r"\s+", " ", m.group(1).upper())
            m = re.match(r"NNAME(\S+)\s+(.*\S)", body)
            if m:
                aliases[m.group(1)] = m.group(2)
            continue
        if line.startswith("999"):
            ended = True
            continue
        if not line[:3].isdigit():
            raise UnsupportedInput(f"{path.name} line {no}: unknown record {line[:12]!r}")
        op = int(line[:3])
        if op not in (317, 327, 367):
            raise UnsupportedInput(f"{path.name} line {no}: record type {line[:3]} is not supported "
                                   "(supported: 317, 327, 367)")
        line = line.ljust(32)
        net = line[3:17].rstrip()
        ref = line[20:26].strip()
        pin = line[27:31].strip() if line[26] == "-" else ""
        m = _FIELDS.match(line[32:])
        if not m:
            raise UnsupportedInput(f"{path.name} line {no}: could not read the access and position fields")
        plated, access, sx, x, sy, y = m.groups()
        hole = re.match(r"\s*D", line[32:]) is not None
        xy = ((-1 if sx == "-" else 1) * int(x) * UNIT_MM, (-1 if sy == "-" else 1) * int(y) * UNIT_MM)
        records.append(D356Record(op, None if net in ("N/C", "") else aliases.get(net, net), ref, pin,
                                  (plated != "U") if hole else None, int(access), xy, no))
    if units != "CUST 0":
        raise UnsupportedInput(f"{path.name}: units {units or '(none given)'!r} are not supported (supported: CUST 0, "
                               "0.0001 inch)")
    if not records:
        raise UnsupportedInput(f"{path.name}: no test records (317 / 327)")
    full = {r.net for r in records if r.net and len(r.net) >= NAME_FIELD and r.net not in aliases.values()}
    return D356(path, records, aliases, full)


# ----------------------------------------------------------------------------- alignment with the drill files
ALIGN_TOL_MM = 0.01      # a D-356 hole and a drill hit coincide (D-356 resolution 2.54 um, drill file 1-2.54 um)
# ASSUMPTION: an offset is accepted when at least 90 % of the plated holes of the D-356 land on a plated drill hit,
# and no other offset comes within 10 points of it. A few holes may be missing on purpose (that is what
# VIA-NO-HOLE reports); a regular grid of holes could otherwise fit at a shifted offset.
ALIGN_MIN = 0.9
ALIGN_MARGIN = 0.1


def align(d: D356, drill_xy: list[tuple[float, float]]) -> tuple[tuple[float, float], int, int]:
    """Offset (dx, dy) in mm that puts the D-356 plated holes onto the drill hits: gerber = d356 + offset.
    Returns (offset, holes matched, plated holes in the D-356)."""
    ours = [r.xy for r in d.records if r.plated]
    if not ours:
        raise UnsupportedInput(f"{d.path.name}: no plated holes to align with the drill files; "
                               "pass --ipc356-offset DX,DY (mm)")
    if not drill_xy:
        raise UnsupportedInput("no plated drill hits to align the IPC-D-356 with")
    tree = STRtree([Point(p) for p in drill_xy])

    def score(off, pts):
        n = 0
        for x, y in pts:
            q = Point(x + off[0], y + off[1])
            if any(Point(drill_xy[i]).distance(q) <= ALIGN_TOL_MM for i in tree.query(q.buffer(ALIGN_TOL_MM))):
                n += 1
        return n

    # Vote: each of K reference holes proposes the offsets to every drill hit; the true offset is proposed by
    # (nearly) all of them. Votes are binned on a 0.05 mm grid and read with their neighbours, since the true
    # offsets differ by the files' few-micron resolution.
    refs = list(dict.fromkeys(ours))
    refs = refs[:: max(1, len(refs) // 20)][:20]
    cell = 0.05
    bins: dict[tuple[int, int], set] = {}
    pairs: dict[tuple[int, int], list] = {}
    for k, (rx, ry) in enumerate(refs):
        for hx, hy in drill_xy:
            c = (round((hx - rx) / cell), round((hy - ry) / cell))
            bins.setdefault(c, set()).add(k)
            pairs.setdefault(c, []).append((hx - rx, hy - ry))

    def votes(c):
        return len(set().union(*(bins.get((c[0] + i, c[1] + j), set()) for i in (-1, 0, 1) for j in (-1, 0, 1))))
    top = sorted(bins, key=votes, reverse=True)[:5]
    ranked = []
    for c in top:
        near = sorted(pairs[c])
        ranked.append(near[len(near) // 2])   # a real offset from this cell
    full = sorted(((score(o, ours), o) for o in ranked), reverse=True)
    best_n, best = full[0]
    second = next((n for n, o in full[1:] if abs(o[0] - best[0]) > ALIGN_TOL_MM or abs(o[1] - best[1]) > ALIGN_TOL_MM), 0)
    if best_n < ALIGN_MIN * len(ours) or best_n - second < ALIGN_MARGIN * len(ours):
        raise UnsupportedInput(
            f"{d.path.name}: could not align with the drill files (best offset {best} mm matches {best_n} of "
            f"{len(ours)} plated holes, next best {second}); pass --ipc356-offset DX,DY (mm) if the origins are known")
    # refine: the mean offset of the matched holes, so the result does not depend on which pair was voted for
    dx, dy = [], []
    for x, y in ours:
        q = Point(x + best[0], y + best[1])
        for i in tree.query(q.buffer(ALIGN_TOL_MM)):
            if Point(drill_xy[i]).distance(q) <= ALIGN_TOL_MM:
                dx.append(drill_xy[i][0] - x)
                dy.append(drill_xy[i][1] - y)
                break
    return (sum(dx) / len(dx), sum(dy) / len(dy)), best_n, len(ours)


# ----------------------------------------------------------------------------- to netcheck's inputs
def to_netcheck(d: D356, offset: tuple[float, float], n_copper: int):
    """Pads (Gerber frame), vias and a netlist for ``run_netcheck``, notes about what was adapted, and the original
    name of every pad renamed REF#n."""
    from .kicad import copper_name
    pads, vias, pins, refs, notes = [], [], {}, set(), []
    seen: dict[tuple[str, str], set] = {}
    for r in d.records:
        if r.op == 367:
            continue
        xy = (r.xy[0] + offset[0], r.xy[1] + offset[1])
        if r.op == 317 and r.ref.upper() == "VIA" and not r.pin:
            vias.append(Via(xy, 0.0, None))
            continue
        if r.op == 327:
            if r.access == 1:
                layers = (copper_name(1, n_copper),)
            elif r.access >= n_copper:   # the bottom: KiCad writes the layer count, EAGLE 16
                layers = (copper_name(n_copper, n_copper),)
            else:
                raise UnsupportedInput(f"{d.path.name} line {r.line}: surface pad on inner layer access A{r.access:02d}")
            kind = "smd"
        else:
            layers, kind = ("*.Cu",), "thru_hole"
        seen.setdefault((r.ref, r.pin), set()).add(r.net)
        pads.append(Pad(r.ref, r.pin, kind, xy, layers, net=r.net))
        refs.add(r.ref)
    # references are cut to 6 columns and pins to 4: different parts can end up with one name
    clash = {k for k, nets in seen.items() if len(nets) > 1}
    origin = {}
    if clash:
        count: Counter = Counter()
        for p in pads:
            if (p.ref, p.pin) in clash:
                count[(p.ref, p.pin)] += 1
                was = p.name
                p.ref = f"{p.ref}#{count[(p.ref, p.pin)]}"
                origin[p.name] = was
                refs.add(p.ref)
        notes.append(f"{len(clash)} pad names occur with different nets (two parts whose references were cut to 6 "
                     "characters, or one part repeating a pad number): renamed REF#n")
    for p in pads:
        if p.net is not None:
            pins[(p.ref, p.pin)] = p.net
        p.net = None
    if d.full_names:
        notes.append(f"{len(d.full_names)} net names fill the 14-character field and may have been cut short: "
                     "two nets cut to the same name are read as one")
    return pads, vias, Netlist(pins, refs, None), notes, origin


def same_pad_shorts(shorts: list[dict], origin: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Split off SHORTs whose pads are all one pad name repeated with different nets (KiCad gives each copy of a
    repeated, unconnected pad number its own net, e.g. a thermal pad and its vias, while the footprint's copper joins
    them). Returns the remaining shorts and a warning for each one split off."""
    keep, warn = [], []
    for s in shorts:
        names = {origin.get(p, p) for p in s["pads"]}
        if len(names) == 1 and all(p in origin for p in s["pads"]):
            warn.append(f"{len(s['pads'])} pads named {next(iter(names))} carry {len(s['nets'])} different nets in the "
                        "IPC-D-356 but share one piece of copper (one part repeating a pad number, e.g. a thermal pad "
                        "and its vias): not a design fault, but an electrical test using this file may flag it")
        else:
            keep.append(s)
    return keep, warn
