# Changelog

Exit codes are part of the interface (0 pass, 1 fail, 2 not checked); every change to what they mean is listed
under **Changed**.

## [Unreleased]

- README: the example output at the top is real output on synthetic boards (it was illustrative), reproducible
  with `python examples/demo.py demo`; a test fails if the README and the tool's output differ.

## [0.2.1] — 2026-11-13

No code changes.

- Releases are archived on Zenodo, which gives each release a DOI; the author and ORCID iD come from
  `CITATION.cff`.
- README: how to cite.

## [0.2.0] — 2026-11-12

### Added

- `freeze record` / `freeze check`: SHA-256 and a semantic fingerprint of each frozen file; reports SAME,
  EQUIVALENT (rewritten, same content), CHANGED, MISSING, NEW.
- `wrongnet`: tracks and vias touching a pad of another net in the `.kicad_pcb`, with real pad shapes.
- `libcheck`: symbols and footprints must exist in the libraries on disk; symbol pins without a footprint pad.
- `dnp`: DNP decisions compared across `parts.csv`, schematic and board; `--exclusive` groups.
- `netcheck`: BREAKOUT (a plated hole only partly covered by copper on a layer), VIA-NO-HOLE (a via of the board
  with no drill hit), net ties, pins sharing one unconnected net, pads without a symbol pin (warning).
- `netcheck --format agent`: a report written for an AI agent, with verdict, evidence, likely causes, next step
  and rules; names from the design files are fenced.
- `netcheck --ipc356`: boards from any CAD tool, checked against their IPC-D-356 netlist (the board's nets). The
  origin is found from the drill files or given with `--ipc356-offset`.
- `dfm`: copper outside the board (off the board, or crossing the edge) is judged without a rules file;
  castellated holes are recognised and noted.
- Drill files: integer Excellon (digit format from the header), drill files written as Gerber X2.
- Validation on KiCad's 20 public demo boards (`docs/public-boards.md`) and a comparison with FabGate
  (`docs/comparison.md`).

### Changed

- `fill` without `--min` and `dfm` without `--rules` print `MEASURED ONLY — not judged`, not `PASS`; the JSON has
  `"judged": false`. Exit code 0.
- `fill --min` naming a layer that is not in the files: exit 2 (was ignored).
- `dfm`: a requested rule that cannot be judged (unknown name, missing file or `--pcb`), or a rules file with only
  null rules: exit 2 unless a rule already failed (was a pass). No copper layers: exit 2.
- `dfm` can exit 1 without `--rules` (copper outside the board). The old check (any layer with more than 0.01 mm²
  outside the outline, only with `--rules`) is gone.
- The board outline is the **centre line** of the profile strokes (was the inner side of the stroke). Board areas
  in `fill` and copper-to-edge / hole-to-edge in `dfm` grow by about half the stroke width; cut-outs are any closed
  loop inside the outline (was: larger than 1 mm²).
- `dfm`: the copper gap search reaches past the largest spacing rule.
- `netcheck`: `--netlist` and `--pcb` are needed unless `--ipc356` is given.

### Fixed

- Aperture macro parameters written in exponent form (`1e-05`) were rejected: exit 2 on boards with rounded
  rectangle pads.
- `netcheck`: bridged solder jumpers and other net-tie footprints were reported as SHORT; no-connect pins that
  KiCad places on one shared unconnected net were reported as SHORT.
- Windows: Gerber, drill and rules files were read in the locale's encoding, and a report written to a pipe in a
  code page such as cp932 crashed on « ». Files are read as UTF-8, and output is written as UTF-8 when the
  output's encoding is not. CI runs on Linux, macOS and Windows.

### Packaging

- License metadata as an SPDX expression (`MIT`); the source distribution includes the tests, docs, examples,
  `AGENTS.md` and this changelog.
- Author metadata (`Yuki Watanabe`) in `pyproject.toml` and `LICENSE`; `CITATION.cff` for GitHub's "Cite this
  repository".

## [0.1.0] — 2026-11-11

- `netcheck` (Gerber + drill connectivity against the KiCad schematic netlist), `fill`, `dfm`, `layers`.
- Rules for AI agents (`AGENTS.md`) and a Claude Code skill.
