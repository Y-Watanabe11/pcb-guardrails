"""Copper outside the board edge.

The edge is the centre line of the profile strokes. Copper beyond it is judged without fab rules: off the board
entirely, or crossing the edge, is wrong for every fab. Copper around a plated hole the edge cuts (a castellated pad)
is intended and only noted.
"""
import json
import math

import pytest
from shapely.geometry import Point

from pcb_guardrails.cli import main
from pcb_guardrails.dfm import board_outline
from synth import _c, _gerber, _path, _xy, make_board


def add(path, body):
    t = path.read_text()
    path.write_text(t.replace("M02*\n", body + "M02*\n"))


def add_hole(b, x, y):
    drl = b["gerbers"] / "board.drl"
    drl.write_text(drl.read_text().replace("X21.0Y10.0\n", f"X21.0Y10.0\nX{x}Y{y}\n"))


def dfm(tmp_path, capsys, *changes, rules=None):
    b = make_board(tmp_path)
    for c in changes:
        c(b)
    out = tmp_path / "d.json"
    args = ["dfm", str(b["gerbers"]), "--pcb", str(b["pcb"]), "--json", str(out)]
    if rules is not None:
        rf = tmp_path / "rules.json"
        rf.write_text(json.dumps(rules))
        args += ["--rules", str(rf)]
    code = main(args)
    return code, json.loads(out.read_text()), capsys.readouterr().out


def top(b):
    return b["gerbers"] / "board-F_Cu.gtl"


def bottom(b):
    return b["gerbers"] / "board-B_Cu.gbl"


def test_edge_is_the_centre_line(tmp_path, capsys):
    """The profile is drawn with a 0.1 mm line on 0..30 x 0..20: the board is exactly that, not the line's inner side."""
    code, m, out = dfm(tmp_path, capsys)
    assert code == 0 and m["edge_problems"] == [] and "EDGE" not in out
    assert m["board"]["outline_bbox_mm"] == [0, 0, 30, 20] and m["board"]["area_mm2"] == pytest.approx(600)
    assert m["layers"]["F.Cu"]["min_copper_to_edge_mm"] == pytest.approx(4.5)   # U1.1 (25,10), 1 mm square, to x=30


def test_copper_off_the_board_fails_without_rules(tmp_path, capsys):
    """A part placed beside the board: its copper will not exist."""
    code, m, out = dfm(tmp_path, capsys, lambda b: add(top(b), "D11*\n" + _xy(40, 10, 3)))
    assert code == 1 and [(e["kind"], e["layer"]) for e in m["edge_problems"]] == [("off-board", "F.Cu")]
    assert "copper entirely outside the board" in out and "MEASURED ONLY" not in out


def test_copper_crossing_the_edge_fails(tmp_path, capsys):
    code, m, out = dfm(tmp_path, capsys, lambda b: add(top(b), _path([(25, 10), (30.5, 10)])))
    (e,) = m["edge_problems"]
    assert code == 1 and e["kind"] == "over-edge" and e["beyond_edge_mm"] == pytest.approx(0.625, abs=1e-3)
    assert "copper crosses the board edge" in out


def test_pour_up_to_the_edge_is_not_outside(tmp_path, capsys):
    """A pour that reaches the centre line exactly (what KiCad does with zero edge clearance) is at the edge.
    Whether that is allowed is the fab's copper-to-edge rule, not this check."""
    pour = "G36*\n" + _xy(22, 0, 2) + _xy(30, 0, 1) + _xy(30, 5, 1) + _xy(22, 5, 1) + _xy(22, 0, 1) + "G37*\n"
    code, m, _ = dfm(tmp_path, capsys, lambda b: add(bottom(b), pour), rules={"min_copper_to_edge_mm": 0.2})
    assert m["edge_problems"] == [] and m["layers"]["B.Cu"]["min_copper_to_edge_mm"] == 0
    assert code == 1 and [v["rule"] for v in m["violations"]] == ["min_copper_to_edge_mm"]


def test_castellated_pad_is_noted_not_failed(tmp_path, capsys):
    """A plated hole cut in half by the edge, with its pad over the edge on both layers."""
    def castellate(b):
        add(top(b), "D12*\n" + _xy(30, 10, 3))
        add(bottom(b), "D12*\n" + _xy(30, 10, 3))
        add_hole(b, 30.0, 10.0)
    code, m, out = dfm(tmp_path, capsys, castellate)
    assert code == 0 and m["edge_problems"] == [] and m["edge"]["castellated_holes"] == 1
    assert {e["kind"] for l in m["edge"]["layers"].values() for e in l} == {"castellated"}
    assert "NOTE: 1 plated holes are cut by the board edge" in out
    assert m["layers"]["F.Cu"]["min_copper_to_edge_mm"] == pytest.approx(4.5)   # the castellated pad is not counted
    assert m["holes"]["min_hole_to_edge_mm"] > 1


def test_same_pad_without_its_hole_fails(tmp_path, capsys):
    """The hole is what makes copper over the edge a castellation; without it the pad is just cut."""
    def pad_only(b):
        add(top(b), "D12*\n" + _xy(30, 10, 3))
    code, m, _ = dfm(tmp_path, capsys, pad_only)
    assert code == 1 and [e["kind"] for e in m["edge_problems"]] == ["over-edge"]


def _rounded_profile(r):
    """0..30 x 0..20 with corners rounded to radius r, drawn with arcs (G02/G03) as CAD tools write them."""
    s = "D14*\n" + _xy(r, 0, 2) + _xy(30 - r, 0, 1)
    s += f"G75*\nG03*\nX{_c(30)}Y{_c(r)}I{_c(0)}J{_c(r)}D01*\nG01*\n" + _xy(30, 20 - r, 1)
    s += f"G03*\nX{_c(30 - r)}Y{_c(20)}I{_c(-r)}J{_c(0)}D01*\nG01*\n" + _xy(r, 20, 1)
    s += f"G03*\nX{_c(0)}Y{_c(20 - r)}I{_c(0)}J{_c(-r)}D01*\nG01*\n" + _xy(0, r, 1)
    s += f"G03*\nX{_c(r)}Y{_c(0)}I{_c(r)}J{_c(0)}D01*\nG01*\n"
    return s


def test_pour_following_a_rounded_corner_is_not_outside(tmp_path, capsys):
    """A pour whose boundary lies on the true arc of a rounded corner. With the arc approximated by a fixed angular
    step (as copper is) the polygon would cut inside the arc by ~0.01 mm and the pour would look outside."""
    r = 8.0
    cx, cy = 30 - r, 20 - r
    arc = [(cx + r * math.cos(math.radians(a / 4)), cy + r * math.sin(math.radians(a / 4))) for a in range(0, 361)]
    pts = [(22, 12), *arc, (22, 20)]

    def rounded(b):
        (b["gerbers"] / "board-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", _rounded_profile(r)))
        add(bottom(b), "G36*\n" + _xy(*pts[0], 2) + "".join(_xy(*p, 1) for p in pts[1:]) + _xy(*pts[0], 1) + "G37*\n")
    code, m, _ = dfm(tmp_path, capsys, rounded)
    assert code == 0 and m["edge_problems"] == []
    assert m["board"]["area_mm2"] == pytest.approx(600 - (4 - math.pi) * r * r, abs=0.05)   # chords within 1 um of each arc


def test_cut_out_is_not_board(tmp_path):
    """A closed loop inside the outline is a cut-out; a loop inside that is board again."""
    g = tmp_path / "Edge.gm1"
    loops = [[(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)],
             [(8, 8), (12, 8), (12, 12), (8, 12), (8, 8)]]
    g.write_text(_gerber("Profile,NP", "".join(_path(l, ap="D14") for l in loops)))
    board = board_outline(g)
    assert board.area == pytest.approx(600 - 100 + 16)
    assert board.contains(Point(2, 2)) and not board.contains(Point(6, 6)) and board.contains(Point(10, 10))


def test_copper_in_a_cut_out_is_off_board(tmp_path, capsys):
    def cut_out(b):
        loops = [[(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], [(2, 15), (6, 15), (6, 19), (2, 19), (2, 15)]]
        (b["gerbers"] / "board-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", "".join(_path(l, ap="D14") for l in loops)))
        add(top(b), "D11*\n" + _xy(4, 17, 3))
    code, m, _ = dfm(tmp_path, capsys, cut_out)
    assert code == 1 and [e["kind"] for e in m["edge_problems"]] == ["off-board"]


def _loop_with_gap(gap):
    """The 0..30 x 0..20 outline as four separate strokes, the second starting ``gap`` mm from where the first ends."""
    return "".join(_path(seg, ap="D14") for seg in ([(0, 0), (30, 0)], [(30 + gap, 0), (30, 20)],
                                                     [(30, 20), (0, 20)], [(0, 20), (0, 0)]))


@pytest.mark.parametrize("gap", [0.00004, 0.0035, 0.049])
def test_gaps_within_the_drawn_line_are_joined(tmp_path, gap):
    """CAD tools round an arc's end and the next line's start separately (gaps of 0.04 um and 3.5 um on public
    KiCad boards). The 0.1 mm stroke closes any gap below 0.05 mm in the drawn outline."""
    g = tmp_path / "Edge.gm1"
    g.write_text(_gerber("Profile,NP", _loop_with_gap(gap)))
    assert board_outline(g).area == pytest.approx(600, abs=10 * gap + 1e-3)   # one corner moves by up to the gap


def test_open_outline_cannot_be_checked(tmp_path, capsys):
    b = make_board(tmp_path)
    (b["gerbers"] / "board-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", _loop_with_gap(0.06)))
    assert main(["dfm", str(b["gerbers"])]) == 2
    assert "could not build a closed board outline" in capsys.readouterr().err


def test_pour_joined_to_a_castellated_pad_still_counts_for_copper_to_edge(tmp_path, capsys):
    """Only the copper around the castellated hole is excepted, not everything it connects to."""
    def castellate_with_pour(b):
        add(top(b), "D12*\n" + _xy(30, 10, 3) + _path([(30, 10), (29.8, 10), (29.8, 3)]))   # a trace 0.075 mm from the edge
        add(bottom(b), "D12*\n" + _xy(30, 10, 3))
        add_hole(b, 30.0, 10.0)
    code, m, _ = dfm(tmp_path, capsys, castellate_with_pour)
    assert code == 0 and m["edge"]["castellated_holes"] == 1
    assert m["layers"]["F.Cu"]["min_copper_to_edge_mm"] == pytest.approx(0.075, abs=1e-3)   # the trace along the edge
    assert m["layers"]["B.Cu"]["min_copper_to_edge_mm"] > 1                           # only the pad there
