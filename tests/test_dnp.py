"""dnp: a DNP decision must reach every source; unclear wording stops the check; exclusive parts are never fitted together."""
import json

import pytest

from pcb_guardrails.cli import main
from synth import make_project, write_parts


@pytest.fixture
def p(tmp_path):
    return make_project(tmp_path)


def run(p, *extra, sources=("--parts", "--sch", "--pcb")):
    out = p["proj"] / "dnp.json"
    args = [a for s in sources for a in (s, str(p[s[2:]]))]
    code = main(["dnp", *args, "--json", str(out), *extra])
    return code, (json.loads(out.read_text()) if out.exists() and code != 2 else None)


def test_consistent_project_passes(p):
    code, r = run(p)
    assert code == 0
    assert r["dnp"] == {"parts": ["R2"], "schematic": ["R2"], "board": ["R2"]}
    assert r["parts"] == {"parts": 5, "schematic": 5, "board": 5}   # H1 is board-only; U1/U2 come from a reused sheet
    assert r["warnings"] == []


def test_dnp_missing_from_schematic(p):
    """Incident 6: the source said DNP, the generated schematic did not."""
    p["sch"].write_text(p["sch"].read_text().replace("(dnp yes)", "(dnp no)"))
    code, r = run(p)
    assert code == 1 and r["fails"] == ["R2: DNP differs: parts DNP, schematic fitted, board DNP"]


def test_dnp_missing_from_board(p):
    p["pcb"].write_text(p["pcb"].read_text().replace("(attr smd dnp)", "(attr smd)"))
    code, r = run(p, sources=("--sch", "--pcb"))
    assert code == 1 and r["fails"] == ["R2: DNP differs: schematic DNP, board fitted"]


@pytest.mark.parametrize("value", ["DNP?", "default DNP", "see D1", "maybe"])
def test_unclear_dnp_wording_cannot_check(p, capsys, value):
    write_parts(p["parts"], [("R1", "Lib:R", "Fp:R_SMD", value)])
    assert run(p)[0] == 2
    assert "not yes/no" in capsys.readouterr().err


def test_missing_parts_between_sources(p):
    write_parts(p["parts"], [("R1", "Lib:R", "", "no"), ("R2", "Lib:RX", "", "yes"), ("C1", "Nested:C", "", "no"),
                             ("U1", "Lib:U3", "", "no"), ("R9", "Lib:R", "", "no")])
    p["pcb"].write_text(p["pcb"].read_text().replace('(property "Reference" "C1")', '(property "Reference" "C7")'))
    code, r = run(p)
    assert code == 1
    assert set(r["fails"]) == {"R9: in the parts list but not in the schematic", "U2: in the schematic but not in the parts list",
                               "C1: in the parts list but not on the board", "R9: in the parts list but not on the board"}
    assert r["warnings"] == ["C7: on the board but not in the parts list", "U2: on the board but not in the parts list"]


def test_exclusive_parts_fitted_together(p):
    assert run(p, "--exclusive", "R1,R2")[0] == 0          # R2 is DNP everywhere
    write_parts(p["parts"], [("R1", "Lib:R", "", "no"), ("R2", "Lib:RX", "", "no"), ("C1", "Nested:C", "", "no"),
                             ("U1", "Lib:U3", "", "no"), ("U2", "Lib:U3", "", "no")])
    code, r = run(p, "--exclusive", "R1,R2")
    assert code == 1 and "R1 / R2: fitted together in the parts (--exclusive R1,R2)" in r["fails"]


def test_bad_arguments_cannot_check(p):
    assert run(p, sources=("--sch",))[0] == 2                        # nothing to compare with
    assert run(p, "--exclusive", "R1,R77")[0] == 2                   # a typo in a group is not a pass
    assert run(p, "--exclusive", "R1")[0] == 2
    write_parts(p["parts"], [("R1", "Lib:R", "Fp:R_SMD")], header=("ref", "symbol", "footprint"))
    assert run(p)[0] == 2                                            # no dnp column
