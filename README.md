# pcb-guardrails

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24185580.svg)](https://doi.org/10.5281/zenodo.24185580)

Verification guardrails for AI-assisted PCB design — independent checks of what the fab actually receives
(Gerber + drill vs. schematic netlist), each born from a real failure.

An AI agent (or a tired human) can produce a board whose design database looks perfect while the files sent to
the fab are wrong: a pour that was never filled, a layer that was not exported, a PCB that drifted from the
schematic, a via whose drill went missing. CAD-internal checks (DRC, ERC) look at the database. This tool looks
at **the deliverable**, rebuilds it from scratch, and compares it with the schematic.

A via whose drill went missing from the export. The board file still has the via, so the CAD tool sees the net
as connected:

```
$ pcb-guardrails netcheck demo/novia/gerbers --netlist demo/novia/board.net.xml --pcb demo/novia/board.kicad_pcb
pads 5, schematic nets with pads 2, copper layers 2, drill files 1
OPEN 1 / SHORT 0 / NO-COPPER 0 / BREAKOUT 0 / VIA-NO-HOLE 1 / pads of parts not in the schematic 0 / copper islands without pads 0
  OPEN  B: R1.1 | R2.2
  VIA-NO-HOLE via (0.3 mm drill) at [15.0, -14.0] in the board data has no plated hole in the drill files
FAIL
```

Inner planes plotted before the zones were filled. The Gerbers are valid files; they just have no ground plane:

```
$ pcb-guardrails fill demo/plane --min In1.Cu=0.8 --min In2.Cu=0.8
board area 600.0 mm2
  F.Cu    copper 0.2%
  In1.Cu  copper 0.0%  FAIL (need >= 80%)
  In2.Cu  copper 0.0%  FAIL (need >= 80%)
  B.Cu    copper 0.2%
FAIL
```

Both are real output on synthetic boards (no real design involved). Reproduce them from a clone with
`python examples/demo.py demo`; the same export without the faults (`demo/good`) passes.

## What it checks

| Command | Catches | How |
|---|---|---|
| `netcheck` | **OPEN** (a net split into pieces), **SHORT** (two nets on one piece of copper), **NO-COPPER** (a pad with no copper in the files), **BREAKOUT** (a plated hole only partly covered by copper on a layer: a shifted or mis-registered layer), **VIA-NO-HOLE** (a via of the board with no drill hit, e.g. a lost stitching via), no-connect pins touching a net | Parses the Gerber copper layers and Excellon drills into geometry, joins copper through plated holes, and compares the resulting pieces with the **schematic** netlist. The CAD tool's connectivity engine is never used. Pad *positions* come from the `.kicad_pcb`; its nets are ignored. |
| `fill` | Pours / planes missing from the Gerbers (e.g. zones never filled before plotting — this exports cleanly, with no error) | Copper area per layer relative to the board area; fail below `--min LAYER=RATIO`. |
| `dfm` | Values below your fab's minimums; **copper outside the board** | Measures trace widths, gaps **between conductors** and **within one conductor**, copper-to-edge, drill sizes, hole-to-hole, hole-to-edge, via and PTH annular rings, silk line widths. Judges them only against a rules file **you** fill in (`--rules`). Copper entirely off the board, or crossing the board edge, fails with or without rules (see [Board edge](#board-edge)). |
| `freeze` | A frozen file rewritten or changed by a later tool run (a "save" that dropped nets, a regenerated board, a re-export into the upload folder) | `freeze record` stores a SHA-256 and a fingerprint of what each file means; `freeze check` reports SAME / **EQUIVALENT** (bytes differ, geometry or content identical: new UUIDs, dates, aperture numbers, item order) / **CHANGED** / MISSING / NEW. |
| `wrongnet` | A track or via touching a pad of **another net** in the `.kicad_pcb` (a track end on the neighbouring pad of a mirrored footprint) | Real pad shapes (rect, roundrect, oval, trapezoid, custom; absolute angles) against track and via copper, per copper layer; net-tie footprints respected. Database-side; `netcheck` sees the same short in the Gerbers. |
| `libcheck` | A symbol or footprint that does not exist on disk, a disabled library, a symbol pin with no footprint pad | Resolves `Library:Name` through the project and global `sym-lib-table` / `fp-lib-table` (nested tables, `${VARS}`), reads the `.kicad_sym` / `.kicad_mod`, compares pin and pad **numbers**. On a parts list before generation, or on a schematic. |
| `dnp` | A DNP decision that did not reach the schematic or board; alternatives fitted together | Compares the DNP flag per part across `parts.csv`, `.kicad_sch` and `.kicad_pcb`; `--exclusive A,B` groups. Unclear values in the parts list (`DNP?`, `see D1`) stop the check. |
| `layers` | Wrong or missing files in the upload folder | Shows how each file was classified (X2 `FileFunction`, falling back to Protel/KiCad extensions). |

Every check is proven by tests that inject one fault each into a small synthetic board — a deleted trace, an
extra trace, a removed via drill, a missing layer file, a shifted layer, a schematic that changed after layout,
a no-connect pin tied to a net, a clear-polarity cut, an unsupported Gerber feature — and assert it is reported.

## Exit codes (stable, for agents and CI)

| Code | Meaning |
|---|---|
| 0 | all checks passed; for `fill` / `dfm` run without limits, *measured only*: the output says `MEASURED ONLY — not judged` and the JSON has `"judged": false` (`dfm` still fails on copper outside the board) |
| 1 | a check failed |
| 2 | **the input could not be checked** (unsupported Gerber feature, missing file, bad arguments). Never read this as "probably fine". |

Unknown Gerber constructs stop the check with exit 2 instead of being skipped: a checker that ignores what it
does not understand will report a broken board as clean.

## Install

From GitHub (not on PyPI yet):

```bash
pip install "git+https://github.com/Y-Watanabe11/pcb-guardrails@v0.2.1"
```

From a clone, for development:

```bash
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

### Other CAD tools: IPC-D-356

Any CAD tool that exports an IPC-D-356 netlist (the test netlist fabs use for electrical test) can be checked:

```bash
pcb-guardrails netcheck fab/gerbers --ipc356 board.ipc    # instead of --netlist and --pcb
```

- The nets are **the board's**, as exported with the layout, not the schematic's. A board that drifted away from
  its schematic is not caught in this mode; the KiCad mode compares with the schematic.
- The IPC file's origin often differs from the Gerbers' (KiCad writes it relative to the drill / place origin).
  The offset is found by putting the IPC file's plated holes onto the drill hits: at least 90 % must land, and no
  other offset may come within 10 points. Otherwise it is exit 2; give the offset with `--ipc356-offset DX,DY`
  (mm). The offset used is printed.
- Read: records 317 (through-hole pad, via), 327 (surface pad, top `A01` or bottom), 367 (non-plated hole,
  ignored), `N/C` pads (must sit alone on their copper), `UNITS CUST 0`. Anything else (other units, blind /
  buried via records 307 / 027, conductor records) is exit 2.
- The format comes from two exporters and their output (KiCad's `export_d356.cpp` and KiCad 10.0.4 files, the
  EAGLE `ipc-d-356.ulp`), not from the IPC standard, which is sold by IPC. The fields after column 32 are read by
  their letters because the exporters place them in different columns; the drill size is not used because they
  write it in different units.
- Net names are limited to 14 characters and references to 6. Nets whose names fill the field are reported as
  possibly cut short (two different nets cut to one name are read as one net); pads whose cut reference and pin
  repeat with different nets are kept apart as `REF#n`.
- Pass `--pcb` as well for a KiCad board to honour net ties; without it a net-tie footprint shows as SHORT.
- The IPC file says nothing about symbol pins. A footprint pad with no symbol pin (a MOSFET's drain tab) carries an
  unconnected net of its own and shows as SHORT with the copper it joins; the KiCad mode reads the symbol's pins and
  makes it a warning. A pad number one part repeats with a different net on each copy (KiCad does this for an
  unconnected thermal pad and its vias), joined by the footprint's copper, is a warning, not a SHORT.

### Before and after generating a schematic / board

```bash
# parts.csv: ref,symbol,footprint,dnp   (see examples/parts.example.csv)
pcb-guardrails libcheck --parts parts.csv --kicad-share <KiCad's folder with symbols/ and footprints/> \
                        --global-tables <KiCad's user config folder>          # before generating
pcb-guardrails libcheck --parts parts.csv --sch board.kicad_sch --kicad-share ... --global-tables ...
pcb-guardrails dnp      --parts parts.csv --sch board.kicad_sch --pcb board.kicad_pcb --exclusive R1,R2
pcb-guardrails wrongnet board.kicad_pcb
```

KiCad sets its library variables (`KICAD10_SYMBOL_DIR` ...) at run time and does not store them, so `libcheck`
needs `--kicad-share` (it maps `KICADnn_SYMBOL_DIR` to `<dir>/symbols`, `..._FOOTPRINT_DIR` to `<dir>/footprints`)
or `--var NAME=PATH`. On macOS that folder is `/Applications/KiCad/KiCad.app/Contents/SharedSupport` and the user
config folder `~/Library/Preferences/kicad/<version>`. An unresolved variable, or a library that is not in the
project table when no global tables were given, is exit 2, never "missing".

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

Every non-null rule must be judged. A rule that cannot be (an unknown rule name, an edge rule without an outline
file, a drill rule without a drill file, a ring rule without `--pcb`, a silk rule without silkscreen files), or a file
whose rules are all null, is reported as NOT JUDGED and `dfm` exits 2 unless a violation already made it FAIL; set a
rule to null if it does not apply. Copper gaps are searched up to the largest spacing rule automatically, so a gap
just below your minimum cannot fall outside the search.

### Board edge

The board edge is the **centre line** of the outline strokes in the Profile / Edge_Cuts file ("the profile is the
center line of the stroked line": K. Tavernier, Ucamco, *The Gerber Guide, Chapter 3: The PCB Profile*, 2015).
Closed loops inside the outline are cut-outs. Outline arcs are followed to within 1 µm. End points closer than half
the stroke width are joined, since their drawn ends overlap (CAD tools round an arc's end and the next line's start
separately; 1 µm for an outline drawn without width). An outline that still does not close is exit 2.

`dfm` then sorts copper beyond the edge, needing no fab rule:

| Kind | Meaning | Result |
|---|---|---|
| off-board | a piece of copper with nothing on the board (a part placed beside the board, copper in a cut-out) | FAIL |
| over-edge | copper crossing the edge: the fab cuts through it | FAIL |
| castellated | copper beyond the edge around a plated hole the edge cuts | NOTE: castellation is a separate fab option, make sure it is ordered |

Copper reaching up to 5 µm past the edge counts as *at* the edge: pours drawn to the edge with zero clearance end
on the centre line. Whether copper may touch the edge at all is your fab's `min_copper_to_edge_mm`, which is measured
to the centre line, ignoring castellated pads (and `min_hole_to_edge_mm` ignores castellated holes).

## For AI agents

`netcheck --format agent` prints a report written for a language model: the verdict and exit code first, what was
and was not checked, and for each problem the evidence, likely causes (root causes such as undrilled vias before the
OPENs they cause) and the next step, including what not to do. Names of nets and parts come from the design files
and are printed only inside `«...»`, on one line, so a net named like an instruction cannot pass for part of the
report. The JSON output (`--json`) carries the same facts for programs.

- [`AGENTS.md`](AGENTS.md) — rules for an agent that designs or edits PCBs (pin numbers, sources of truth, what
  never to run on production files, when to stop and ask).
- [`.claude/skills/pcb-guardrails/SKILL.md`](.claude/skills/pcb-guardrails/SKILL.md) — a Claude Code skill that
  runs the checks before fab files are handed over and reports the result without "fixing" the Gerbers.
- [`docs/incidents.md`](docs/incidents.md) — the failures behind each check.

## Limitations

- `netcheck` against the schematic needs KiCad (XML netlist and `.kicad_pcb`); other CAD tools are checked against
  the board's nets through IPC-D-356. `fill`, `dfm` and `freeze` read Gerber / drill files from any tool.
- Does not check what a pin *means* (`libcheck` compares pin and pad numbers only), footprint geometry against a datasheet, impedance,
  current capacity, creepage/clearance for high voltage, or anything electrical beyond connectivity.
- Supported Gerber subset: RS-274X with X2 attributes, `%FSLA%`, mm or inch, circular/rect/obround/polygon
  apertures, aperture macros (primitives 1, 4, 5, 20, 21), regions, multi-quadrant arcs, dark and clear
  polarity. Step-and-repeat, image transforms (`%LM/LR/LS%`) and single-quadrant arcs stop with exit 2.
- Drill files: Excellon with decimal coordinates, or integer coordinates when the digit format is stated (KiCad's
  `; FORMAT={3:3/...}` comment or a `METRIC,TZ,000.000` header; leading / trailing zero suppression or kept
  zeros); drill files written as Gerber X2 (`TF.FileFunction,Plated|NonPlated,...`). Plating comes from X2
  `TA.AperFunction` or the file function; a mixed file without either (KiCad's "minimal header") stops with exit 2,
  because plated and non-plated holes cannot be told apart. Plated holes below 0.01 mm join nothing and are
  reported (KiCad writes a 0.00001 mm via drill as `T1C0.000`).
- `netcheck`: footprints with `net_tie_pad_groups` (bridged solder jumpers, star-ground ties) join their nets on
  purpose and are listed as NET-TIE, not SHORT. Pins KiCad's netlist places on one shared `unconnected-(...)` net may
  share copper; a pin with no net must sit alone. A footprint pad the symbol has no pin for (read from the netlist's
  `<libparts>`, e.g. a MOSFET drain tab) is a warning naming the net its copper joins, not a FAIL. Copper a footprint draws between its own pads without declaring
  a net tie (a PCB trace fuse, for example) is a SHORT: the copper is really there.
- `libcheck`: KiCad-format libraries only (`.kicad_sym` files, `.pretty` folders); symbol libraries stored as a folder
  and other library types stop with exit 2. Units of one symbol share one footprint.
- `wrongnet`: trapezoid pads are taken as their bounding rectangle and chamfered corners as square (both may
  over-report, never miss). A drill offset moves the copper, not the hole (checked against KiCad's Gerber output).
  Zones are not checked (use `netcheck` on the Gerbers).
- `freeze`: EQUIVALENT ignores UUID values, so a changed symbol ↔ footprint link (`path`) alone is not reported.
  A Gerber change hidden under other copper (a trace end moved inside a pad) is EQUIVALENT, because the copper
  is identical. Files the fingerprint cannot parse fall back to bytes only and are never EQUIVALENT.

## Tested on public boards

The 20 demo projects shipped in KiCad's source tree (`demos/`, from a 4-layer microwave board to a 1500-part VME
board) were exported with KiCad 10.0.4 on copies and checked, then exported again in every Gerber / drill option
KiCad offers. See [`docs/public-boards.md`](docs/public-boards.md) for the method and every finding; in short, each
remaining FAIL is either confirmed by KiCad's own DRC / schematic parity or is a real difference in the fab files.

## Roadmap

- v0.2 (done, see [`CHANGELOG.md`](CHANGELOG.md)): design-freeze fingerprints, library preflight with pin/pad
  numbers, tracks on wrong-net pads, DNP consistency; BREAKOUT and VIA-NO-HOLE; reports for AI agents; IPC-D-356
  for other CAD tools; the board edge as the outline's centre line; nothing unjudged is ever a pass; validated on
  KiCad's public demo boards.
- Next: IPC-D-356 files from other CAD tools (only KiCad's have been tried); `--format agent` for every command;
  speed on large boards (minutes for 5000+ pads).
- v0.3: assembly — JLCPCB BOM/CPL helpers and per-part rotation offsets, 1:1 footprint print templates,
  headless LTspice batch runs.

Related: [FabGate](https://pypi.org/project/fabgate/) wraps KiCad's DRC and schematic parity in a scored gate and
checks that a fab package has its files; pcb-guardrails checks what is in those files. See
[`docs/comparison.md`](docs/comparison.md) for a fault-by-fault comparison.

## 日本語の概要

基板メーカーに渡すファイル（ガーバーとドリル）だけから銅のつながりを組み直し、回路図のネットリストと照合する
ツールです。CAD の中の検査（DRC・ERC）では見えない、書き出しの段階の間違い（塗っていないベタ、抜けた層、
回路図とずれた PCB、消えたビアの穴）を捕まえます。AI エージェントが基板を設計するときの安全柵として、
エージェント向けの規則（`AGENTS.md`）と Claude Code のスキルも同梱しています。

- 終了コードは 0 = 合格、1 = 不合格、2 = 確認できなかった（合格ではない）。判定していないものを合格とは表示しません。
- 回路図との照合は KiCad（XML ネットリストと `.kicad_pcb`）。他の CAD は IPC-D-356 ネットリスト（基板側のネット）で照合できます。
- ほかに、ベタの抜け（`fill`）、ファブの規則値との比較と基板外の銅（`dfm`）、凍結したファイルの書き換え（`freeze`）、
  別ネットのパッドに触れる配線（`wrongnet`）、ライブラリの実在とピン番号（`libcheck`）、DNP の食い違い（`dnp`）。
- `netcheck --format agent` は AI エージェントが読んで動けるように、判定・証拠・原因の候補・次の手順を順に出力します。
- インストール: `pip install "git+https://github.com/Y-Watanabe11/pcb-guardrails@v0.2.1"`（PyPI には未登録）。

## Citing

If pcb-guardrails helps your work, cite it with **Cite this repository** on the GitHub page (APA or BibTeX, from
`CITATION.cff`). Each release is archived on Zenodo with its own DOI;
[10.5281/zenodo.24185580](https://doi.org/10.5281/zenodo.24185580) always points to the latest release.

## License

MIT
