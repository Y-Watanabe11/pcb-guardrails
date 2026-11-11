"""Design-freeze fingerprints: a regenerated-but-identical board is EQUIVALENT, every injected real change is CHANGED."""
import json

from pcb_guardrails.cli import main
from synth import make_board


def freeze(tmp_path, mutate=()):
    b = make_board(tmp_path, set(mutate))
    rec = tmp_path / "freeze.json"
    assert main(["freeze", "record", str(b["gerbers"]), str(b["pcb"]), str(b["netlist"]), "-o", str(rec)]) == 0
    return b, rec


def check(tmp_path, rec, *extra):
    out = tmp_path / "check.json"
    code = main(["freeze", "check", str(rec), "--json", str(out), *extra])
    r = json.loads(out.read_text())
    return code, r, {f["path"]: f for f in r["files"]}


def test_untouched_files_pass(tmp_path):
    _, rec = freeze(tmp_path)
    code, r, _ = check(tmp_path, rec)
    assert code == 0 and r["counts"]["SAME"] == 6 and r["counts"]["CHANGED"] == 0
    assert json.loads(rec.read_text())["files"]["board.kicad_pcb"]["summary"]["net"] == 3


def test_regenerated_files_are_equivalent_not_same(tmp_path):
    _, rec = freeze(tmp_path)
    make_board(tmp_path, {"regenerated"})  # new date, UUIDs, aperture/tool numbers, item order; same board
    code, r, files = check(tmp_path, rec)
    assert code == 1  # a rewrite of a frozen file is reported by default
    assert {p: f["status"] for p, f in files.items()} == {
        "board.kicad_pcb": "EQUIVALENT", "board.net.xml": "SAME", "gerbers/board-B_Cu.gbl": "EQUIVALENT",
        "gerbers/board-Edge_Cuts.gm1": "EQUIVALENT", "gerbers/board-F_Cu.gtl": "EQUIVALENT", "gerbers/board.drl": "EQUIVALENT"}
    assert check(tmp_path, rec, "--allow-equivalent")[0] == 0


def test_dropped_net_declarations_are_changed(tmp_path):
    """Incident 2: a 'DRC and save' run removed every net declaration from the board file."""
    _, rec = freeze(tmp_path)
    make_board(tmp_path, {"regenerated", "drop_nets"})
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1
    assert files["board.kicad_pcb"]["status"] == "CHANGED"
    assert files["board.kicad_pcb"]["changes"] == ["net 3 -> 0", "net_names_used 2 (unchanged)"]


def test_kicad10_net_format_is_changed_but_explained(tmp_path):
    """A KiCad 10 re-save drops the numbered net declarations and refers to nets by name. It is a real rewrite
    (CHANGED), but the summary shows the net names survived, unlike a real loss of nets."""
    _, rec = freeze(tmp_path)
    make_board(tmp_path, {"kicad10_nets"})
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1 and files["board.kicad_pcb"]["status"] == "CHANGED"
    assert files["board.kicad_pcb"]["changes"] == ["net 3 -> 0", "net_ref_form number -> name", "net_names_used 2 (unchanged)"]


def test_explicit_zero_angle_is_equivalent_but_a_rotation_is_not(tmp_path):
    b, rec = freeze(tmp_path)
    t = b["pcb"].read_text()
    assert '(property "Reference" "R1" (at 0 0 0)' in t
    b["pcb"].write_text(t.replace('(at 10 -10)', '(at 10 -10 0)'))  # KiCad re-save writes the zero angle
    assert check(tmp_path, rec)[2]["board.kicad_pcb"]["status"] == "EQUIVALENT"
    b["pcb"].write_text(t.replace('(at 10 -10)', '(at 10 -10 180)'))
    assert check(tmp_path, rec)[2]["board.kicad_pcb"]["status"] == "CHANGED"


def test_reordered_polygon_points_are_changed(tmp_path):
    b, rec = freeze(tmp_path)
    t = b["pcb"].read_text()
    b["pcb"].write_text(t.replace("(xy 1 -1) (xy 3 -1) (xy 3 -3)", "(xy 1 -1) (xy 3 -3) (xy 3 -1)").replace("cafe", "beef"))
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1 and files["board.kicad_pcb"]["status"] == "CHANGED"


def test_trace_corner_moved_10um_is_changed(tmp_path):
    b, rec = freeze(tmp_path)
    top = b["gerbers"] / "board-F_Cu.gtl"
    t = top.read_text()
    assert "X9000000Y14000000D01*" in t  # the exposed corner of trace B (9,10)-(9,14)-(15,14)
    top.write_text(t.replace("X9000000Y14000000D01*", "X9010000Y14000000D01*"))
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1 and files["gerbers/board-F_Cu.gtl"]["status"] == "CHANGED"


def test_change_hidden_under_a_pad_is_equivalent(tmp_path):
    """Moving a trace end 10 um *inside* a 1.6 mm pad leaves the copper identical: the fingerprint is geometry."""
    b, rec = freeze(tmp_path)
    top = b["gerbers"] / "board-F_Cu.gtl"
    top.write_text(top.read_text().replace("X19000000Y10000000D01*", "X19010000Y10000000D01*"))
    _, _, files = check(tmp_path, rec)
    assert files["gerbers/board-F_Cu.gtl"]["status"] == "EQUIVALENT"


def test_removed_via_drill_is_changed(tmp_path):
    _, rec = freeze(tmp_path)
    make_board(tmp_path, {"regenerated", "no_via_drill"})
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1
    assert files["gerbers/board.drl"]["status"] == "CHANGED"
    assert "holes 3 -> 2" in files["gerbers/board.drl"]["changes"]


def test_missing_and_new_files(tmp_path):
    b, rec = freeze(tmp_path)
    (b["gerbers"] / "board-B_Cu.gbl").unlink()
    (b["gerbers"] / "board-In1_Cu.g1").write_text("%FSLAX46Y46*%\n%MOMM*%\nM02*\n")
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1
    assert files["gerbers/board-B_Cu.gbl"]["status"] == "MISSING"
    assert files["gerbers/board-In1_Cu.g1"]["status"] == "NEW"


def test_unparseable_file_is_bytes_only_and_never_equivalent(tmp_path):
    _, rec = freeze(tmp_path, {"step_repeat"})
    f = json.loads(rec.read_text())["files"]["gerbers/board-F_Cu.gtl"]
    assert f["fingerprint"] is None and "step-and-repeat" in f["note"]
    make_board(tmp_path, {"step_repeat", "regenerated"})
    code, _, files = check(tmp_path, rec, "--allow-equivalent")
    assert code == 1 and files["gerbers/board-F_Cu.gtl"]["status"] == "CHANGED"


def test_record_is_not_overwritten(tmp_path):
    b, rec = freeze(tmp_path)
    before = rec.read_text()
    assert main(["freeze", "record", str(b["pcb"]), "-o", str(rec)]) == 2
    assert rec.read_text() == before


def test_bad_record_cannot_check(tmp_path, capsys):
    bad = tmp_path / "freeze.json"
    bad.write_text('{"files": {}}')
    assert main(["freeze", "check", str(bad)]) == 2
    assert "CANNOT CHECK" in capsys.readouterr().err
    assert main(["freeze", "check", str(tmp_path / "nope.json")]) == 2
