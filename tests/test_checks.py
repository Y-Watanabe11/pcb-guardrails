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
