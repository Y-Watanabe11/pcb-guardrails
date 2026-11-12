# Rules for AI agents designing or editing PCBs

These rules come from a real board designed with an AI coding agent. Each one exists because its absence cost
time or nearly cost hardware. Put this file (or a link to it) where your agent reads its instructions
(`CLAUDE.md`, `AGENTS.md`, system prompt).

## 1. Facts come from documents, never from memory

- **Never write a pin number, pin name, footprint pad, or package dimension from memory or by guessing.**
  Read the datasheet or the library file, and record the source (document, revision, page) next to the value.
  If you cannot read the source, stop and ask.
- Check that every symbol, footprint and pin name you are about to use exists in the libraries on disk before
  generating anything. Do not substitute a generic symbol for a real part because it is easier to place:
  that silently deletes design information.
  `pcb-guardrails libcheck --parts parts.csv` does the existence and pin/pad-number part of this.
- Active parts: confirm the minimum supply voltage from the datasheet against the **worst-case** rail, not the
  nominal one. Part suggestions written for an earlier rail plan are a common trap.
- Fab capabilities (trace/space, drill, annular ring) come from the fab's current published page for the exact
  layer count and service, never from memory.

## 2. One source of truth, and generate from it

- Keep connectivity in one human-reviewable place (a netlist table or the schematic) and generate the rest from
  it. To change the circuit, change the source and regenerate; do not hand-patch generated files.
- Keep DNP decisions in the source as an explicit yes/no (`parts.csv` `dnp` column), never as prose; check them
  with `pcb-guardrails dnp` after every regeneration.
- Passing every automated check only proves the source was transcribed faithfully. It does not prove the
  source is right (a wrong resistor value or a reversed diode passes). Put review effort on the source.

## 3. Verify the deliverable, not the database

- Run `pcb-guardrails netcheck`, `fill` and `dfm` on **the exact files you will upload**, after the final export.
- Exit code 2 means *not checked*. Report it as such. Never summarise it as "probably fine".
- Prove a check can fail: when you add or change a checker, inject the fault it targets and confirm it is caught.
- A clean DRC is not a clean board. Gerbers plotted from a board with unfilled zones are valid files without the
  ground plane, and no tool complains.

## 4. Do not damage production files

- Before overwriting any file, confirm it is committed (or backed up) and look at what is there.
- Run anything that writes (DRC with "save", zone refill with save, format upgrades, regenerators) on a **copy**.
  One CLI "save after DRC" removed every net declaration from a board file.
- After a design freeze, record a hash of each frozen file and re-check it after every tool run
  (`pcb-guardrails freeze record` / `freeze check`). Regenerating a file can change it (new UUIDs, reordered
  items) even when the geometry is identical; `freeze check` reports that as EQUIVALENT, a real change as CHANGED.
  Treat EQUIVALENT as a rewrite of a frozen file too: report it, and restore from the backup unless the human
  accepts it.
- Do not run auto-routers or bulk "fix" operations on a frozen board.

## 5. Ask before acting when the cost of being wrong is high

- Stop and ask on any ambiguity about pinout, orientation (pin 1, mirror/chirality of a footprint), polarity,
  voltage limits, or anything that can destroy the part. "Probably right" is not good enough for one-off hardware.
- Copper, footprint and hole changes after review need explicit human approval, with the reason.
- Ordering, purchasing, uploading to a fab, and commits are the human's actions unless explicitly delegated.

## 6. Leave a trail

- Mark every assumption in place (`# ASSUMPTION:`) and list them at the end of the task.
- When a decision overrides an earlier rule, record the date, who decided, and why, next to the rule.
- Report outcomes faithfully: failing checks with their output, skipped steps as skipped.
