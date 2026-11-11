"""DNP (do not populate) consistency between the parts list, the schematic and the board.

Each generated file can be self-consistent while a DNP decision never reached it (a note in the source was worded
in a way the generator did not recognise). This compares the flag part by part across every source given:

    parts.csv   the ``dnp`` column (strict yes/no; anything else stops the check)
    schematic   ``(dnp yes)`` on the symbol
    board       ``dnp`` in the footprint's ``(attr ...)``; board-only footprints (mounting holes, logos) are skipped

It also reports parts present in one source and missing from another, and, for each ``--exclusive A,B[,C]`` group
(alternatives that must never be fitted together, e.g. two supplies on one rail), any source that populates more
than one of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .gerber import UnsupportedInput
from .kicad import PcbFootprint, SchSymbol
from .parts import Part, natural_key


@dataclass
class DnpResult:
    sources: dict[str, dict[str, bool]]
    fails: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fails

    def to_dict(self) -> dict:
        return {"ok": self.ok, "dnp": {s: sorted(r for r, d in m.items() if d) for s, m in self.sources.items()},
                "parts": {s: len(m) for s, m in self.sources.items()}, "fails": self.fails, "warnings": self.warns}


def run_dnp(parts: list[Part] | None, sch: list[SchSymbol] | None, pcb: list[PcbFootprint] | None,
            exclusive: list[list[str]] = ()) -> DnpResult:
    sources: dict[str, dict[str, bool]] = {}
    fails: list[str] = []
    warns: list[str] = []
    if parts is not None:
        if any(p.dnp is None for p in parts):
            raise UnsupportedInput("the parts list has no dnp column")
        sources["parts"] = {p.ref: bool(p.dnp) for p in parts}
    off_board: set[str] = set()
    if sch is not None:
        m: dict[str, bool] = {}
        for s in sch:
            if s.ref in m and m[s.ref] != s.dnp:
                fails.append(f"{s.ref}: units of one part disagree on DNP in the schematic")
            m[s.ref] = m.get(s.ref, False) or s.dnp
            if not s.on_board:
                off_board.add(s.ref)
        sources["schematic"] = m
    if pcb is not None:
        sources["board"] = {f.ref: f.dnp for f in pcb if not f.board_only}
    if len(sources) < 2 and not exclusive:
        raise UnsupportedInput("give at least two of --parts / --sch / --pcb (or --exclusive groups) to compare")

    names = list(sources)
    design = next((n for n in ("parts", "schematic") if n in sources), None)
    if "parts" in sources and "schematic" in sources:
        a, b = sources["parts"], sources["schematic"]
        fails += [f"{r}: in the parts list but not in the schematic" for r in sorted(set(a) - set(b), key=natural_key)]
        fails += [f"{r}: in the schematic but not in the parts list" for r in sorted(set(b) - set(a), key=natural_key)]
    if "board" in sources and design:
        a, b = sources[design], sources["board"]
        fails += [f"{r}: in the {'parts list' if design == 'parts' else 'schematic'} but not on the board"
                  for r in sorted(set(a) - set(b) - off_board, key=natural_key)]
        warns += [f"{r}: on the board but not in the {'parts list' if design == 'parts' else 'schematic'}"
                  for r in sorted(set(b) - set(a), key=natural_key)]

    for ref in sorted(set().union(*sources.values()), key=natural_key):
        vals = {n: sources[n][ref] for n in names if ref in sources[n]}
        if len(set(vals.values())) > 1:
            fails.append(f"{ref}: DNP differs: " + ", ".join(f"{n} {'DNP' if v else 'fitted'}" for n, v in vals.items()))

    for group in exclusive:
        unknown = [r for r in group if not any(r in m for m in sources.values())]
        if unknown:
            raise UnsupportedInput(f"--exclusive {','.join(group)}: {', '.join(unknown)} not found in any source")
        for n in names:
            fitted = [r for r in group if r in sources[n] and not sources[n][r]]
            if len(fitted) > 1:
                fails.append(f"{' / '.join(fitted)}: fitted together in the {n} (--exclusive {','.join(group)})")
    return DnpResult(sources, fails, warns)
