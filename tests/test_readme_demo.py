"""The example output at the top of the README must be what the tool prints on examples/demo.py's boards."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pcb_guardrails.cli import main

ROOT = Path(__file__).resolve().parent.parent


def _readme_blocks():
    text = (ROOT / "README.md").read_text()
    for block in re.findall(r"```\n(\$ pcb-guardrails .*?)```", text, re.S):
        cmd, _, out = block.partition("\n")
        yield cmd[2:], out


BLOCKS = list(_readme_blocks())


def test_readme_has_demo_blocks():
    assert len(BLOCKS) >= 2


@pytest.mark.parametrize("cmd,expected", BLOCKS, ids=[c.split()[1] + ":" + c.split()[2] for c, _ in BLOCKS])
def test_readme_output_is_real(tmp_path, monkeypatch, capsys, cmd, expected):
    subprocess.run([sys.executable, str(ROOT / "examples" / "demo.py"), str(tmp_path / "demo")], check=True,
                   capture_output=True)
    monkeypatch.chdir(tmp_path)
    args = shlex.split(cmd)[1:]
    code = main(args)
    out = capsys.readouterr().out
    assert out == expected
    assert code == (1 if out.rstrip().endswith("FAIL") else 0)


def test_demo_good_board_passes(tmp_path, monkeypatch, capsys):
    subprocess.run([sys.executable, str(ROOT / "examples" / "demo.py"), str(tmp_path / "demo")], check=True,
                   capture_output=True)
    monkeypatch.chdir(tmp_path)
    code = main(["netcheck", "demo/good/gerbers", "--netlist", "demo/good/board.net.xml",
                 "--pcb", "demo/good/board.kicad_pcb"])
    assert code == 0
    assert capsys.readouterr().out.rstrip().endswith("PASS")
