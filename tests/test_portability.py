"""Behaviour that must not depend on the platform."""
import io
import re
import sys
from pathlib import Path

from pcb_guardrails.cli import main
from synth import make_board

SRC = Path(__file__).parent.parent / "src" / "pcb_guardrails"


def test_source_names_encodings():
    """Without an encoding, Python uses the locale's (cp932 on Japanese Windows): the same file reads differently."""
    bad = []
    for f in SRC.glob("*.py"):
        lines = f.read_text(encoding="utf-8").splitlines()
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith("#") or not re.search(r"\.(read_text|write_text)\(|\bopen\(", line):
                continue
            call = " ".join(lines[no - 1:no + 2])          # a call may continue on the next lines
            if "encoding=" not in call and not re.search(r"""\bopen\([^,]+,\s*["'][rwa]?b["']""", call):
                bad.append(f"{f.name}:{no}: {line.strip()}")
    assert bad == []


def test_report_survives_a_non_utf8_pipe(tmp_path, monkeypatch):
    """An agent reads the report from a pipe; on Windows that pipe's encoding is the ANSI code page, which cannot
    encode « ». The report must come out, in UTF-8, instead of crashing."""
    b = make_board(tmp_path, {"open_A"})
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp932"))
    code = main(["netcheck", str(b["gerbers"]), "--netlist", str(b["netlist"]), "--pcb", str(b["pcb"]),
                 "--format", "agent"])
    sys.stdout.flush()
    assert code == 1 and "[OPEN] net «A»" in raw.getvalue().decode("utf-8")
