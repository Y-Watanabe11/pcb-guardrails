"""Each test injects one known fault and proves the checker reports it (and that the clean board passes)."""
import json

import pytest

from pcb_guardrails.cli import main
from synth import make_board, make_plane_board


def run(tmp_path, mutate=(), extra=()):
    b = make_board(tmp_path, set(mutate))
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out), *extra])
    return code, (json.loads(out.read_text()) if out.exists() else None)


def test_clean_board_passes(tmp_path):
    code, r = run(tmp_path)
    assert code == 0
    assert r["ok"] and r["nets"] == 2 and r["pads"] == 5
    assert {n["net"]: n["status"] for n in r["per_net"]} == {"A": "OK", "B": "OK"}
    assert set(r["per_net"][1]["layers"]) == {"F.Cu", "B.Cu"}  # net B uses both layers through the via


def test_missing_trace_is_open(tmp_path):
    code, r = run(tmp_path, {"open_A"})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["A"]


def test_extra_trace_is_short(tmp_path):
    code, r = run(tmp_path, {"short_AB"})
    assert code == 1
    assert r["shorts"] and r["shorts"][0]["nets"] == ["A", "B"]


def test_no_connect_pin_touching_a_net_is_short(tmp_path):
    code, r = run(tmp_path, {"nc_short"})
    assert code == 1
    assert r["shorts"][0]["nets"] == ["B", "unconnected:U1.1"]


def test_missing_via_drill_is_open(tmp_path):
    code, r = run(tmp_path, {"no_via_drill"})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["B"]
    assert r["vias_without_hole"] == [{"xy_kicad": [15.0, -14.0], "drill_mm": 0.3}]


def test_missing_redundant_via_drill_is_caught(tmp_path):
    """A stitching via whose loss changes no connectivity (found on a public 6-layer board: one of 279 GND vias):
    OPEN cannot see it, the via-to-hole check can."""
    b = make_board(tmp_path)
    t = b["pcb"].read_text()
    b["pcb"].write_text(t.replace(' (via (at 15 -14)', ' (via (at 25 -2) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu"))\n (via (at 15 -14)'))
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    r = json.loads(out.read_text())
    assert code == 1 and not r["opens"] and r["vias_without_hole"] == [{"xy_kicad": [25.0, -2.0], "drill_mm": 0.3}]


def test_missing_layer_file_is_open(tmp_path):
    code, r = run(tmp_path, {"no_bottom"})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["B"]


def test_shifted_layer_leaves_pads_without_copper(tmp_path):
    code, r = run(tmp_path, {"shift_top"})
    assert code == 1
    assert {p["pad"] for p in r["no_copper"]} == {"R1.1", "R1.2", "U1.1"}


def test_schematic_drift_is_caught(tmp_path):
    code, r = run(tmp_path, {"netlist_drift"})
    assert code == 1
    assert r["opens"] and r["shorts"]


def test_clear_polarity_cut_is_open(tmp_path):
    code, r = run(tmp_path, {"clear_cut"})
    assert code == 1
    assert [o["net"] for o in r["opens"]] == ["A"]


def test_unsupported_feature_cannot_pass(tmp_path, capsys):
    code, _ = run(tmp_path, {"step_repeat"})
    assert code == 2
    assert "CANNOT CHECK" in capsys.readouterr().err


def test_dfm_measures_and_judges(tmp_path):
    b = make_board(tmp_path)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"min_trace_width_mm": 0.2, "min_via_annular_ring_mm": 0.2}))
    out = tmp_path / "dfm.json"
    code = main(["dfm", str(b["gerbers"]), "--pcb", str(b["pcb"]), "--rules", str(rules), "--json", str(out), "--gap-window", "2"])
    m = json.loads(out.read_text())
    assert m["layers"]["F.Cu"]["min_trace_width_mm"] == pytest.approx(0.25)
    assert m["vias"][0]["annular_ring_mm"] == pytest.approx(0.15)
    assert m["layers"]["F.Cu"]["min_gap_between_conductors_mm"] == pytest.approx(0.4)  # R2.1 to R2.2 (1.6 mm pads, 2 mm apart)
    assert code == 1 and [v["rule"] for v in m["violations"]] == ["min_via_annular_ring_mm"]


def test_fill_catches_missing_pour(tmp_path):
    ok = main(["fill", str(make_plane_board(tmp_path / "a", filled=True)), "--min", "In1.Cu=0.8"])
    bad = main(["fill", str(make_plane_board(tmp_path / "b", filled=False)), "--min", "In1.Cu=0.8"])
    assert (ok, bad) == (0, 1)


def test_layers_are_classified_by_file_function(tmp_path, capsys):
    b = make_board(tmp_path)
    assert main(["layers", str(b["gerbers"])]) == 0
    out = capsys.readouterr().out
    assert "L1 Top F.Cu" in out and "L2 Bot B.Cu" in out and "board.drl" in out


def test_roundrect_macro_with_tiny_parameters(tmp_path):
    """KiCad's RoundRect macro gets corner coordinates like 0.000010; such parameters once stopped the check."""
    from pcb_guardrails.gerber import parse_gerber
    g = tmp_path / "rr.gbr"
    g.write_text("%FSLAX46Y46*%\n%MOMM*%\n%TF.FileFunction,Copper,L1,Top*%\n"
                 "%AMRoundRect*\n0 Rectangle with rounded corners*\n4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*\n"
                 "1,1,$1+$1,$2,$3*\n1,1,$1+$1,$4,$5*\n1,1,$1+$1,$6,$7*\n1,1,$1+$1,$8,$9*\n"
                 "20,1,$1+$1,$2,$3,$4,$5,0*\n20,1,$1+$1,$4,$5,$6,$7,0*\n20,1,$1+$1,$6,$7,$8,$9,0*\n20,1,$1+$1,$8,$9,$2,$3,0*%\n"
                 "%ADD10RoundRect,0.850000X0.000010X-1.300000X0.000010X1.300000X-0.000010X1.300000X-0.000010X-1.300000X0*%\n"
                 "D10*\nX0Y0D03*\nM02*\n")
    layer = parse_gerber(g)
    (obj,) = layer.objects
    assert obj.bounds == pytest.approx((-0.85, -2.15, 0.85, 2.15), abs=1e-3)   # a 1.7 x 4.3 mm stadium-like pad


def test_net_tie_is_not_a_short(tmp_path):
    """A bridged solder jumper / net-tie footprint joins its nets on purpose (some libraries write "1, 2")."""
    b = make_board(tmp_path, {"short_AB"})
    t = b["pcb"].read_text()
    b["pcb"].write_text(t.replace('(footprint "Test:R_SMD" (layer "F.Cu")', '(footprint "Test:R_SMD" (layer "F.Cu") (net_tie_pad_groups "1, 2")'))
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    r = json.loads(out.read_text())
    assert code == 0 and r["shorts"] == [] and r["ties"][0]["nets"] == ["A", "B"] and r["ties"][0]["net_ties"] == ["R1"]


def test_net_tie_does_not_excuse_other_nets(tmp_path):
    code, r = run(tmp_path, {"nc_short"})     # no tie declared: still a short
    assert code == 1 and r["shorts"]


def test_zero_size_via_drill_is_open(tmp_path, capsys):
    """KiCad writes a 0.00001 mm via drill as T1C0.000: nothing is drilled, so the via joins no layers."""
    b = make_board(tmp_path)
    drl = b["gerbers"] / "board.drl"
    drl.write_text(drl.read_text().replace("T1C0.300", "T1C0.000"))
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    r = json.loads(out.read_text())
    assert code == 1 and [o["net"] for o in r["opens"]] == ["B"] and len(r["tiny_holes"]) == 1
    assert "smaller than 0.01 mm" in capsys.readouterr().out


DRILL_HEAD = ("M48\n; #@! TF.FileFunction,MixedPlating,1,2\nFMAT,2\n{units}\n"
              "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n"
              "; #@! TA.AperFunction,Plated,PTH,ComponentDrill\nT2C0.800\n%\nG90\nG05\n")


@pytest.mark.parametrize("units, coords", [
    # KiCad "suppress leading zeros": METRIC,TZ, numbers right-aligned
    ("; FORMAT={3:3/ absolute / metric / suppress leading zeros}\nMETRIC,TZ", ("X15000Y14000", "X19000Y10000", "X21000Y10000")),
    # KiCad "suppress trailing zeros": METRIC,LZ, numbers left-aligned
    ("; FORMAT={3:3/ absolute / metric / suppress trailing zeros}\nMETRIC,LZ", ("X015Y014", "X019Y01", "X021Y01")),
    # keep zeros, digit format from a standard header instead of KiCad's comment
    ("METRIC,TZ,000.000", ("X015000Y014000", "X019000Y010000", "X021000Y010000")),
])
def test_integer_excellon_coordinates(tmp_path, units, coords):
    b = make_board(tmp_path)
    via, p1, p2 = coords
    (b["gerbers"] / "board.drl").write_text(DRILL_HEAD.format(units=units) + f"T1\n{via}\nT2\n{p1}\n{p2}\nM30\n")
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    assert code == 0 and json.loads(out.read_text())["ok"]


def test_integer_excellon_without_digit_format_cannot_pass(tmp_path, capsys):
    b = make_board(tmp_path)
    (b["gerbers"] / "board.drl").write_text(DRILL_HEAD.format(units="METRIC") + "T1\nX15000Y14000\nT2\nX19000Y10000\nX21000Y10000\nM30\n")
    assert main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"])]) == 2
    assert "no digit format" in capsys.readouterr().err


@pytest.mark.parametrize("via, expect", [(True, 0), (False, 1)])
def test_drill_file_written_as_gerber(tmp_path, via, expect):
    """KiCad's 'drill as Gerber' output: one Gerber X2 file per plating kind (label Drill, Rout or Mixed)."""
    b = make_board(tmp_path)
    (b["gerbers"] / "board.drl").unlink()
    body = "%ADD10C,0.300000*%\n%ADD11C,0.800000*%\n" + ("D10*\nX15000000Y14000000D03*\n" if via else "")
    body += "D11*\nX19000000Y10000000D03*\nX21000000Y10000000D03*\nM02*\n"
    (b["gerbers"] / "board-PTH-drl.gbr").write_text("%TF.FileFunction,Plated,1,2,PTH,Mixed*%\n%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n" + body)
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    assert code == expect
    if not via:
        assert [o["net"] for o in json.loads(out.read_text())["opens"]] == ["B"]


def test_pins_on_one_unconnected_net_may_share_copper(tmp_path):
    """KiCad's netlist puts no-connect pins of one node (e.g. two PHASE pins of a regulator) on one shared
    "unconnected-(...)" net; copper joining them is not a short. A pin with no net at all must still sit alone."""
    b = make_board(tmp_path)
    t = b["netlist"].read_text()
    b["netlist"].write_text(t.replace('name="/B"', 'name="unconnected-(R1-Pad1)"'))
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)])
    assert code == 0 and json.loads(out.read_text())["shorts"] == []


def test_pad_without_symbol_pin_is_not_a_no_connect(tmp_path):
    """A footprint pad the symbol has no pin for (a MOSFET's drain tab) may join copper; a real no-connect pin may not."""
    b = make_board(tmp_path, {"nc_short"})          # U1.1 joined to net B by copper
    t = b["netlist"].read_text()
    with_pins = lambda pins: t.replace('<comp ref="U1"/>', '<comp ref="U1"><libsource lib="T" part="TP"/></comp>').replace(
        "<nets>", f'<libparts><libpart lib="T" part="TP"><pins>{pins}</pins></libpart></libparts><nets>')
    b["netlist"].write_text(with_pins('<pin num="2"/>'))   # the symbol has no pin 1: U1.1 is a bare pad
    out = tmp_path / "r.json"
    args = ["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)]
    assert main(args) == 0 and json.loads(out.read_text())["pads_without_pin"] == [{"pad": "U1.1", "joins": ["B"]}]
    b["netlist"].write_text(with_pins('<pin num="1"/>'))   # pin 1 exists and is not connected: it must sit alone
    assert main(args) == 1 and json.loads(out.read_text())["shorts"][0]["nets"] == ["B", "unconnected:U1.1"]


def test_pad_without_symbol_pin_alone_is_not_reported(tmp_path):
    b = make_board(tmp_path)                        # U1.1 sits alone
    t = b["netlist"].read_text()
    b["netlist"].write_text(t.replace('<comp ref="U1"/>', '<comp ref="U1"><libsource lib="T" part="TP"/></comp>').replace(
        "<nets>", '<libparts><libpart lib="T" part="TP"><pins><pin num="2"/></pins></libpart></libparts><nets>'))
    out = tmp_path / "r.json"
    assert main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["pads_without_pin"] == []


def test_shifted_bottom_layer_breaks_holes_out_of_their_pads(tmp_path):
    """A whole layer shifted 0.5 mm: R2's 1.6 mm pads still touch their 0.8 mm holes, so connectivity there is intact
    and only BREAKOUT shows it (found by a fault matrix on a public KiCad demo board). The 0.6 mm via pad no longer
    touches its hole at all, so net B is also OPEN."""
    code, r = run(tmp_path, {"shift_bottom"})
    assert code == 1 and not r["shorts"] and [o["net"] for o in r["opens"]] == ["B"]
    assert {b["layer"] for b in r["breakouts"]} == {"B.Cu"} and len(r["breakouts"]) == 2   # the two R2 holes


def test_clean_board_has_no_breakouts(tmp_path):
    code, r = run(tmp_path)
    assert code == 0 and r["breakouts"] == []


def test_fill_without_limits_is_not_a_pass(tmp_path, capsys):
    """Nothing was judged, so the output must not say PASS (it once did)."""
    g = make_plane_board(tmp_path, filled=False)
    out = tmp_path / "f.json"
    assert main(["fill", str(g), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "MEASURED ONLY — not judged" in text and "PASS" not in text
    assert json.loads(out.read_text())["judged"] is False


def test_fill_limit_on_a_missing_layer_cannot_pass(tmp_path, capsys):
    """A typo in a layer name ('In1.cu') once made the requested judgment silently disappear."""
    g = make_plane_board(tmp_path, filled=False)
    assert main(["fill", str(g), "--min", "In1.cu=0.8"]) == 2
    assert "not in the files: In1.cu" in capsys.readouterr().err


def test_dfm_without_rules_is_not_judged(tmp_path, capsys):
    b = make_board(tmp_path)
    out = tmp_path / "d.json"
    assert main(["dfm", str(b["gerbers"]), "--json", str(out)]) == 0
    assert "MEASURED ONLY — not judged" in capsys.readouterr().out
    assert json.loads(out.read_text())["judged"] is False


def _dfm(tmp_path, rules, *extra, pcb=True):
    b = make_board(tmp_path)
    rf = tmp_path / "rules.json"
    rf.write_text(json.dumps(rules))
    out = tmp_path / "d.json"
    args = ["dfm", str(b["gerbers"]), "--rules", str(rf), "--json", str(out), *extra] + (["--pcb", str(b["pcb"])] if pcb else [])
    code = main(args)
    return code, (json.loads(out.read_text()) if out.exists() else None)


def test_dfm_on_a_folder_without_copper_cannot_pass(tmp_path, capsys):
    """An empty upload folder once printed PASS with a rules file."""
    (tmp_path / "empty").mkdir()
    rf = tmp_path / "rules.json"
    rf.write_text('{"min_trace_width_mm": 0.1}')
    assert main(["dfm", str(tmp_path / "empty"), "--rules", str(rf)]) == 2
    assert "no copper layers" in capsys.readouterr().err


@pytest.mark.parametrize("rules, pcb, reason", [
    ({"min_spacing": 0.1}, True, "unknown rule name"),                        # a typo: once silently ignored
    ({"min_via_annular_ring_mm": 0.1}, False, "needs --pcb"),
    ({"min_silk_line_mm": 0.1}, True, "no silkscreen"),                      # the synthetic board has no silk file
    ({"min_trace_width_mm": None, "_source": "x"}, True, "every rule in the file is null"),
])
def test_dfm_rules_that_cannot_be_judged(tmp_path, rules, pcb, reason):
    code, m = _dfm(tmp_path, rules, pcb=pcb)
    assert code == 2 and any(reason in nj["reason"] for nj in m["not_judged"])


def test_dfm_violation_wins_over_not_judged(tmp_path):
    code, m = _dfm(tmp_path, {"min_via_annular_ring_mm": 0.2, "min_silk_line_mm": 0.1})
    assert code == 1 and m["violations"] and m["not_judged"]


def test_dfm_gap_window_reaches_past_the_spacing_rule(tmp_path):
    """Gaps are searched only up to the window: with a 0.3 mm window the 0.4 mm gap between R2's pads was never seen,
    and a 0.5 mm spacing rule passed. The window now grows to cover the rule."""
    code, m = _dfm(tmp_path, {"min_spacing_mm": 0.5}, "--gap-window", "0.3")
    assert code == 1 and m["gap_window_mm"] == pytest.approx(0.55)
    assert m["layers"]["F.Cu"]["min_gap_between_conductors_mm"] == pytest.approx(0.4)
