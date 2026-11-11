"""A tiny two-layer board written from scratch, so the tests own every byte (no third-party board files).

    R1 (SMD, top)  pads 1 (9,10)  2 (11,10)
    R2 (THT)       pads 1 (19,10) 2 (21,10), drill 0.8
    U1 (SMD, top)  pad 1 (25,10), in the schematic but not connected (a no-connect pin)
    via            (15,14), 0.6 / 0.3

    net A: R1.2 -- top trace --> R2.1
    net B: R1.1 -- top --> via -- bottom --> R2.2

Coordinates below are in the Gerber frame (y up); the .kicad_pcb gets y negated, as KiCad does.
Mutations (names in ``mutate``) inject exactly one fault each, so a test can prove the checker notices it.
"""
from __future__ import annotations

from pathlib import Path


def _c(v: float) -> str:
    return str(int(round(v * 1_000_000)))


def _xy(x, y, d):
    return f"X{_c(x)}Y{_c(y)}D0{d}*\n"


def _gerber(func: str, body: str, extra_header: str = "") -> str:
    return ("%FSLAX46Y46*%\n%MOMM*%\n" f"%TF.FileFunction,{func}*%\n%LPD*%\n{extra_header}"
            "%ADD10C,0.250000*%\n%ADD11R,1.000000X1.000000*%\n%ADD12C,1.600000*%\n%ADD13C,0.600000*%\n%ADD14C,0.100000*%\n"
            f"{body}M02*\n")


def _path(pts, ap="D10"):
    s = f"{ap}*\n" + _xy(*pts[0], 2)
    for p in pts[1:]:
        s += _xy(*p, 1)
    return s


def make_board(root: Path, mutate: frozenset[str] | set[str] = frozenset()) -> dict[str, Path]:
    g = root / "gerbers"
    g.mkdir(parents=True, exist_ok=True)
    shift = 0.6 if "shift_top" in mutate else 0.0

    top = "D11*\n" + _xy(9 + shift, 10, 3) + _xy(11 + shift, 10, 3)
    top += "D12*\n" + _xy(19 + shift, 10, 3) + _xy(21 + shift, 10, 3)
    top += "D13*\n" + _xy(15 + shift, 14, 3)
    top += "D11*\n" + _xy(25 + shift, 10, 3)  # U1.1, a no-connect pin
    if "open_A" not in mutate:
        top += _path([(11 + shift, 10), (19 + shift, 10)])
    top += _path([(9 + shift, 10), (9 + shift, 14), (15 + shift, 14)])
    if "short_AB" in mutate:
        top += _path([(11 + shift, 10), (11 + shift, 14)])
    if "nc_short" in mutate:
        top += _path([(21 + shift, 10), (25 + shift, 10)])
    if "clear_cut" in mutate:  # a clear-polarity flash that cuts trace A in two (negative artwork done wrong)
        top += "%LPC*%\nD11*\n" + _xy(15 + shift, 10, 3) + "%LPD*%\n"
    header = "%SRX2Y1I30.0J0*%\n" if "step_repeat" in mutate else ""
    (g / "board-F_Cu.gtl").write_text(_gerber("Copper,L1,Top", top, header))

    if "no_bottom" not in mutate:
        bot = "D12*\n" + _xy(19, 10, 3) + _xy(21, 10, 3) + "D13*\n" + _xy(15, 14, 3)
        bot += _path([(15, 14), (21, 14), (21, 10)])
        (g / "board-B_Cu.gbl").write_text(_gerber("Copper,L2,Bot", bot))

    edge = _path([(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], ap="D14")
    (g / "board-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", edge))

    via = "" if "no_via_drill" in mutate else "X15.0Y14.0\n"
    (g / "board.drl").write_text(
        "M48\n; #@! TF.FileFunction,MixedPlating,1,2\nFMAT,2\nMETRIC\n"
        "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n"
        "; #@! TA.AperFunction,Plated,PTH,ComponentDrill\nT2C0.800\n%\nG90\nG05\n"
        f"T1\n{via}T2\nX19.0Y10.0\nX21.0Y10.0\nM30\n")

    pcb = root / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "pcb-guardrails-test")\n'
        ' (footprint "Test:R_SMD" (layer "F.Cu") (at 10 -10)\n'
        '  (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))\n'
        '  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")))\n'
        ' (footprint "Test:R_THT" (layer "F.Cu") (at 20 -10)\n'
        '  (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" thru_hole circle (at -1 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask"))\n'
        '  (pad "2" thru_hole circle (at 1 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask")))\n'
        ' (footprint "Test:TP" (layer "F.Cu") (at 25 -10)\n'
        '  (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))\n'
        ' (via (at 15 -14) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")))\n')

    a_pins, b_pins = (("R1", "2"), ("R2", "1")), (("R1", "1"), ("R2", "2"))
    if "netlist_drift" in mutate:  # the schematic changed but the PCB was not updated
        a_pins, b_pins = (("R1", "1"), ("R2", "1")), (("R1", "2"), ("R2", "2"))
    nodes = lambda pins: "".join(f'<node ref="{r}" pin="{p}"/>' for r, p in pins)  # noqa: E731
    net = root / "board.net.xml"
    comps = "".join(f'<comp ref="{r}"/>' for r in ("R1", "R2", "U1"))
    net.write_text(f'<?xml version="1.0"?><export version="E"><components>{comps}</components><nets>'
                   f'<net code="1" name="/A">{nodes(a_pins)}</net><net code="2" name="/B">{nodes(b_pins)}</net>'
                   f"</nets></export>\n")
    return {"gerbers": g, "pcb": pcb, "netlist": net}


def make_plane_board(root: Path, filled: bool) -> Path:
    """Four-layer outline with an inner plane that is either poured (region) or missing."""
    g = root / "plane"
    g.mkdir(parents=True, exist_ok=True)
    (g / "p-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", _path([(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], ap="D14")))
    (g / "p-F_Cu.gtl").write_text(_gerber("Copper,L1,Top", "D11*\n" + _xy(5, 5, 3)))
    region = "G36*\n" + _xy(0.5, 0.5, 2) + _xy(29.5, 0.5, 1) + _xy(29.5, 19.5, 1) + _xy(0.5, 19.5, 1) + _xy(0.5, 0.5, 1) + "G37*\n"
    (g / "p-In1_Cu.g1").write_text(_gerber("Copper,L2,Inr", region if filled else ""))
    (g / "p-In2_Cu.g2").write_text(_gerber("Copper,L3,Inr", ""))
    (g / "p-B_Cu.gbl").write_text(_gerber("Copper,L4,Bot", "D11*\n" + _xy(5, 5, 3)))
    return g
