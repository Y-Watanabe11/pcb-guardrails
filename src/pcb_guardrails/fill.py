"""Check that copper pours actually made it into the Gerbers.

Exporting Gerbers from a board whose zones were never filled (for example a script-generated .kicad_pcb
plotted with a CLI) produces valid-looking files **without the ground plane, and without any error**.
File size is a poor proxy, so this measures the copper area of each layer relative to the board area.
"""
from __future__ import annotations

from shapely.ops import unary_union

from .dfm import board_outline
from .gerber import FabFiles, UnsupportedInput, parse_gerber
from .kicad import copper_name


def run_fill(files: FabFiles, minimums: dict[str, float] | None = None) -> dict:
    if not files.profile:
        raise UnsupportedInput("no board outline (Profile / Edge_Cuts) found; cannot compute coverage")
    _, board = board_outline(files.profile)
    n = len(files.copper)
    layers, failures = {}, []
    for num, _side, path in files.copper:
        name = copper_name(num, n)
        cu = unary_union(parse_gerber(path).objects).intersection(board)
        ratio = cu.area / board.area if board.area else 0.0
        layers[name] = {"copper_mm2": round(cu.area, 2), "coverage": round(ratio, 4), "file": path.name}
        need = (minimums or {}).get(name)
        if need is not None and ratio < need:
            failures.append({"layer": name, "coverage": round(ratio, 4), "min": need})
    return {"ok": not failures, "board_area_mm2": round(board.area, 2), "layers": layers, "failures": failures}
