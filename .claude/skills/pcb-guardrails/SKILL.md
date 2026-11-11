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
3. Connectivity: `pcb-guardrails netcheck <gerber-folder> --netlist <tmp>/board.net.xml --pcb <board>.kicad_pcb --json <tmp>/netcheck.json`
4. Planes: `pcb-guardrails fill <gerber-folder> --min <plane layer>=<ratio>` for each layer that should be a
   pour (ask the human for the layers if the stack-up is not documented).
5. Manufacturing minimums: `pcb-guardrails dfm <gerber-folder> --pcb <board>.kicad_pcb --rules <rules.json>` —
   only with a rules file the human filled from the fab's current capabilities. Without one, report the
   measurements and say they were not judged.

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
