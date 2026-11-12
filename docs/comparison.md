# Comparison with FabGate

Date: 2026-11-12. FabGate 0.1.9 (PyPI, MIT), pcb-guardrails at this commit, KiCad 10.0.4 on macOS. All runs used
copies of KiCad's public demo boards (see [`public-boards.md`](public-boards.md)).

FabGate is the closest tool we know of ("Pre-fabrication quality gate for PCB projects — KiCad design gate plus
Gerber/ODB++ fab package checks, scored reports, and CI"). The two tools turn out to answer different questions,
so this is a description of coverage, not a ranking.

## How FabGate was run

Every published FabGate version we tried (0.1.5 to 0.1.9, wheels and the 0.1.9 source distribution) imports a
module `fabgate.reports` that is not in the package, so its command line does not start; the repository linked from
PyPI returned 404. Its analysis entry point `fabgate.pipeline.run_review` does not need that module, so it was
called directly, unchanged, in its own virtual environment. Report writing, scoring presentation and the sign-off
workflow could therefore not be tried.

## What each tool reads

| | FabGate | pcb-guardrails |
|---|---|---|
| KiCad project | runs `kicad-cli pcb drc --schematic-parity` and turns its findings into a scored verdict; adds layout rules (outline present, board thickness, ground / power pours, copper layer count, stitching vias) | reads the `.kicad_pcb` for pad and via positions, tracks, DNP and net ties; reads the schematic hierarchy for parts and DNP |
| Gerber / drill package | checks which files are present (top / bottom copper, outline, drill, mask, silk, empty files) | parses the geometry, rebuilds copper connectivity through plated holes and compares it with the **schematic** netlist |
| ODB++ | file presence | not supported |
| Fab rules | via KiCad's board setup (DRC) | measured; judged only against a rules file the user fills in |
| Output | verdict (pass / hold / fail), score, HTML / JSON report, audit record, sign-off | PASS / FAIL / CANNOT CHECK with exit codes 0 / 1 / 2, JSON |

## Injected faults (two boards: 2-layer through-hole and 6-layer SMD)

One fault per copy. FabGate counts as catching a fault when a finding appears that its baseline run on the
unmodified board does not have (both boards already have DRC findings, so the verdict alone says little).

| Fault | where | pcb-guardrails | FabGate (KiCad mode) | FabGate (Gerber mode) |
|---|---|---|---|---|
| a trace segment missing | Gerber only | `netcheck` OPEN | not seen (reads the board, not the files) | pass |
| a trace joining two nets | Gerber only | `netcheck` SHORT | not seen | pass |
| one via drill hit missing (a redundant GND stitching via) | drill file only | `netcheck` VIA-NO-HOLE | not seen | pass |
| bottom copper file missing | package | `netcheck` OPEN / NO-COPPER | not seen | info "Top copper found without bottom copper", verdict pass |
| bottom copper shifted 0.5 mm | Gerber only | `netcheck` BREAKOUT | not seen | pass |
| via drill size written as 0.000 | drill file only | `netcheck` (plated holes below 0.01 mm reported; nets OPEN) | not seen | pass |
| pours not filled before plotting (incident 1) | board and Gerbers | `netcheck` OPEN | "Missing connection between items" (KiCad DRC on the unfilled board) | pass |
| a track end on the neighbouring pad of another net | board, re-exported | `wrongnet` + `netcheck` SHORT | DRC findings (tracks crossing, items of different nets joined) | pass |
| a part DNP on the board only | board | `dnp` | parity: DNP attribute differs | pass |

Two of these faults first passed pcb-guardrails too: the shifted layer (a uniform shift keeps a layer's own
connectivity) and the missing stitching via (connectivity unchanged). The matrix is how they were found; `BREAKOUT`
and `VIA-NO-HOLE` were added and checked for false positives on all 20 public boards and two private boards (none).

## On the 20 public boards

- FabGate in Gerber mode gave "pass, 98" on every board, including one whose 183 vias are exported as
  zero-diameter drills (`T1C0.000`), since the files are all present.
- FabGate in KiCad mode reports what KiCad's DRC and schematic parity report, which is a lot: clearance, courtyard,
  hole-size, annular-ring, parity and net-conflict findings that pcb-guardrails does not check (its `dfm` only
  measures against a rules file). On that board it does flag the 0.00001 mm via drill as a hole-size violation.
- FabGate's layout rules said "Board uses 0 copper layer(s)" for some KiCad 10 boards, which suggests its own board
  parser does not read that format fully.

## Summary

- **Use FabGate (or KiCad's DRC directly) for**: design-rule violations in the board, schematic parity, a scored
  report and a sign-off record. It inherits KiCad's DRC, which is far broader than anything here.
- **Use pcb-guardrails for**: proving that the files you upload build the circuit in the schematic — the cases
  where the board is right but the package is not (a lost trace, an extra trace, a missing or zero-size drill, a
  missing or shifted layer, pours never filled) — and for DNP / library / wrong-pad checks with strict exit codes
  for agents.
- They overlap only where both read the KiCad board (wrong-pad tracks, DNP, unfilled pours seen through DRC).
