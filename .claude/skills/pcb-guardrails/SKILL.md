---
name: pcb-guardrails
description: Check PCB fab files (Gerber + drill) against the schematic before they are uploaded or handed over. Use after the final Gerber export, before ordering, after any change to a schematic or board, or when asked whether a board is ready for fabrication.
---

# Checking fab files with pcb-guardrails

Run the checks on **the exact files that will be uploaded** (unzip the archive into a folder first). Do not
re-export to "make it pass" without saying so: the point is to check what the fab will receive.

## Steps

1. Export the schematic netlist (KiCad):
   `kicad-cli sch export netlist --format kicadxml -o <tmp>/board.net.xml <board>.kicad_sch`
2. Classify the files: `pcb-guardrails layers <gerber-folder>` — confirm every copper layer, the outline and the
   drill file(s) are present.
3. Connectivity: `pcb-guardrails netcheck <gerber-folder> --netlist <tmp>/board.net.xml --pcb <board>.kicad_pcb --format agent --json <tmp>/netcheck.json`
   — read the agent report, follow its "next" steps and RULES, and pass its problems to the human as listed.
   For a board from another CAD tool, use its IPC-D-356 netlist instead: `--ipc356 <board>.ipc` (no `--netlist` /
   `--pcb`). Tell the human that this compares with the board's nets, not the schematic's, and quote the offset
   and the notes the report prints.
4. Planes: `pcb-guardrails fill <gerber-folder> --min <plane layer>=<ratio>` for each layer that should be a
   pour (ask the human for the layers if the stack-up is not documented).
5. Manufacturing minimums: `pcb-guardrails dfm <gerber-folder> --pcb <board>.kicad_pcb --rules <rules.json>` —
   only with a rules file the human filled from the fab's current capabilities. Without one, report the
   measurements and say they were not judged. Copper outside the board (`EDGE` lines) fails with or without
   rules; pass a castellation `NOTE` to the human, since castellation must be ordered from the fab.
6. Design database (when the `.kicad_sch` / `.kicad_pcb` are available):
   `pcb-guardrails wrongnet <board>.kicad_pcb`, and when the project has a `parts.csv`:
   `pcb-guardrails dnp --parts parts.csv --sch <board>.kicad_sch --pcb <board>.kicad_pcb` and
   `pcb-guardrails libcheck --parts parts.csv --sch <board>.kicad_sch --kicad-share <dir> --global-tables <dir>`.
   Ask the human for `--exclusive` groups if alternatives are documented but not listed.
7. Design freeze (when the project has a freeze record): `pcb-guardrails freeze check <freeze.json>` after every
   tool run that could write files. Report EQUIVALENT files as rewritten (not as unchanged) and CHANGED files with
   their listed changes. Do not re-record (`freeze record --force`) to make it pass; that is the human's decision.

Write outputs to a scratch/temporary folder, not into the project, unless asked.

## Reading the result

- Exit 0: PASS. Report the summary lines (pads, nets, OPEN/SHORT/NO-COPPER counts, smallest gaps).
- Exit 1: FAIL. Report every OPEN / SHORT / NO-COPPER / violation with its pads and coordinates. **Do not edit
  Gerbers or the board to fix it**; explain the likely cause and ask how to proceed.
- Exit 2: NOT CHECKED. Report the message verbatim. Never describe it as passing or "probably fine".
- `pads of parts not in the schematic` and `copper islands without pads` are warnings: list them and let the
  human decide.

## Never

- Never run a command that saves over a production `.kicad_pcb` (for example DRC with a save option) as part of
  checking; use a copy.
- Never change fab rules values from memory; they come from the fab's page via the human.
