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
    tree = sexpdata.loads(Path(path).read_text())
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


@dataclass
class Netlist:
    pins: dict[tuple[str, str], str]   # (ref, pin) -> net
    refs: set[str]                     # every component in the schematic (pins of these with no net are no-connects)


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
    return Netlist(pins, refs | {r for r, _ in pins})


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


def load_schematic(path: str | Path) -> list[SchSymbol]:
    """Every placed symbol of a schematic and its sub-sheets (each sheet file read once). References come from the
    symbol's ``instances`` (one per use of a reused sheet), else from its Reference property. Power symbols (marked
    ``(power)`` in the schematic's symbol cache) and references starting with ``#`` are skipped."""
    out: list[SchSymbol] = []
    seen: set[Path] = set()
    todo = [Path(path)]
    while todo:
        f = todo.pop(0)
        rf = f.resolve()
        if rf in seen:
            continue
        seen.add(rf)
        if not f.is_file():
            raise FileNotFoundError(f"schematic sheet {f} not found")
        tree = sexpdata.loads(f.read_text(encoding="utf-8"))
        power = {str(ls[1]) for cache in _kids(tree, "lib_symbols") for ls in _kids(cache, "symbol") if _kids(ls, "power")}
        for sym in _kids(tree, "symbol"):
            lib = _kids(sym, "lib_id")
            if not lib or str(lib[0][1]) in power:   # power symbols and PWR_FLAG are not parts, whatever their reference
                continue
            refs = [str(r[1]) for inst in _kids(sym, "instances") for proj in _kids(inst, "project")
                    for pth in _kids(proj, "path") for r in _kids(pth, "reference")]
            if not refs:
                refs = [_prop(sym, "Reference") or "?"]
            for ref in dict.fromkeys(refs):
                if ref.startswith("#"):
                    continue
                out.append(SchSymbol(ref, str(lib[0][1]), _prop(sym, "Footprint") or "", _flag(sym, "dnp", False),
                                     _flag(sym, "on_board", True), f.name))
        for sh in _kids(tree, "sheet"):
            name = _prop(sh, "Sheetfile") or _prop(sh, "Sheet file")
            if name:
                todo.append(f.parent / name)
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
    tree = sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for fp in _kids(tree, "footprint") + _kids(tree, "module"):
        attrs = tuple(str(_S(a)) for at in _kids(fp, "attr") for a in at[1:] if not isinstance(a, list))
        out.append(PcbFootprint(_reference(fp), str(fp[1]), attrs))
    return out
