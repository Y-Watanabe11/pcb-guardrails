# Validation on public KiCad boards

Date: 2026-11-12. KiCad 10.0.4 (macOS). Boards: the 20 demo projects in KiCad's source tree
(`demos/`, gitlab.com/kicad/code/kicad, commit `595568f`, 2026-11-11). Nothing from them is stored in this
repository; the run used copies in a scratch folder.

## Method

1. Copy each demo project; export Gerbers (`--check-zones`), Excellon drill and the KiCad XML netlist with
   `kicad-cli`.
2. Run `netcheck`, `fill`, `dfm`, `wrongnet`, `dnp` (schematic vs board) and `libcheck` (schematic, KiCad's stock
   libraries plus the project tables).
3. For three boards (2, 2 and 6 copper layers), export again with every Gerber / drill option KiCad offers and run
   `netcheck` on each folder.
4. Every FAIL and every exit 2 was traced to its cause. Where KiCad can judge the same thing, its DRC with schematic
   parity (`kicad-cli pcb drc --schematic-parity --refill-zones`) was run on the copy and compared.
5. The schematic reader was compared with KiCad's own netlist (references and library ids of every part).

## Tool defects found and fixed

| Found on | Defect | Fix (with a test that fails on the old code) |
|---|---|---|
| multichannel, vme-wren | KiCad's `RoundRect` aperture macro gets parameters like `0.000010`; they were substituted as `1e-05`, which the expression check rejected: **exit 2 on any board with rounded-rectangle pads** | parameters substituted in fixed-point notation |
| vme-wren, jetson | schematic reader took every `instances` path of a symbol, including paths left over from removed sheets: wrong or duplicate references (350 parts on one board) | hierarchy walked from the root; a symbol takes the reference recorded for its own sheet path (KiCad 6 `symbol_instances` too). Now identical to KiCad's netlist on all 18 boards with schematics |
| complex_hierarchy, royalblue | `wrongnet` stopped on pads with a drill offset | offset applied to the copper, not the hole; checked against KiCad's Gerber copper for 54 offset pads at 0/90/180/270 degrees |
| jetson | `wrongnet` stopped on a custom pad with an arc primitive | `gr_arc` (start / mid / end) supported |
| royalblue | a footprint file with an extra bracket crashed `libcheck` | every S-expression read goes through one reader; unreadable files are exit 2 |
| openair, tiny_tapeout, royalblue | bridged solder jumpers (`net_tie_pad_groups`) reported as SHORT; `wrongnet` mis-read groups written `"1, 2"` | net ties honoured in `netcheck` (listed as NET-TIE) and parsed with spaces |
| vme-wren | two no-connect pins KiCad puts on **one** `unconnected-(...)` net (a regulator's PHASE pins) reported as SHORT | pins sharing one unconnected net may share copper; a pin with no net still must sit alone |
| openair | a footprint pad with no symbol pin (a MOSFET's drain tab) treated as a no-connect pin | pin lists read from the netlist's `<libparts>`; such a pad is a warning naming the net its copper joins (a pad alone is not reported) |
| tiny_tapeout | `dnp` ignored board-only footprints that the schematic also has | board-only footprints count when the design names them |
| fault matrix (complex_hierarchy, cm5_minima) | a whole layer shifted 0.5 mm passed: the shift keeps the layer's own connectivity and large through-hole pads still touch their holes | `netcheck` BREAKOUT: a plated hole only partly covered by copper on a layer (castellated holes on the outline excepted) |
| fault matrix (cm5_minima) | a missing drill hit of one of 279 GND stitching vias passed: connectivity unchanged | `netcheck` VIA-NO-HOLE: every via of the `.kicad_pcb` must have a plated hole in the drill files |

BREAKOUT and VIA-NO-HOLE report nothing on any of the 20 public boards or the two private boards.

## Board edge

`dfm` used to fail any layer with more than 0.01 mm² of copper outside the outline (whenever `--rules` was given).
That failed 7 of the 20 boards, and it measured from the wrong line: the outline polygon was the **inner side** of
the Edge.Cuts stroke, not its centre line. The copper "outside" was:

| Board | Copper beyond the inner side of the stroke | Stroke width | What it is |
|---|---|---|---|
| interf_u, sonde xilinx, stickhub, video, nfc_antenna | 0.025–0.09 mm, pours along the edge | 0.05–0.2 mm | pours ending on the centre line (within 0.5 µm): at the edge, not past it |
| royalblue54L_feather | 28 pads × 8 layers, 0.9 mm | 0.1 mm | castellated header: 1.0 mm plated holes centred on the edge, `(castellated_pads yes)` in the board setup |
| multichannel (unrouted) | up to 72 mm | 0.05 mm | parts not yet placed on the board |

Now the edge is the centre line (see the README, *Board edge*), and copper beyond it is sorted: off the board
entirely, crossing the edge, or around a plated hole the edge cuts (castellated, noted only). The first two fail
with or without rules; touching the edge is left to the fab's `min_copper_to_edge_mm`.

Result on the 20 boards: `dfm` fails only multichannel (unrouted), with copper entirely off the board on F.Cu and
B.Cu; royalblue gets the castellation note (28 holes); the pour boards measure 0.0005 mm copper-to-edge. Building the
outline from centre lines showed that CAD tools leave gaps where an arc meets a line (0.04 µm on cm5_minima, 3.5 µm
on nfc_antenna); end points closer than half the stroke width are now joined. `fill` and `netcheck` exit codes are
unchanged on all boards.

## Drill / Gerber formats (step 3)

| Export option | Before | After |
|---|---|---|
| no X2, no aperture macros, 5-digit precision | pass | pass |
| drill in inch, routed slots, separate PTH/NPTH files | pass | pass |
| integer Excellon: suppress leading / trailing zeros, keep zeros | exit 2 | pass (digit format from KiCad's `FORMAT={3:3/...}` comment or a `METRIC,TZ,000.000` header; holes identical to the decimal export within the format's 1 um resolution) |
| drill files written as Gerber X2 | exit 2 (no drill file found) | pass (`TF.FileFunction,Plated|NonPlated,...,Drill|Rout|Mixed`) |
| Excellon "minimal header" | exit 2 | exit 2, by design: a mixed file without plating attributes cannot say which holes are plated |

## Board findings (tool correct)

| Board | Finding | Confirmed by |
|---|---|---|
| royalblue54L_feather | `netcheck` OPEN on 30+ nets: all 183 vias are `blind` with `(drill 0.00001)`, which KiCad exports as `T1C0.000`. Nothing would be drilled, so the vias join no layers in the fab files. KiCad's DRC flags the drill size ("hole size out of range", blocking) but reports 0 unconnected items: it sees the vias as connections. | drill file content; KiCad DRC hole-size violation; `netcheck` now prints the count of plated holes below 0.01 mm |
| openair-max | GND OPEN at I2C3 pad 6: the schematic puts the pin on GND, the board leaves the pad unconnected | KiCad parity: `net_conflict` |
| openair-max | SHORT across F2: the "PCB fuse" footprint draws a copper line from pad to pad without declaring a net tie | footprint content (a trace fuse; intended, but undeclared) |
| openair-max | Battery1, Solarpower1: DNP in the schematic, fitted on the board | KiCad parity: `footprint_symbol_mismatch` (DNP) |
| tiny_tapeout | OPEN `Net-(C3-Pad1)` at J15; tracks of `out7` and `GND` on the placeholder pads J16 / J18 | KiCad DRC: 1 unconnected item, 3 `shorting_items` |
| vme-wren | Cage3, J1, J13: fitted in the schematic, DNP on the board | file content |
| several | `libcheck`: footprints renamed or removed in KiCad 10's libraries (DSUB, tantalum "HandSolder", a SOIC-8 variant); project libraries missing from the project tables | library folders on disk |
| sonde xilinx | `libcheck` warning: the units of U1 / U2 use four different symbol ids (a schematic converted from KiCad 5) | file content |
| multichannel (unrouted) | OPEN on the unrouted copy, PASS on the routed one | expected |

Not checked: `constraints` (written by a newer KiCad than 10.0.4; the board did not load) and `microwave` (no
schematic). The largest boards took 92 s (jetson, 10 layers, 4771 pads) and 106 s (vme-wren, 12 layers, 6875 pads)
for `netcheck`.

## Remaining conservative simplifications

- `wrongnet` takes chamfered corners and trapezoids as full rectangles: it may over-report contact on those pads,
  never miss it (3 of 54 offset pads on royalblue differ from the Gerber by exactly their chamfered corners).

## IPC-D-356 (`netcheck --ipc356`)

Each board was also exported as IPC-D-356 with `kicad-cli pcb export ipcd356` (KiCad 10.0.4) and checked with
`--ipc356` in place of the KiCad netlist, on the same Gerbers and drill files.

| | |
|---|---|
| origin | KiCad writes the IPC file relative to the drill / place origin; the offset was found on all 18 boards with plated holes, with every plated hole of the IPC file on a drill hit (2 to 4896 holes), e.g. (83.500, -138.001) mm on complex_hierarchy |
| exit codes | identical to the KiCad mode on all 18 boards that have both (royalblue 54 OPEN and 183 undrillable vias, tiny_tapeout 1 OPEN 2 SHORT, unrouted multichannel 61 OPEN, the rest PASS) |
| openair-max | KiCad mode: GND OPEN (schematic and board disagree) and the F2 trace-fuse SHORT. IPC mode: no OPEN (the board's own nets are used, so the disagreement is invisible) and two SHORTs: F2, and the drain tabs of Q1 / Q2, whose pad 9 has no symbol pin and gets an unconnected net of its own in the IPC file |
| microwave | exit 2: no plated holes to align on (an all-SMD board); `--ipc356-offset` is needed |
| time | the same as the KiCad mode (jetson 308 s, vme-wren 489 s on a loaded machine) |

Defects found on the way and fixed, each with a test:

| Found on | Defect | Fix |
|---|---|---|
| ecc83-pp_v2, multichannel | alignment failed (25 of 34 holes): a slot's drill-file position is where the router starts, not its centre | holes aligned on their centres |
| jetson | SHORT between 7 copies of U30 pad 57: KiCad gives each copy of an unconnected, repeated pad number (a thermal pad and its vias) its own net, and the footprint's copper joins them | a SHORT made only of copies of one pad name is a warning |

The name limits of the format are visible on these boards: up to 1345 net names per board fill the 14-character
field (KiCad 10.0.4 keeps the last 14 characters), and references are cut to 6 (21 pad names on kit-dev-coldfire
repeat with different nets and are kept apart as `REF#n`). Two separately routed nets cut to one name would show
as an extra OPEN; none appeared, since every OPEN / SHORT count matches the KiCad mode except where explained. A short
between two such nets would be invisible in this mode.
