"""Command line: ``pcb-guardrails <check> ...``

Exit codes (stable, for agents and CI):
    0  all checks passed
    1  a check failed (OPEN / SHORT / missing copper / rule violation / pour missing / frozen file changed)
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
from .dfm import apply_rules, run_dfm
from .fill import run_fill
from . import freeze as _freeze
from .gerber import UnsupportedInput, discover
from .kicad import copper_name, load_netlist, load_pcb
from .netcheck import run_netcheck

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
    pads, _ = load_pcb(a.pcb)
    netlist = load_netlist(a.netlist)
    r = run_netcheck(files, pads, netlist, clearance_report=a.clearance_report)
    print(f"pads {r.pads}, schematic nets with pads {r.nets}, copper layers {len(files.copper)}, drill files {len(files.drills)}")
    print(f"OPEN {len(r.opens)} / SHORT {len(r.shorts)} / NO-COPPER {len(r.no_copper)} / "
          f"pads of parts not in the schematic {len(r.not_in_netlist)} / copper islands without pads {len(r.islands)}")
    for o in r.opens:
        print(f"  OPEN  {o['net']}: " + " | ".join(", ".join(g) for g in o["groups"]))
    for s in r.shorts:
        print(f"  SHORT {' + '.join(s['nets'])}: {', '.join(s['pads'][:12])}")
    for p in r.no_copper:
        print(f"  NO-COPPER {p['pad']} ({p['net']}) at {p['xy_kicad']} on {p['layers']}")
    for l, c in r.clearance.items():
        m = c["min"]
        if m:
            print(f"  {l}: smallest gap between different copper {m['mm']} mm ({' / '.join(m['between'])}) at {m['at_kicad']}")
    if a.net_csv:
        with open(a.net_csv, "w", newline="", encoding="utf-8") as fo:
            w = csv.writer(fo)
            w.writerow(["net", "pads", "copper_pieces", "layers", "status", "pad_list"])
            for row in r.per_net:
                w.writerow([row["net"], row["pads"], row["copper_pieces"], " ".join(row["layers"]), row["status"], " ".join(row["pad_list"])])
    _write_json(a.json, r.to_dict())
    print("PASS" if r.ok else "FAIL")
    return PASS if r.ok else FAIL


def cmd_dfm(a) -> int:
    files = discover(a.gerbers)
    pads, vias = load_pcb(a.pcb) if a.pcb else (None, None)
    m = run_dfm(files, pads, vias, gap_window=a.gap_window)
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
    violations = []
    if a.rules:
        rules = json.loads(Path(a.rules).read_text())
        violations = apply_rules(m, rules)
        for v in violations:
            print(f"  VIOLATION {v}")
        print("PASS" if not violations else "FAIL")
    else:
        print("(measured only; pass --rules <your fab's rules.json> to judge)")
    m["violations"] = violations
    _write_json(a.json, m)
    return FAIL if violations else PASS


def cmd_fill(a) -> int:
    files = discover(a.gerbers)
    mins = {}
    for spec in a.min or []:
        k, v = spec.split("=")
        mins[k] = float(v)
    r = run_fill(files, mins)
    print(f"board area {r['board_area_mm2']} mm2")
    for l, i in r["layers"].items():
        need = mins.get(l)
        flag = "" if need is None else ("  OK" if i["coverage"] >= need else f"  FAIL (need >= {need:.0%})")
        print(f"  {l:<7} copper {i['coverage']:.1%}{flag}")
    _write_json(a.json, r)
    if not mins:
        print("(measured only; pass --min LAYER=RATIO, e.g. --min In1.Cu=0.8, to judge)")
    print("PASS" if r["ok"] else "FAIL")
    return PASS if r["ok"] else FAIL


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
        return CANNOT


if __name__ == "__main__":
    sys.exit(main())
