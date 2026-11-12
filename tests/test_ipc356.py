"""netcheck with an IPC-D-356 netlist (any CAD tool) instead of the KiCad netlist and board."""
import json

import pytest

from pcb_guardrails.cli import main
from pcb_guardrails.ipc356 import read_ipc356, to_netcheck
from synth import d356_line, make_board, make_d356


def run(tmp_path, capsys, mutate=(), args=(), **kw):
    b = make_board(tmp_path, set(mutate))
    ipc = make_d356(tmp_path / "board.ipc", **kw)
    out = tmp_path / "r.json"
    code = main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--json", str(out), *args])
    text = capsys.readouterr()
    return code, (json.loads(out.read_text()) if out.exists() else None), text.out + text.err


def test_clean_board_passes_and_origin_is_found(tmp_path, capsys):
    code, r, out = run(tmp_path, capsys)
    assert code == 0 and r["ok"] and r["nets"] == 2 and r["pads"] == 5
    assert "shifted by (5.000, -3.000) mm" in out and "3 of 3 plated holes" in out
    assert "not the schematic's" in out


@pytest.mark.parametrize("fault, key", [("open_A", "opens"), ("short_AB", "shorts"), ("nc_short", "shorts"),
                                        ("no_bottom", "opens"), ("shift_bottom", "breakouts")])
def test_faults_are_caught(tmp_path, capsys, fault, key):
    code, r, _ = run(tmp_path, capsys, {fault})
    assert code == 1 and r[key]


def test_unconnected_pad_must_sit_alone(tmp_path, capsys):
    code, r, _ = run(tmp_path, capsys, {"nc_short"})
    assert r["shorts"][0]["nets"] == ["/B", "unconnected:U1.1"]


def test_missing_via_hole(tmp_path, capsys):
    """With one of three holes gone, 2 of 3 is below the alignment threshold: the origin must then be given."""
    code, _, out = run(tmp_path, capsys, {"no_via_drill"})
    assert code == 2 and "matches 2 of 3 plated holes" in out
    code, r, _ = run(tmp_path / "b", capsys, {"no_via_drill"}, args=["--ipc356-offset", "5,-3"])
    assert code == 1 and len(r["vias_without_hole"]) == 1


def test_other_units_cannot_be_checked(tmp_path, capsys):
    code, _, out = run(tmp_path, capsys, units="CUST 1")
    assert code == 2 and "units 'CUST 1' are not supported" in out


def test_unknown_record_cannot_be_checked(tmp_path, capsys):
    blind = d356_line(307, "/B", "VIA", "", (10, 4), (0.3, "P"), 0)
    code, _, out = run(tmp_path, capsys, extra=[blind])
    assert code == 2 and "record type 307 is not supported" in out


def test_no_holes_to_align_cannot_be_checked_unless_offset_given(tmp_path, capsys):
    b = make_board(tmp_path)
    ipc = make_d356(tmp_path / "board.ipc", drop={"R2.1", "R2.2", "via"})
    assert main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc)]) == 2
    assert "pass --ipc356-offset" in capsys.readouterr().err
    assert main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--ipc356-offset", "5,-3"]) == 0


def test_wrong_offset_puts_pads_off_their_copper(tmp_path, capsys):
    b = make_board(tmp_path)
    ipc = make_d356(tmp_path / "board.ipc")
    assert main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--ipc356-offset", "0,0"]) == 1


def test_eagle_style_fields(tmp_path):
    """Spacing as in the EAGLE ipc-d-356.ulp format strings: padded numbers after the field letters."""
    p = tmp_path / "e.ipc"
    p.write_text("P  UNITS CUST 0\n"
                 "317GND              J1    -1    D  39PA00X+  1234Y+  5678X 650Y 650R  0 S0\n"
                 "327VCC              U1    -2          A16X+ 20000Y- 1000X 300Y 500R 90 S0\n999\n")
    d = read_ipc356(p)
    assert [(r.op, r.net, r.ref, r.pin, r.plated, r.access) for r in d.records] == [
        (317, "GND", "J1", "1", True, 0), (327, "VCC", "U1", "2", None, 16)]
    assert d.records[0].xy == pytest.approx((1234 * 0.00254, 5678 * 0.00254))
    assert d.records[1].xy == pytest.approx((20000 * 0.00254, -1000 * 0.00254))
    pads, _, _, _, _ = to_netcheck(d, (0, 0), 2)
    assert pads[1].layers == ("B.Cu",)    # A16 on a two-layer board: the bottom


def test_references_cut_to_six_characters_are_kept_apart(tmp_path):
    p = tmp_path / "c.ipc"
    p.write_text("P  UNITS CUST 0\n"
                 + d356_line(327, "VBAT", "Battery1", "1", (1, 1), None, 1) + "\n"
                 + d356_line(327, "VBAT2", "Battery2", "1", (5, 1), None, 1) + "\n999\n")
    pads, _, netlist, notes, _ = to_netcheck(read_ipc356(p), (0, 0), 2)
    assert sorted(netlist.pins.values()) == ["VBAT", "VBAT2"] and len(netlist.pins) == 2
    assert any("cut to 6 characters" in n for n in notes)


def test_full_width_net_names_are_flagged(tmp_path):
    p = tmp_path / "n.ipc"
    p.write_text("P  UNITS CUST 0\n" + d356_line(327, "ET-(U102-CAP+)", "C1", "1", (1, 1), None, 1) + "\n999\n")
    _, _, _, notes, _ = to_netcheck(read_ipc356(p), (0, 0), 2)
    assert any("may have been cut short" in n for n in notes)


def test_netlist_and_ipc356_together_are_refused(tmp_path, capsys):
    b = make_board(tmp_path)
    ipc = make_d356(tmp_path / "board.ipc")
    assert main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--netlist", str(b["netlist"])]) == 2


def test_one_pad_number_repeated_with_different_nets_is_a_warning(tmp_path, capsys):
    """KiCad gives each copy of an unconnected, repeated pad number its own net (a thermal pad and its vias), and the
    footprint's copper joins them. Not a design fault: reported as a warning, not a SHORT."""
    b = make_board(tmp_path, {"nc_short"})   # copper joins R2.2 and U1.1
    u1 = d356_line(327, "UNC-(U1-EP)_1", "U1", "1", (25 - 5, 10 + 3), None, 1)
    r22 = d356_line(317, "UNC-(U1-EP)_2", "U1", "1", (21 - 5, 10 + 3), (0.8, "P"), 0)   # R2.2's pad, renamed U1.1
    ipc = make_d356(tmp_path / "board.ipc", drop={"U1.1", "R2.2", "R1.1", "via"}, extra=[u1, r22])
    code = main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--ipc356-offset", "5,-3", "--format", "agent"])
    out = capsys.readouterr().out
    assert code == 0 and "share one piece of copper (one part repeating a pad number" in out and "[SHORT]" not in out
    assert "vs. the board's IPC-D-356 netlist" in out


def test_two_parts_cut_to_one_name_still_short(tmp_path, capsys):
    """Same cut name, but other pads are on the copper too: a real SHORT stays a SHORT."""
    b = make_board(tmp_path, {"nc_short"})
    u1 = d356_line(327, "N1", "U1", "1", (25 - 5, 10 + 3), None, 1)
    r22 = d356_line(317, "/B", "U1", "1", (21 - 5, 10 + 3), (0.8, "P"), 0)
    ipc = make_d356(tmp_path / "board.ipc", drop={"U1.1", "R2.2"}, extra=[u1, r22])
    assert main(["netcheck", str(b["gerbers"]), "--ipc356", str(ipc), "--ipc356-offset", "5,-3"]) == 1
