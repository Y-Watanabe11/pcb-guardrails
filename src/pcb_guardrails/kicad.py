"""Read pad positions and vias from a ``.kicad_pcb``, and the schematic netlist from ``kicad-cli sch export netlist``.

Only *positions* are taken from the PCB. Net membership comes from the schematic netlist, so a board whose
PCB nets drifted away from the schematic is caught rather than trusted.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import sexpdata


@dataclass
class Pad:
    ref: str
    pin: str
    kind: str                       # smd / thru_hole / connect
    xy: tuple[float, float]         # Gerber frame (y up), mm
    layers: tuple[str, ...]
    size: tuple[float, float] | None = None
    drill: tuple[float, float] | None = None   # (w, h); round holes have w == h
    net: str | None = None          # filled from the schematic netlist

    @property
    def name(self) -> str:
        return f"{self.ref}.{self.pin}"


@dataclass
class Via:
    xy: tuple[float, float]
    size: float
    drill: float


def read_sexpr(path: str | Path, every: bool = False, **kw):
    """Parse a KiCad S-expression file. Any parse failure becomes UnsupportedInput (exit 2), never a crash:
    a file this tool cannot read must not count as either good or bad."""
    from .gerber import UnsupportedInput
    text = Path(path).read_text(encoding="utf-8")
    try:
        return sexpdata.parse(text, **kw) if every else sexpdata.loads(text, **kw)
    except Exception as e:   # sexpdata's errors are not ValueErrors
        raise UnsupportedInput(f"{path}: not a readable S-expression ({type(e).__name__}: {str(e)[:80].strip()})") from e


def _S(v):
    return v.value() if isinstance(v, sexpdata.Symbol) else v


def _kids(node, key):
    return [c for c in node[1:] if isinstance(c, list) and c and _S(c[0]) == key]


def _reference(fp) -> str:
    for p in _kids(fp, "property"):
        if p[1] == "Reference":
            return str(p[2])
    for t in _kids(fp, "fp_text"):  # KiCad 6/7
        if _S(t[1]) == "reference":
            return str(t[2])
    raise ValueError("footprint without a Reference")


def load_pcb(path: str | Path) -> tuple[list[Pad], list[Via]]:
    tree = read_sexpr(path)
    pads, vias = [], []
    for fp in _kids(tree, "footprint") + _kids(tree, "module"):
        ref = _reference(fp)
        at = _kids(fp, "at")[0]
        x0, y0 = float(at[1]), float(at[2])
        th = math.radians(float(at[3]) if len(at) > 3 else 0.0)
        for p in _kids(fp, "pad"):
            num, kind = str(p[1]), _S(p[2])
            if kind == "np_thru_hole" or num == "":
                continue
            a = _kids(p, "at")[0]
            lx, ly = float(a[1]), float(a[2])
            X = x0 + lx * math.cos(th) + ly * math.sin(th)
            Y = y0 - lx * math.sin(th) + ly * math.cos(th)
            layers = tuple(str(_S(v)) for v in _kids(p, "layers")[0][1:])
            size = tuple(float(v) for v in _kids(p, "size")[0][1:3]) if _kids(p, "size") else None
            drill = None
            if _kids(p, "drill"):
                dr = _kids(p, "drill")[0][1:]
                if dr and _S(dr[0]) == "oval":
                    drill = (float(dr[1]), float(dr[2] if len(dr) > 2 and not isinstance(dr[2], list) else dr[1]))
                elif dr and not isinstance(dr[0], list):
                    drill = (float(dr[0]), float(dr[0]))
            pads.append(Pad(ref, num, kind, (X, -Y), layers, size, drill))
    for v in _kids(tree, "via"):
        at = _kids(v, "at")[0]
        vias.append(Via((float(at[1]), -float(at[2])), float(_kids(v, "size")[0][1]), float(_kids(v, "drill")[0][1])))
    return pads, vias


def net_tie_groups(fp) -> list[frozenset[str]]:
    """Pad-number groups a net-tie footprint joins on purpose: ``(net_tie_pad_groups "1,2" "3,4")``.
    KiCad writes "1,2" and some libraries "1, 2"."""
    return [frozenset(n.strip() for n in str(s).split(",") if n.strip()) for t in _kids(fp, "net_tie_pad_groups") for s in t[1:]]


def load_net_ties(path: str | Path) -> list[tuple[str, frozenset[str]]]:
    tree = read_sexpr(path)
    return [(_reference(fp), g) for fp in _kids(tree, "footprint") + _kids(tree, "module") for g in net_tie_groups(fp)]


@dataclass
class Netlist:
    pins: dict[tuple[str, str], str]   # (ref, pin) -> net
    refs: set[str]                     # every component in the schematic (pins of these with no net are no-connects)
    symbol_pins: dict[str, set[str]] = None   # ref -> pin numbers of its symbol (from <libparts>), when known


def load_netlist(path: str | Path) -> Netlist:
    """KiCad XML netlist (``kicad-cli sch export netlist --format kicadxml``)."""
    root = ET.parse(path).getroot()
    nets = root.find("nets")
    if nets is None:
        raise ValueError(f"{path}: no <nets> element; is this a KiCad XML netlist?")
    pins = {}
    for n in nets:
        name = n.get("name", "").lstrip("/")
        for nd in n.findall("node"):
            pins[(nd.get("ref"), nd.get("pin"))] = name
    comps = root.find("components")
    refs = {c.get("ref") for c in comps} if comps is not None else set()
    libpins = {}
    for lp in root.find("libparts") if root.find("libparts") is not None else []:
        pl = lp.find("pins")
        libpins[(lp.get("lib"), lp.get("part"))] = {x.get("num") for x in pl} if pl is not None else set()
    symbol_pins = {}
    for c in comps if comps is not None else []:
        ls = c.find("libsource")
        if ls is not None and (ls.get("lib"), ls.get("part")) in libpins:
            symbol_pins[c.get("ref")] = libpins[(ls.get("lib"), ls.get("part"))]
    return Netlist(pins, refs | {r for r, _ in pins}, symbol_pins)


def copper_name(layer_no: int, n_layers: int) -> str:
    """Gerber copper layer number (1 = top) -> KiCad layer name."""
    if layer_no == 1:
        return "F.Cu"
    if layer_no == n_layers:
        return "B.Cu"
    return f"In{layer_no - 1}.Cu"


# ----------------------------------------------------------------------------- schematic symbols, board footprints
@dataclass
class SchSymbol:
    ref: str
    lib_id: str
    footprint: str
    dnp: bool
    on_board: bool
    file: str


def _prop(node, name) -> str | None:
    for p in _kids(node, "property"):
        if len(p) > 2 and p[1] == name:
            return str(p[2])
    return None


def _flag(node, key, default: bool) -> bool:
    k = _kids(node, key)
    if not k:
        return default
    return len(k[0]) < 2 or str(_S(k[0][1])) == "yes"


def _uuid(node) -> str:
    u = _kids(node, "uuid")
    return str(u[0][1]) if u else ""


def load_schematic(path: str | Path) -> list[SchSymbol]:
    """Every placed symbol of a schematic hierarchy, one entry per use: a sheet used twice yields its symbols twice,
    each with the reference of that use.

    The hierarchy is walked from the root sheet, so each use has a path (``/<root uuid>/<sheet uuid>/...``) and a
    symbol takes the reference recorded for that path (KiCad 7+: the symbol's ``instances``; KiCad 6: the root's
    ``symbol_instances``). A symbol with no instance records at all (older files, hand-written test files) uses
    its Reference property; one whose records are all for other paths (left over from a copied or removed sheet)
    is not part of this design and is skipped, as KiCad does. Power symbols (``(power)`` in the symbol cache) and
    references starting with ``#`` are skipped."""
    root = Path(path)
    trees: dict[Path, list] = {}

    def tree_of(f: Path):
        rf = f.resolve()
        if rf not in trees:
            if not f.is_file():
                raise FileNotFoundError(f"schematic sheet {f} not found")
            trees[rf] = read_sexpr(f)
        return trees[rf]

    top = tree_of(root)
    root_uuid = _uuid(top)
    v6 = {}   # KiCad 6: "/<sheet uuids>/<symbol uuid>" -> reference
    for si in _kids(top, "symbol_instances"):
        for pth in _kids(si, "path"):
            for r in _kids(pth, "reference"):
                v6[str(pth[1])] = str(r[1])

    out: list[SchSymbol] = []
    stack = [(root, "", 0)]      # (file, path of sheet uuids below the root, depth)
    while stack:
        f, sheet_path, depth = stack.pop()
        if depth > 32:
            raise ValueError(f"{f}: schematic hierarchy deeper than 32 sheets (a sheet that includes itself?)")
        tree = tree_of(f)
        power = {str(ls[1]) for cache in _kids(tree, "lib_symbols") for ls in _kids(cache, "symbol") if _kids(ls, "power")}
        here = {f"/{root_uuid}{sheet_path}", sheet_path or "/"}
        for sym in _kids(tree, "symbol"):
            lib = _kids(sym, "lib_id")
            if not lib or str(lib[0][1]) in power:   # power symbols and PWR_FLAG are not parts, whatever their reference
                continue
            records = [(str(pth[1]), str(r[1])) for inst in _kids(sym, "instances") for proj in _kids(inst, "project")
                       for pth in _kids(proj, "path") for r in _kids(pth, "reference")]
            ref = next((r for pth, r in records if pth in here), None)
            if ref is None and v6:
                ref = v6.get(f"{sheet_path}/{_uuid(sym)}")
            if ref is None:
                if records or v6:
                    continue
                ref = _prop(sym, "Reference") or "?"
            if ref.startswith("#"):
                continue
            out.append(SchSymbol(ref, str(lib[0][1]), _prop(sym, "Footprint") or "", _flag(sym, "dnp", False),
                                 _flag(sym, "on_board", True), f.name))
        for sh in reversed(_kids(tree, "sheet")):
            name = _prop(sh, "Sheetfile") or _prop(sh, "Sheet file")
            if name:
                stack.append((f.parent / name, f"{sheet_path}/{_uuid(sh)}", depth + 1))
    return out


@dataclass
class PcbFootprint:
    ref: str
    fpid: str
    attrs: tuple[str, ...]

    @property
    def dnp(self) -> bool:
        return "dnp" in self.attrs

    @property
    def board_only(self) -> bool:
        return "board_only" in self.attrs


def load_footprints(path: str | Path) -> list[PcbFootprint]:
    tree = read_sexpr(path)
    out = []
    for fp in _kids(tree, "footprint") + _kids(tree, "module"):
        attrs = tuple(str(_S(a)) for at in _kids(fp, "attr") for a in at[1:] if not isinstance(a, list))
        out.append(PcbFootprint(_reference(fp), str(fp[1]), attrs))
    return out
