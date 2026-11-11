# Incidents behind the checks

Each entry is a failure seen while designing a real two-board instrument (a 2-layer power board and a 4-layer
mixed-signal/HV board) with an AI coding agent. Project-specific details are omitted.

| # | What happened | Why existing checks missed it | Guardrail |
|---|---|---|---|
| 1 | Gerbers plotted from a script-generated board had **no ground plane**. The zones had never been filled; plotting from the CLI succeeded without a warning. | DRC on the database and the plot command both succeed; the files are valid Gerber. | `fill` (copper coverage per layer) |
| 2 | A "DRC and save" CLI run on the production board file **removed every net declaration** (111 → 0). | The command reported success. | AGENTS.md §4: write-capable commands only on copies; `freeze check` reports the board as CHANGED with `net 111 -> 0` (see note below) |
| 3 | A track endpoint landed on the **neighbouring pad of the same part, on a different net** (a footprint mirrored on the back side). | Depending on overlap, it did not register as a clearance violation. | `netcheck` sees it as SHORT in the Gerbers; v0.2 adds a database-side endpoint check |
| 4 | A socket footprint's staggered hole pattern was built **mirrored** (Y axis flipped). Mirrored chiral patterns cannot be fixed by rotation. | "No overlaps" and visual inspection both looked fine. | Numeric comparison against the drawing; 1:1 print template checked against the real part (v0.3) |
| 5 | Regenerating a frozen board produced a file with **new internal IDs** although the geometry was unchanged, breaking the freeze record. | Diffs of a one-line S-expression file are unreadable. | `freeze record` / `freeze check`: byte hash plus a geometry/content fingerprint, so a rewrite is EQUIVALENT and a real change is CHANGED; restore from backup |
| 6 | A part was marked "do not populate" in the design notes, but the wording did not match the parser, so the **DNP flag never reached** the schematic or board. | Each generated file was self-consistent. | DNP consistency check across source, schematic and board (v0.2) |
| 7 | The assembly house's part library uses **different zero-rotation conventions per part number** (even within SOT-23-5), so placement previews showed rotated ICs. | The CPL was "correct" in CAD terms. | Per-part offsets measured from the assembly preview (v0.3) |
| 8 | While porting the connectivity check, **no-connect pins** turned out to be absent from the netlist, and pads with no net were being skipped — so a no-connect pin shorted to a net would have passed. | Found by reading the real board's report ("8 pads not in netlist"). | `netcheck` treats a pin of a schematic part with no net as one that must sit alone; tested |
| 9 | A copper gap of 0.141 mm appeared on a board whose different-net minimum was 0.15 mm. It was **same-net copper** (an exposed pad and an adjacent via joined only through other layers). | A per-layer measurement cannot tell same-net from different-net copper. | `dfm` reports gaps between conductors and within a conductor separately |
| 10 | Candidate parts listed from an earlier, higher-voltage rail plan **could not run on the final 3.3 V rail** (two cases). | The list looked authoritative. | AGENTS.md §1: check minimum supply against the worst-case rail |

## Note on incident 2 (2026-11-11)

While validating `freeze check` on copies of the real boards, a plain KiCad 10.0.4 re-save
(`kicad-cli pcb upgrade --force`) of a script-generated board also dropped all numbered net declarations
(110 → 0). The board was not damaged: KiCad 10 refers to nets by name (`(net "GND")`) instead of by number, and
every pad and track kept its net. `freeze check` shows this as `net_ref_form number -> name` with
`net_names_used` unchanged. A real loss of nets would change `net_names_used` instead. Whether incident 2 was
this format change or a real loss was not re-verified; the guardrail (write-capable commands only on copies,
check the frozen files afterwards) applies either way.
