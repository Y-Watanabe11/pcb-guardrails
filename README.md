# pcb-guardrails

Verification guardrails for AI-assisted PCB design — independent checks of what the fab actually receives
(Gerber + drill vs. schematic netlist), each born from a real failure.

An AI agent (or a tired human) can produce a board whose design database looks perfect while the files sent to
the fab are wrong: a pour that was never filled, a layer that was not exported, a PCB that drifted from the
schematic, a via whose drill went missing. CAD-internal checks (DRC, ERC) look at the database. This tool looks
at **the deliverable**, rebuilds it from scratch, and compares it with the schematic.

```
$ pcb-guardrails netcheck fab/gerbers --netlist board.net.xml --pcb board.kicad_pcb
pads 412, schematic nets with pads 68, copper layers 4, drill files 1
OPEN 0 / SHORT 1 / NO-COPPER 0 / pads of parts not in the schematic 0 / copper islands without pads 0
  SHORT +3V3 + SPI_CLK: C12.1, R7.2, U3.5
  F.Cu: smallest gap between different copper 0.15 mm (GND / +3V3) at [41.2, 33.85]
FAIL
```

## What it checks

| Command | Catches | How |
|---|---|---|
| `netcheck` | **OPEN** (a net split into pieces), **SHORT** (two nets on one piece of copper), **NO-COPPER** (a pad with no copper in the files), no-connect pins touching a net | Parses the Gerber copper layers and Excellon drills into geometry, joins copper through plated holes, and compares the resulting pieces with the **schematic** netlist. The CAD tool's connectivity engine is never used. Pad *positions* come from the `.kicad_pcb`; its nets are ignored. |
| `fill` | Pours / planes missing from the Gerbers (e.g. zones never filled before plotting — this exports cleanly, with no error) | Copper area per layer relative to the board area; fail below `--min LAYER=RATIO`. |
| `dfm` | Values below your fab's minimums | Measures trace widths, gaps **between conductors** and **within one conductor**, copper-to-edge, drill sizes, hole-to-hole, hole-to-edge, via and PTH annular rings, silk line widths. Judges only against a rules file **you** fill in (`--rules`). |
| `freeze` | A frozen file rewritten or changed by a later tool run (a "save" that dropped nets, a regenerated board, a re-export into the upload folder) | `freeze record` stores a SHA-256 and a fingerprint of what each file means; `freeze check` reports SAME / **EQUIVALENT** (bytes differ, geometry or content identical: new UUIDs, dates, aperture numbers, item order) / **CHANGED** / MISSING / NEW. |
| `layers` | Wrong or missing files in the upload folder | Shows how each file was classified (X2 `FileFunction`, falling back to Protel/KiCad extensions). |

Every check is proven by tests that inject one fault each into a small synthetic board — a deleted trace, an
extra trace, a removed via drill, a missing layer file, a shifted layer, a schematic that changed after layout,
a no-connect pin tied to a net, a clear-polarity cut, an unsupported Gerber feature — and assert it is reported.

## Exit codes (stable, for agents and CI)

| Code | Meaning |
|---|---|
| 0 | all checks passed |
| 1 | a check failed |
| 2 | **the input could not be checked** (unsupported Gerber feature, missing file, bad arguments). Never read this as "probably fine". |

Unknown Gerber constructs stop the check with exit 2 instead of being skipped: a checker that ignores what it
does not understand will report a broken board as clean.

## Install

```bash
pip install -e .            # from a clone
pip install -e ".[dev]"     # with pytest
```

Requires Python ≥ 3.10, `shapely` ≥ 2 and `sexpdata`.

## Quick start (KiCad)

```bash
kicad-cli sch export netlist --format kicadxml -o board.net.xml board.kicad_sch
pcb-guardrails layers   fab/gerbers
pcb-guardrails netcheck fab/gerbers --netlist board.net.xml --pcb board.kicad_pcb --json netcheck.json --net-csv nets.csv
pcb-guardrails fill     fab/gerbers --min In1.Cu=0.8          # a solid inner ground plane
pcb-guardrails dfm      fab/gerbers --pcb board.kicad_pcb --rules my_fab_rules.json
```

### Design freeze

```bash
pcb-guardrails freeze record fab/gerbers board.kicad_pcb board.kicad_sch -o freeze.json   # at the freeze
pcb-guardrails freeze check freeze.json        # after every tool run; exit 1 on any rewrite or change
pcb-guardrails freeze check freeze.json --allow-equivalent   # accept rewrites that changed nothing real
```

Folders are recorded non-recursively (hidden files skipped) and new files in them are reported. Paths are stored
relative to the record, so commit it next to the files. What the fingerprint ignores:

| File | Fingerprint covers | Ignores |
|---|---|---|
| Gerber | the geometry after parsing (union of all objects, 0.1 µm grid) and the X2 file function | comments, attributes, dates, aperture numbers, object order, how the copper is split into objects |
| Excellon | every hole: plating, diameter, position, slot extent; the file function | comments, tool numbers, hole order |
| `.gbrjob` | the job JSON | `Header.CreationDate`, `Header.GenerationSoftware` |
| `.kicad_pcb` / `_sch` / `_sym` / `_mod` | the S-expression | UUID values, `uuid` / `tstamp` / `generator` entries, sibling order (kept for polygon points, stack-up, table cells), quoted vs unquoted words, `1` vs `1.0`, `(at x y 0)` vs `(at x y)` |
| anything else | bytes only | nothing |

A changed KiCad board also lists its item counts that changed (`net 110 -> 0`, `segment 412 -> 411`) and, for
boards, the number of net names used and the form of net references. A KiCad 10 format upgrade shows
`net_ref_form number -> name` with `net_names_used` unchanged; a real loss of nets changes `net_names_used`.
A format upgrade is reported as CHANGED, not EQUIVALENT: it is a different file the next tool will read.

Point the commands at **the exact folder you upload** (unzip the archive first), not at a fresh export.

### Fab rules

No fab rules are built in. Capabilities change and depend on layer count, copper weight and service tier, and
a wrong number baked into a checker is worse than none. Copy [`examples/rules.example.json`](examples/rules.example.json),
fill it from your fab's current capabilities page, and pass it with `--rules`.

## For AI agents

- [`AGENTS.md`](AGENTS.md) — rules for an agent that designs or edits PCBs (pin numbers, sources of truth, what
  never to run on production files, when to stop and ask).
- [`.claude/skills/pcb-guardrails/SKILL.md`](.claude/skills/pcb-guardrails/SKILL.md) — a Claude Code skill that
  runs the checks before fab files are handed over and reports the result without "fixing" the Gerbers.
- [`docs/incidents.md`](docs/incidents.md) — the failures behind each check.

## Limitations

- KiCad only for pad positions and the netlist format (Gerber/drill parsing itself is CAD-neutral).
- Does not check symbol-pin ↔ footprint-pad mapping, footprint geometry against a datasheet, impedance,
  current capacity, creepage/clearance for high voltage, or anything electrical beyond connectivity.
- Supported Gerber subset: RS-274X with X2 attributes, `%FSLA%`, mm or inch, circular/rect/obround/polygon
  apertures, aperture macros (primitives 1, 4, 5, 20, 21), regions, multi-quadrant arcs, dark and clear
  polarity. Step-and-repeat, image transforms (`%LM/LR/LS%`) and single-quadrant arcs stop with exit 2.
- Excellon: decimal coordinates, plating from X2 `TA.AperFunction` or the file function; drilled and routed slots.
- `freeze`: EQUIVALENT ignores UUID values, so a changed symbol ↔ footprint link (`path`) alone is not reported.
  A Gerber change hidden under other copper (a trace end moved inside a pad) is EQUIVALENT, because the copper
  is identical. Files the fingerprint cannot parse fall back to bytes only and are never EQUIVALENT.

## Roadmap

- v0.2: design-freeze fingerprints (done); schematic/PCB guardrails — library existence check before generation,
  track endpoints on wrong-net pads, DNP consistency.
- v0.3: assembly — JLCPCB BOM/CPL helpers and per-part rotation offsets, 1:1 footprint print templates,
  headless LTspice batch runs.

Related: [FabGate](https://pypi.org/project/fabgate/) (pre-fabrication gate for KiCad projects). pcb-guardrails
focuses on rebuilding connectivity from the fab files alone and on rules for AI agents.

## 日本語の概要

基板メーカーに渡すファイル（ガーバーとドリル）だけから銅のつながりを組み直し、回路図のネットリストと照合する
ツールです。CAD の中の検査（DRC・ERC）では見えない、書き出しの段階の間違い（塗っていないベタ、抜けた層、
回路図とずれた PCB、消えたビアの穴）を捕まえます。AI エージェントが基板を設計するときの安全柵として、
エージェント向けの規則（`AGENTS.md`）と Claude Code のスキルも同梱しています。

## License

MIT
