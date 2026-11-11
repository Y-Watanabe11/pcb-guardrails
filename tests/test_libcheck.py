"""libcheck: symbols / footprints must exist on disk and pin numbers must meet pad numbers; unresolvable libraries
are 'cannot check', never 'present' or 'missing'."""
import json

import pytest

from pcb_guardrails.cli import main
from synth import make_project, write_parts


def run(p, *args, share=True, mode=("--parts",)):
    out = p["proj"] / "lib.json"
    src = []
    if "--parts" in mode:
        src += ["--parts", str(p["parts"])]
    if "--sch" in mode:
        src += ["--sch", str(p["sch"])]
    extra = ["--var", f"SHARE={p['share']}"] if share else []
    code = main(["libcheck", *src, *extra, "--json", str(out), *args])
    return code, (json.loads(out.read_text()) if out.exists() and code != 2 else None)


@pytest.fixture
def p(tmp_path):
    return make_project(tmp_path)


def test_clean_parts_list_passes(p):
    code, r = run(p)
    assert code == 0 and r["symbols_ok"] == 5 and r["footprints_ok"] == 5
    assert r["warnings"] == []   # the unnumbered, non-plated and paste-only pads of SOT3 are not counted


def test_clean_schematic_passes(p):
    code, r = run(p, mode=("--sch",))
    assert code == 0 and r["items"] == 5   # R1 R2 C1 + U1 U2 from one sheet used twice; the power symbol is skipped


@pytest.mark.parametrize("row, text", [
    (("R1", "Lib:NoSuch", "Fp:R_SMD", "no"), "no symbol 'NoSuch' in library 'Lib'"),
    (("R1", "Lib:R", "Fp:NoSuch", "no"), "no NoSuch.kicad_mod"),
    (("R1", "R", "Fp:R_SMD", "no"), "not of the form Library:Name"),
    (("R1", "Off:R", "Fp:R_SMD", "no"), "library 'Off' is disabled"),
    (("R1", "Lib:U3", "Fp:R_SMD", "no"), "pins ['3'] have no pad in Fp:R_SMD"),
    (("R1", "", "Fp:R_SMD", "no"), "no symbol given"),
])
def test_each_fault_fails(p, row, text):
    write_parts(p["parts"], [row])
    code, r = run(p)
    assert code == 1
    assert [f["ref"] for f in r["fails"]] == ["R1"] and text in r["fails"][0]["problem"]


def test_pad_without_pin_is_a_warning(p):
    write_parts(p["parts"], [("U1", "Lib:U3", "Fp:TAB4", "no")])
    code, r = run(p)
    assert code == 0 and "pads ['4'] have no pin" in r["warnings"][0]["problem"]


def test_derived_symbol_uses_parent_pins_and_default_footprint(p):
    write_parts(p["parts"], [("R2", "Lib:RX", "", "")])
    code, r = run(p)
    assert code == 0 and r["footprints_ok"] == 1


def test_undefined_variable_cannot_check(p, capsys):
    code, _ = run(p, share=False)   # C1 lives in a nested table under ${SHARE}
    assert code == 2
    assert "${SHARE} is not defined" in capsys.readouterr().err


def test_library_not_in_project_table(p, tmp_path, capsys):
    write_parts(p["parts"], [("R1", "Global:R", "Fp:R_SMD", "no")])
    assert run(p)[0] == 2   # might be in the global table, which was not given
    assert "--global-tables" in capsys.readouterr().err
    g = tmp_path / "global"
    g.mkdir()
    (g / "sym-lib-table").write_text("(sym_lib_table (version 7))\n")
    (g / "fp-lib-table").write_text("(fp_lib_table (version 7))\n")
    code, r = run(p, "--global-tables", str(g))
    assert code == 1 and "not in any library table" in r["fails"][0]["problem"]


def test_schematic_without_footprint_fails(p):
    p["sch"].write_text(p["sch"].read_text().replace('"R1" (at 0 0 0)) (property "Footprint" "Fp:R_SMD"',
                                                     '"R1" (at 0 0 0)) (property "Footprint" ""'))
    code, r = run(p, mode=("--sch",))
    assert code == 1 and r["fails"] == [{"ref": "R1", "problem": "no footprint assigned in the schematic", "source": "board.kicad_sch"}]


def test_schematic_compared_with_parts_list(p):
    write_parts(p["parts"], [("R1", "Lib:RX", "Fp:R_SMD", "no"), ("R2", "Lib:RX", "", "yes"), ("C1", "Nested:C", "Fp:ONE", ""),
                             ("U1", "Lib:U3", "Fp:SOT3", "no"), ("R9", "Lib:R", "Fp:R_SMD", "no")])
    code, r = run(p, mode=("--parts", "--sch"))
    assert code == 1
    probs = {(f["ref"], f["problem"]) for f in r["fails"]}
    assert ("R1", "schematic uses Lib:R, parts list says Lib:RX") in probs
    assert ("C1", "schematic footprint Fp:R_SMD, parts list says Fp:ONE") in probs
    assert ("R9", "in the parts list but not in the schematic") in probs
    assert ("U2", "in the schematic but not in the parts list") in probs
    assert ("C1", "Nested:C pins ['2'] have no pad in Fp:ONE") in probs   # the parts list itself is checked too
