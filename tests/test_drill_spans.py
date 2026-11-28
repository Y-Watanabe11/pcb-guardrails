"""Blind and buried vias join only the copper layers their drill file names, never the whole stack.

A four-layer board written from scratch:

    R1.1  SMD on F.Cu at (5,10)  -- top trace -->    via at (10,10)
    R2.1  SMD on B.Cu at (20,10) -- bottom trace --> pad at (10,10)

Through the via, net A is connected. If the via is blind (layers 1-2) it never reaches B.Cu, so net A is OPEN in
the fab files, whatever the board data says. The drill file formats follow what KiCad 10.0.4 writes
(``; #@! TF.FileFunction,Plated,1,2,Blind`` in Excellon, ``%TF.FileFunction,Plated,1,2,Blind,Drill*%`` as Gerber).
"""
import json

from pcb_guardrails.cli import main
from synth import _gerber, _path, _xy


def _excellon(func: str | None, hits: str, aper: str = "Plated,PTH,ViaDrill") -> str:
    ff = f"; #@! TF.FileFunction,{func}\n" if func else ""
    return (f"M48\n; DRILL file synthetic\n{ff}FMAT,2\nMETRIC\n; #@! TA.AperFunction,{aper}\nT1C0.300\n"
            f"%\nG90\nG05\nT1\n{hits}M30\n")


def make_four_layer(root, drills: dict[str, str], inner_route: bool = False):
    """``drills``: file name -> content. ``inner_route``: net A goes F.Cu -> blind via (10,10) -> In1.Cu trace ->
    through via (15,10) -> B.Cu, the correct use of a blind via."""
    g = root / "gerbers"
    g.mkdir(parents=True)
    top = "D11*\n" + _xy(5, 10, 3) + "D13*\n" + _xy(10, 10, 3) + _path([(5, 10), (10, 10)])
    if inner_route:
        in1 = "D13*\n" + _xy(10, 10, 3) + _xy(15, 10, 3) + _path([(10, 10), (15, 10)])
        bot = "D11*\n" + _xy(20, 10, 3) + "D13*\n" + _xy(15, 10, 3) + _path([(15, 10), (20, 10)])
    else:
        in1 = ""
        bot = "D11*\n" + _xy(20, 10, 3) + "D13*\n" + _xy(10, 10, 3) + _path([(10, 10), (20, 10)])
    (g / "b-F_Cu.gtl").write_text(_gerber("Copper,L1,Top", top))
    (g / "b-In1_Cu.g1").write_text(_gerber("Copper,L2,Inr", in1))
    (g / "b-In2_Cu.g2").write_text(_gerber("Copper,L3,Inr", ""))
    (g / "b-B_Cu.gbl").write_text(_gerber("Copper,L4,Bot", bot))
    (g / "b-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", _path([(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], ap="D14")))
    for name, text in drills.items():
        (g / name).write_text(text)
    vias = ' (via blind (at 10 -10) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))\n'
    if inner_route:
        vias += ' (via (at 15 -10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
    pcb = root / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "pcb-guardrails-test")\n (net 0 "")\n (net 1 "/A")\n'
        ' (footprint "Test:TP" (layer "F.Cu") (at 5 -10)\n  (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "/A")))\n'
        ' (footprint "Test:TP" (layer "B.Cu") (at 20 -10)\n  (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu" "B.Mask") (net 1 "/A")))\n' + vias + ")\n")
    net = root / "b.net.xml"
    net.write_text('<?xml version="1.0"?><export version="E"><components><comp ref="R1"/><comp ref="R2"/></components>'
                   '<nets><net code="1" name="/A"><node ref="R1" pin="1"/><node ref="R2" pin="1"/></net></nets></export>\n')
    return g, pcb, net


def run(tmp_path, drills, inner_route=False):
    g, pcb, net = make_four_layer(tmp_path, drills, inner_route)
    out = tmp_path / "r.json"
    code = main(["netcheck", str(g), "--netlist", str(net), "--pcb", str(pcb), "--json", str(out)])
    return code, (json.loads(out.read_text()) if out.exists() else None)


VIA = "X10.0Y10.0\n"


def test_through_via_joins_every_layer(tmp_path):
    code, r = run(tmp_path, {"b.drl": _excellon("Plated,1,4,PTH", VIA)})
    assert code == 0 and not r["opens"]


def test_blind_via_does_not_reach_the_bottom(tmp_path):
    code, r = run(tmp_path, {"b-front-in1.drl": _excellon("Plated,1,2,Blind", VIA, "Plated,Buried,ViaDrill")})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["A"]


def test_buried_via_does_not_reach_the_outer_layers(tmp_path):
    code, r = run(tmp_path, {"b-in1-in2.drl": _excellon("Plated,2,3,Buried", VIA, "Plated,Buried,ViaDrill")})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["A"]


def test_span_order_is_not_significant(tmp_path):
    """The spec allows the span to be written bottom first (its backdrill example writes 8,7)."""
    code, r = run(tmp_path, {"b-front-in1.drl": _excellon("Plated,2,1,Blind", VIA, "Plated,Buried,ViaDrill")})
    assert code == 1 and [o["net"] for o in r["opens"]] == ["A"]
    code, r = run(tmp_path / "x", {"b.drl": _excellon("Plated,4,1,PTH", VIA)})
    assert code == 0


def test_blind_via_in_a_gerber_drill_file(tmp_path):
    drill = _gerber("Plated,1,2,Blind,Drill", "%ADD20C,0.300000*%\nD20*\n" + _xy(10, 10, 3))
    code, r = run(tmp_path, {"b-front-in1-drl.gbr": drill})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["A"]


def test_blind_via_used_correctly_passes(tmp_path):
    code, r = run(tmp_path, {"b-front-in1.drl": _excellon("Plated,1,2,Blind", VIA, "Plated,Buried,ViaDrill"),
                             "b.drl": _excellon("Plated,1,4,PTH", "X15.0Y10.0\n")}, inner_route=True)
    assert code == 0 and not r["opens"] and not r["breakouts"]


def test_drill_file_without_a_layer_span_cannot_pass(tmp_path, capsys):
    """Plating is known per tool, but nothing says which of the four layers the holes join."""
    code, _ = run(tmp_path, {"b.drl": _excellon(None, VIA)})
    assert code == 2
    assert "layer span" in capsys.readouterr().err


def test_layer_span_outside_the_stack_cannot_pass(tmp_path, capsys):
    code, _ = run(tmp_path, {"b.drl": _excellon("Plated,1,6,PTH", VIA)})
    assert code == 2
    assert "1,6" in capsys.readouterr().err


def test_pad_between_two_pieces_of_copper_cannot_pass(tmp_path, capsys):
    """R1.1 at (10,10) has no copper at its centre; one piece 0.04 mm above it leads to R2, another 0.04 mm below
    is an island. Which one the pad is on decides OPEN or PASS, so neither may be guessed."""
    g = tmp_path / "gerbers"
    g.mkdir()
    top = ("%ADD20C,0.040000*%\nD20*\n" + _xy(10, 10.04, 3) + _xy(10, 9.96, 3) + _path([(10, 10.04), (10, 10.3)], ap="D20")
           + _path([(10, 10.3), (20, 10.3), (20, 10)]) + "D11*\n" + _xy(20, 10, 3))
    (g / "b-F_Cu.gtl").write_text(_gerber("Copper,L1,Top", top))
    (g / "b-B_Cu.gbl").write_text(_gerber("Copper,L2,Bot", ""))
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "pcb-guardrails-test")\n (net 0 "")\n (net 1 "/A")\n'
        ' (footprint "Test:TP" (layer "F.Cu") (at 10 -10)\n  (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 0.1 0.1) (layers "F.Cu" "F.Mask") (net 1 "/A")))\n'
        ' (footprint "Test:TP" (layer "F.Cu") (at 20 -10)\n  (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "/A"))))\n')
    net = tmp_path / "b.net.xml"
    net.write_text('<?xml version="1.0"?><export version="E"><components><comp ref="R1"/><comp ref="R2"/></components>'
                   '<nets><net code="1" name="/A"><node ref="R1" pin="1"/><node ref="R2" pin="1"/></net></nets></export>\n')
    code = main(["netcheck", str(g), "--netlist", str(net), "--pcb", str(pcb), "--allow-no-drill"])
    assert code == 2
    assert "R1.1" in capsys.readouterr().err
