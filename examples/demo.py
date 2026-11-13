"""Write the synthetic demo boards used in the README, so its example output can be reproduced from a clone.

    python examples/demo.py demo
    pcb-guardrails netcheck demo/good/gerbers  --netlist demo/good/board.net.xml  --pcb demo/good/board.kicad_pcb
    pcb-guardrails netcheck demo/novia/gerbers --netlist demo/novia/board.net.xml --pcb demo/novia/board.kicad_pcb
    pcb-guardrails fill demo/plane --min In1.Cu=0.8 --min In2.Cu=0.8

The boards come from the test suite's generator (tests/synth.py): every byte is written from scratch, with one
fault injected per folder. No real design is involved.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from synth import make_board, make_plane_board  # noqa: E402

BOARDS = {
    "good": set(),             # exported correctly
    "novia": {"no_via_drill"},  # one via lost its drill hit
    "short": {"short_AB"},      # an extra trace joins two nets
    "open": {"open_A"},         # a trace is missing
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    if root.exists() and any(root.iterdir()):
        print(f"{root} is not empty; choose a new folder")
        return 2
    for name, faults in BOARDS.items():
        make_board(root / name, faults)
    plane = make_plane_board(root / "plane_tmp", filled=False)  # 4 layers, inner planes never filled
    plane.rename(root / "plane")
    (root / "plane_tmp").rmdir()
    print(f"demo boards written to {root}: " + ", ".join([*BOARDS, "plane"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
