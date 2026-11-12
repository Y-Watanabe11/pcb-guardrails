"""Command line: ``pcb-guardrails <check> ...``

Exit codes (stable, for agents and CI):
    0  all checks passed
    1  a check failed (OPEN / SHORT / missing copper / rule violation / pour missing / frozen file changed / wrong-net pad)
    2  the input could not be checked (unsupported Gerber feature, missing file, bad arguments)
A result of 2 must never be read as "probably fine".
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .dfm import SPACING_KEYS, active_rules, apply_rules, run_dfm, unjudgeable
from .dnp import run_dnp
from .fill import run_fill
from . import freeze as _freeze
from .gerber import UnsupportedInput, discover
from .kicad import copper_name, load_footprints, load_net_ties, load_netlist, load_pcb, load_schematic
from .libcheck import Item, Libraries, Vars, load_tables, run_libcheck
from .parts import load_parts, natural_key
from .agent_report import render_cannot, render_netcheck
from .netcheck import run_netcheck
from .wrongnet import run_wrongnet

PASS, FAIL, CANNOT = 0, 1, 2


def _write_json(path, obj):
    if path:
        Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def cmd_layers(a) -> int:
    f = discover(a.gerbers)
    n = len(f.copper)
    print(f"copper layers: {n}")
    for num, side, p in f.copper:
        print(f"  L{num} {side:<3} {copper_name(num, n):<7} {p.name}")
    print(f"outline: {f.profile.name if f.profile else '(none)'}")
    print(f"legend:  {', '.join(f'{k}={v.name}' for k, v in f.legend.items()) or '(none)'}")
    print(f"drills:  {', '.join(p.name for p in f.drills) or '(none)'}")
    if not f.copper or not f.drills:
        print("CANNOT CHECK: copper layers or drill file missing")
        return CANNOT
    return PASS


def cmd_netcheck(a) -> int:
    files = discover(a.gerbers)
    if not files.copper:
        raise UnsupportedInput("no copper layers found")
    if not files.drills and not a.allow_no_drill:
        raise UnsupportedInput("no drill file found (pass --allow-no-drill for single-layer boards without holes)")
    pads, vias = load_pcb(a.pcb)
    netlist = load_netlist(a.netlist)
    r = run_netcheck(files, pads, netlist, clearance_report=a.clearance_report, net_ties=load_net_ties(a.pcb), vias=vias)
    if a.format == "agent":
        print(render_netcheck(r, files), end="")
    else:
        _print_netcheck_text(r, files)
    if a.net_csv:
        with open(a.net_csv, "w", newline="", encoding="utf-8") as fo:
            w = csv.writer(fo)
            w.writerow(["net", "pads", "copper_pieces", "layers", "status", "pad_list"])
            for row in r.per_net:
                w.writerow([row["net"], row["pads"], row["copper_pieces"], " ".join(row["layers"]), row["status"], " ".join(row["pad_list"])])
    _write_json(a.json, r.to_dict())
    if a.format != "agent":
        print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def _print_netcheck_text(r, files) -> None:
    print(f"pads {r.pads}, schematic nets with pads {r.nets}, copper layers {len(files.copper)}, drill files {len(files.drills)}")
    print(f"OPEN {len(r.opens)} / SHORT {len(r.shorts)} / NO-COPPER {len(r.no_copper)} / BREAKOUT {len(r.breakouts)} / VIA-NO-HOLE {len(r.vias_without_hole)} / "
          f"pads of parts not in the schematic {len(r.not_in_netlist)} / copper islands without pads {len(r.islands)}")
    for o in r.opens:
        print(f"  OPEN  {o['net']}: " + " | ".join(", ".join(g) for g in o["groups"]))
    for s in r.shorts:
        print(f"  SHORT {' + '.join(s['nets'])}: {', '.join(s['pads'][:12])}")
    for w in r.pads_without_pin:
        print(f"  WARN {w['pad']} has no pin in its symbol but its copper joins {' + '.join(w['joins'])} "
              "(intended for a thermal tab; check it otherwise)")
    if r.tiny_holes:
        print(f"  WARN {len(r.tiny_holes)} plated holes in the drill files are smaller than 0.01 mm (e.g. at "
              f"{r.tiny_holes[0]['xy_kicad']}): they will not be drilled, so they connect no layers")
    for v in r.vias_without_hole[:10]:
        print(f"  VIA-NO-HOLE via ({v['drill_mm']} mm drill) at {v['xy_kicad']} in the .kicad_pcb has no plated hole in the drill files")
    if len(r.vias_without_hole) > 10:
        print(f"  VIA-NO-HOLE ... {len(r.vias_without_hole) - 10} more")
    for b in r.breakouts[:10]:
        print(f"  BREAKOUT {b['layer']}: plated hole {b['d_mm']} mm at {b['xy_kicad']} only {b['covered']:.0%} covered by copper")
    if len(r.breakouts) > 10:
        print(f"  BREAKOUT ... {len(r.breakouts) - 10} more")
    for t in r.ties:
        print(f"  NET-TIE {' + '.join(t['nets'])} joined on purpose by {', '.join(t['net_ties'])}")
    for p in r.no_copper:
        print(f"  NO-COPPER {p['pad']} ({p['net']}) at {p['xy_kicad']} on {p['layers']}")
    for l, c in r.clearance.items():
        m = c["min"]
        if m:
            print(f"  {l}: smallest gap between different copper {m['mm']} mm ({' / '.join(m['between'])}) at {m['at_kicad']}")


def cmd_dfm(a) -> int:
    files = discover(a.gerbers)
    if not files.copper:
        raise UnsupportedInput("no copper layers found")
    rules = json.loads(Path(a.rules).read_text()) if a.rules else None
    window = a.gap_window
    if rules:   # gaps are only searched up to the window: it must reach past the largest spacing rule
        window = max([window] + [float(rules[k]) + 0.05 for k in SPACING_KEYS if rules.get(k) is not None])
    pads, vias = load_pcb(a.pcb) if a.pcb else (None, None)
    m = run_dfm(files, pads, vias, gap_window=window)
    m["gap_window_mm"] = window
    for l, info in m["layers"].items():
        print(f"  {l}: trace min {info['min_trace_width_mm']} mm, gap between conductors {info['min_gap_between_conductors_mm']} mm "
              f"at {info['min_gap_between_conductors_at_kicad']}, within a conductor {info['min_gap_within_conductor_mm']} mm"
              + (f", copper-edge {info['min_copper_to_edge_mm']} mm" if "min_copper_to_edge_mm" in info else ""))
    h = m["holes"]
    if h.get("sizes"):
        print("  holes: " + ", ".join(f"{s['d_mm']} {s['type']} x{s['count']}" for s in h["sizes"]))
        print(f"  hole-hole min {h.get('min_hole_to_hole_mm')} mm, hole-edge min {h.get('min_hole_to_edge_mm')} mm")
    for v in m.get("vias") or []:
        print(f"  via {v['size_mm']}/{v['drill_mm']}: ring {v['annular_ring_mm']} mm x{v['count']}")
    for r in m.get("pads", {}).get("thru_hole_annular_rings", [])[:6]:
        print(f"  PTH ring {r['ring_mm']} mm: {', '.join(r['refs'][:8])}")
    for side, s in m["silk"].items():
        w = s["min_line_width_mm"]
        print(f"  silk {side}: min line {w} mm" if w is not None else f"  silk {side}: no stroked lines (flashes/regions only)")
    violations, not_judged = [], []
    if rules is not None:
        violations = apply_rules(m, rules)
        not_judged = unjudgeable(rules, files, bool(a.pcb))
        for v in violations:
            print(f"  VIOLATION {v}")
        for nj in not_judged:
            print(f"  NOT JUDGED {nj['rule']}: {nj['reason']}")
        if not active_rules(rules):
            not_judged.append({"rule": "(all)", "reason": "every rule in the file is null"})
            print("  NOT JUDGED: every rule in the file is null")
    m["violations"], m["not_judged"], m["judged"] = violations, not_judged, rules is not None
    _write_json(a.json, m)
    if rules is None:
        print("MEASURED ONLY — not judged (pass --rules <your fab's rules.json> to judge)")
        return PASS
    if violations:
        print("FAIL")
        return FAIL
    if not_judged:
        print("CANNOT CHECK: some requested rules could not be judged (listed above)")
        return CANNOT
    print("PASS")
    return PASS


def cmd_fill(a) -> int:
    files = discover(a.gerbers)
    mins = {}
    for spec in a.min or []:
        k, v = spec.split("=")
        mins[k] = float(v)
    r = run_fill(files, mins)
    unknown = sorted(set(mins) - set(r["layers"]))
    if unknown:   # a typo in a layer name must not turn a requested judgment into a silent pass
        raise UnsupportedInput(f"--min names layers that are not in the files: {', '.join(unknown)} "
                               f"(copper layers found: {', '.join(r['layers'])})")
    r["judged"] = bool(mins)
    print(f"board area {r['board_area_mm2']} mm2")
    for l, i in r["layers"].items():
        need = mins.get(l)
        flag = "" if need is None else ("  OK" if i["coverage"] >= need else f"  FAIL (need >= {need:.0%})")
        print(f"  {l:<7} copper {i['coverage']:.1%}{flag}")
    _write_json(a.json, r)
    if not mins:
        print("MEASURED ONLY — not judged (pass --min LAYER=RATIO, e.g. --min In1.Cu=0.8, to judge)")
        return PASS
    print("PASS" if r["ok"] else "FAIL")
    return PASS if r["ok"] else FAIL


def cmd_wrongnet(a) -> int:
    r = run_wrongnet(a.pcb)
    print(f"pads {r.pads}, tracks {r.tracks}, vias {r.vias}")
    for h in r.hits:
        print(f"  WRONG-NET {h['item']} of {h['item_net']} touches {h['pad']} ({h['pad_net']}) "
              f"{'at its end' if h['where'] == 'end' else 'along its length'} at {h['at_kicad']} on {', '.join(h['layers'])}")
    print(f"WRONG-NET {len(r.hits)}")
    _write_json(a.json, r.to_dict())
    print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def _vars(specs):
    out = {}
    for spec in specs or []:
        if "=" not in spec:
            raise ValueError(f"--var {spec!r}: expected NAME=VALUE")
        k, v = spec.split("=", 1)
        out[k] = v
    return out


def cmd_libcheck(a) -> int:
    if not a.parts and not a.sch:
        raise ValueError("pass --parts and/or --sch")
    project = Path(a.project_dir or Path(a.sch or a.parts).parent)
    v = Vars(_vars(a.var), a.kicad_share, project.resolve())
    libs = Libraries(load_tables("symbol", project, a.sym_lib_table or [], a.global_tables, v),
                     load_tables("footprint", project, a.fp_lib_table or [], a.global_tables, v))
    items, cross, cross_warn = [], [], []
    parts = load_parts(a.parts) if a.parts else []
    for pt in parts:
        items.append(Item(pt.ref, pt.symbol, pt.footprint, True, f"{Path(a.parts).name}:{pt.line}"))
    if a.sch:
        by_ref = {}
        for s in load_schematic(a.sch):
            if s.ref in by_ref and by_ref[s.ref].lib_id != s.lib_id:   # seen in schematics converted from KiCad 5
                cross_warn.append({"ref": s.ref, "problem": f"units use different symbols: {by_ref[s.ref].lib_id} / {s.lib_id}",
                                   "source": s.file})
            by_ref.setdefault(s.ref, s)
        if parts:
            pmap = {pt.ref: pt for pt in parts}
            for ref in sorted(set(pmap) - set(by_ref)):
                cross.append({"ref": ref, "problem": "in the parts list but not in the schematic", "source": "schematic"})
            for ref in sorted(set(by_ref) - set(pmap)):
                cross.append({"ref": ref, "problem": "in the schematic but not in the parts list", "source": by_ref[ref].file})
            for ref in sorted(set(pmap) & set(by_ref)):
                pt, s = pmap[ref], by_ref[ref]
                if pt.symbol and pt.symbol != s.lib_id:
                    cross.append({"ref": ref, "problem": f"schematic uses {s.lib_id}, parts list says {pt.symbol}", "source": s.file})
                if pt.footprint and pt.footprint != s.footprint:
                    cross.append({"ref": ref, "problem": f"schematic footprint {s.footprint or '(none)'}, parts list says "
                                  f"{pt.footprint}", "source": s.file})
        else:
            items += [Item(s.ref, s.lib_id, s.footprint, s.on_board, s.file, False) for s in by_ref.values()]
    r = run_libcheck(items, libs)
    r.fails = cross + r.fails
    r.warns = cross_warn + r.warns
    print(f"parts {r.items}: symbols found {r.symbols_ok}, footprints found {r.footprints_ok}")
    for f in r.fails:
        print(f"  FAIL {f['ref']}: {f['problem']}")
    for w in r.warns:
        print(f"  WARN {w['ref']}: {w['problem']}")
    _write_json(a.json, r.to_dict())
    print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def cmd_dnp(a) -> int:
    groups = [[r.strip() for r in g.split(",") if r.strip()] for g in a.exclusive or []]
    if any(len(g) < 2 for g in groups):
        raise ValueError("--exclusive needs at least two references, e.g. --exclusive R16,R17")
    r = run_dnp(load_parts(a.parts) if a.parts else None, load_schematic(a.sch) if a.sch else None,
                load_footprints(a.pcb) if a.pcb else None, groups)
    for name, m in r.sources.items():
        dnp = sorted((ref for ref, d in m.items() if d), key=natural_key)
        print(f"  {name:<9} parts {len(m)}, DNP {len(dnp)}: {', '.join(dnp) or '-'}")
    for f in r.fails:
        print(f"  FAIL {f}")
    for w in r.warns:
        print(f"  WARN {w}")
    _write_json(a.json, r.to_dict())
    print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def cmd_freeze_record(a) -> int:
    out = Path(a.output)
    if out.exists() and not a.force:
        raise ValueError(f"{out} exists; it may be the record you need to check against. Pass --force to replace it")
    rec = _freeze.record(a.paths, out)
    for rel, f in rec["files"].items():
        fp = "fingerprint" if f["fingerprint"] else "bytes only"
        print(f"  {f['kind']:<6} {fp:<11} {rel}" + (f"  ({f['note']})" if f["note"] else ""))
    for d in rec["subdirs_not_recorded"]:
        print(f"  (subfolder not recorded: {d})")
    out.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"recorded {len(rec['files'])} files in {out}")
    return PASS


def cmd_freeze_check(a) -> int:
    r = _freeze.check(a.record, allow_equivalent=a.allow_equivalent)
    for e in r.entries:
        if e["status"] == "SAME" and not a.verbose:
            continue
        print(f"  {e['status']:<10} {e['path']}" + (f"  ({e['note']})" if e["note"] else ""))
        for c in e["changes"]:
            print(f"             {c}")
    print(" / ".join(f"{k} {v}" for k, v in r.to_dict()["counts"].items()))
    if r.count("EQUIVALENT") and not a.allow_equivalent:
        print("(EQUIVALENT files were rewritten without a real change; restore them from the backup, or pass "
              "--allow-equivalent if a rewrite is acceptable)")
    _write_json(a.json, r.to_dict())
    print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pcb-guardrails", description="Independent checks of PCB fab outputs.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("layers", help="show how the files in a Gerber folder were classified")
    p.add_argument("gerbers")
    p.set_defaults(fn=cmd_layers)

    p = sub.add_parser("netcheck", help="Gerber + drill connectivity vs. the schematic netlist (OPEN / SHORT)")
    p.add_argument("gerbers", help="folder with the Gerber and drill files you upload to the fab")
    p.add_argument("--netlist", required=True, help="KiCad XML netlist: kicad-cli sch export netlist --format kicadxml")
    p.add_argument("--pcb", required=True, help=".kicad_pcb (pad positions only; its nets are not used)")
    p.add_argument("--json", help="write the full result as JSON")
    p.add_argument("--net-csv", help="write one row per net")
    p.add_argument("--clearance-report", type=float, default=0.2, help="report copper gaps below this (mm), default 0.2")
    p.add_argument("--allow-no-drill", action="store_true")
    p.add_argument("--format", choices=["text", "agent"], default="text",
                   help="agent: a report for an AI agent to read (verdict, evidence, likely causes, next step)")
    p.set_defaults(fn=cmd_netcheck)

    p = sub.add_parser("dfm", help="measure trace widths, gaps, holes, rings; judge with --rules")
    p.add_argument("gerbers")
    p.add_argument("--pcb", help=".kicad_pcb for via and PTH-pad annular rings")
    p.add_argument("--rules", help="JSON with your fab's minimums (see examples/rules.example.json)")
    p.add_argument("--gap-window", type=float, default=0.5, help="search copper gaps up to this many mm (default 0.5); None = none that small")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_dfm)

    p = sub.add_parser("fill", help="copper coverage per layer (catches unfilled pours)")
    p.add_argument("gerbers")
    p.add_argument("--min", action="append", metavar="LAYER=RATIO", help="e.g. In1.Cu=0.8 (repeatable)")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_fill)

    p = sub.add_parser("wrongnet", help="tracks / vias in a .kicad_pcb touching a pad of a different net")
    p.add_argument("pcb")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_wrongnet)

    p = sub.add_parser("libcheck", help="symbols / footprints exist on disk and their pin / pad numbers agree")
    p.add_argument("--parts", help="parts.csv (ref,symbol,footprint[,dnp]); check before generating a schematic")
    p.add_argument("--sch", help=".kicad_sch (with sub-sheets); with --parts, also compared with the parts list")
    p.add_argument("--project-dir", help="folder with the project's sym-lib-table / fp-lib-table (default: that of --sch or --parts)")
    p.add_argument("--global-tables", help="folder with KiCad's global sym-lib-table / fp-lib-table (KiCad's user config folder)")
    p.add_argument("--kicad-share", help="KiCad's folder holding symbols/ and footprints/ (sets KICADnn_*_DIR)")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="a path variable used in the tables (repeatable)")
    p.add_argument("--sym-lib-table", action="append", help="an extra symbol library table (repeatable)")
    p.add_argument("--fp-lib-table", action="append", help="an extra footprint library table (repeatable)")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_libcheck)

    p = sub.add_parser("dnp", help="DNP agrees between the parts list, schematic and board; exclusive groups")
    p.add_argument("--parts", help="parts.csv with a dnp column")
    p.add_argument("--sch", help=".kicad_sch (with sub-sheets)")
    p.add_argument("--pcb", help=".kicad_pcb")
    p.add_argument("--exclusive", action="append", metavar="REF,REF[,REF]",
                   help="parts that must never be fitted together (repeatable)")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_dnp)

    p = sub.add_parser("freeze", help="design freeze: record fingerprints of frozen files, then check nothing changed")
    fsub = p.add_subparsers(dest="freeze_cmd", required=True)
    q = fsub.add_parser("record", help="fingerprint files / folders (folders: their files, not recursive)")
    q.add_argument("paths", nargs="+")
    q.add_argument("-o", "--output", required=True, help="freeze record to write (JSON); paths are stored relative to it")
    q.add_argument("--force", action="store_true", help="replace an existing record")
    q.set_defaults(fn=cmd_freeze_record)
    q = fsub.add_parser("check", help="compare the files with a freeze record")
    q.add_argument("record")
    q.add_argument("--allow-equivalent", action="store_true",
                   help="pass files whose bytes changed but whose geometry/content did not (new UUIDs, dates, order)")
    q.add_argument("--json")
    q.add_argument("-v", "--verbose", action="store_true", help="also list unchanged files")
    q.set_defaults(fn=cmd_freeze_check)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (UnsupportedInput, OSError, ValueError) as e:
        print(f"CANNOT CHECK: {e}", file=sys.stderr)
        if getattr(a, "format", "text") == "agent":
            print(render_cannot(a.cmd, str(e)), end="")
        return CANNOT


if __name__ == "__main__":
    sys.exit(main())
