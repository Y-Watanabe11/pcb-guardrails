"""A tiny two-layer board written from scratch, so the tests own every byte (no third-party board files).

    R1 (SMD, top)  pads 1 (9,10)  2 (11,10)
    R2 (THT)       pads 1 (19,10) 2 (21,10), drill 0.8
    U1 (SMD, top)  pad 1 (25,10), in the schematic but not connected (a no-connect pin)
    via            (15,14), 0.6 / 0.3

    net A: R1.2 -- top trace --> R2.1
    net B: R1.1 -- top --> via -- bottom --> R2.2

Coordinates below are in the Gerber frame (y up); the .kicad_pcb gets y negated, as KiCad does.
Mutations (names in ``mutate``) inject exactly one fault each, so a test can prove the checker notices it.
``regenerated`` is not a fault: the same board written again with a new date, new UUIDs, renumbered apertures and
drill tools, and items in a different order (what a CAD tool may do when it regenerates a file).
"""
from __future__ import annotations

import re
from pathlib import Path


def _c(v: float) -> str:
    return str(int(round(v * 1_000_000)))


def _xy(x, y, d):
    return f"X{_c(x)}Y{_c(y)}D0{d}*\n"


def _gerber(func: str, body: str, extra_header: str = "", date: str = "2026-01-01T00:00:00+00:00") -> str:
    return (f"G04 Created {date}*\n%TF.CreationDate,{date}*%\n"
            "%FSLAX46Y46*%\n%MOMM*%\n" f"%TF.FileFunction,{func}*%\n%LPD*%\n{extra_header}"
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
    regen = "regenerated" in mutate
    date = "2026-02-02T12:34:56+00:00" if regen else "2026-01-01T00:00:00+00:00"

    chunks = ["D11*\n" + _xy(9 + shift, 10, 3) + _xy(11 + shift, 10, 3),
              "D12*\n" + _xy(19 + shift, 10, 3) + _xy(21 + shift, 10, 3),
              "D13*\n" + _xy(15 + shift, 14, 3),
              "D11*\n" + _xy(25 + shift, 10, 3)]  # U1.1, a no-connect pin
    if "open_A" not in mutate:
        chunks.append(_path([(11 + shift, 10), (19 + shift, 10)]))
    chunks.append(_path([(9 + shift, 10), (9 + shift, 14), (15 + shift, 14)]))
    top = "".join(reversed(chunks) if regen else chunks)
    if "short_AB" in mutate:
        top += _path([(11 + shift, 10), (11 + shift, 14)])
    if "nc_short" in mutate:
        top += _path([(21 + shift, 10), (25 + shift, 10)])
    if "clear_cut" in mutate:  # a clear-polarity flash that cuts trace A in two (negative artwork done wrong)
        top += "%LPC*%\nD11*\n" + _xy(15 + shift, 10, 3) + "%LPD*%\n"
    header = "%SRX2Y1I30.0J0*%\n" if "step_repeat" in mutate else ""
    renumber = (lambda t: re.sub(r"D(1\d)(?=\D)", lambda m: f"D{int(m.group(1)) + 10}", t)) if regen else (lambda t: t)  # noqa: E731
    (g / "board-F_Cu.gtl").write_text(renumber(_gerber("Copper,L1,Top", top, header, date)))

    if "no_bottom" not in mutate:
        bot = "D12*\n" + _xy(19, 10, 3) + _xy(21, 10, 3) + "D13*\n" + _xy(15, 14, 3)
        bot += _path([(15, 14), (21, 14), (21, 10)])
        (g / "board-B_Cu.gbl").write_text(renumber(_gerber("Copper,L2,Bot", bot, date=date)))

    edge = _path([(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], ap="D14")
    (g / "board-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", edge, date=date))

    via = "" if "no_via_drill" in mutate else "X15.0Y14.0\n"
    t_via, t_pth = ("T2", "T1") if regen else ("T1", "T2")
    tools = [f"; #@! TA.AperFunction,Plated,PTH,ViaDrill\n{t_via}C0.300\n",
             f"; #@! TA.AperFunction,Plated,PTH,ComponentDrill\n{t_pth}C0.800\n"]
    hits = [f"{t_via}\n{via}", f"{t_pth}\nX19.0Y10.0\nX21.0Y10.0\n"]
    if regen:
        tools.reverse()
        hits = [f"{t_pth}\nX21.0Y10.0\nX19.0Y10.0\n", f"{t_via}\n{via}"]
    (g / "board.drl").write_text(
        f"M48\n; DRILL file {date}\n; #@! TF.FileFunction,MixedPlating,1,2\nFMAT,2\nMETRIC\n"
        + "".join(tools) + "%\nG90\nG05\n" + "".join(hits) + "M30\n")

    uid = lambda i: f"{(0xbeef if regen else 0xcafe):08x}-0000-4000-8000-{i:012x}"  # noqa: E731
    k10 = "kicad10_nets" in mutate  # KiCad 10 form: no declarations, references by name
    nets = "" if "drop_nets" in mutate or k10 else ' (net 0 "")\n (net 1 "/A")\n (net 2 "/B")\n'
    A, B = ('(net "/A")', '(net "/B")') if k10 else ('(net 1 "/A")', '(net 2 "/B")')
    items = [
        ' (footprint "Test:R_SMD" (layer "F.Cu") (uuid "%s") (at 10 -10)\n'
        '  (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "%s"))\n'
        '  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") %s)\n'
        '  (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") %s))\n' % (uid(1), uid(2), B, A),
        ' (footprint "Test:R_THT" (layer "F.Cu") (uuid "%s") (at 20 -10)\n'
        '  (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "%s"))\n'
        '  (pad "1" thru_hole circle (at -1 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") %s)\n'
        '  (pad "2" thru_hole circle (at 1 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") %s))\n' % (uid(3), uid(4), A, B),
        ' (footprint "Test:TP" (layer "F.Cu") (uuid "%s") (at 25 -10)\n'
        '  (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS") (uuid "%s"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))\n' % (uid(5), uid(6)),
        ' (via (at 15 -14) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") %s (uuid "%s"))\n' % ('(net "/B")' if k10 else "(net 2)", uid(7)),
        ' (gr_poly (pts (xy 1 -1) (xy 3 -1) (xy 3 -3)) (layer "F.SilkS") (width 0.1) (uuid "%s"))\n' % uid(8),
    ]
    if regen:
        items.reverse()
    pcb = root / "board.kicad_pcb"
    pcb.write_text(f'(kicad_pcb (version 20240108) (generator "pcb-guardrails-test") (generator_version "{date}")\n'
                   + nets + "".join(items) + ")\n")

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
