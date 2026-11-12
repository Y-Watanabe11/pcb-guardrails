"""Design-freeze fingerprints: record frozen files, and later prove they were not changed by a tool run.

Each file gets two fingerprints:

    sha256       the exact bytes. Any rewrite changes it, including one that only renews IDs or timestamps.
    fingerprint  what the file *means*, so a rewrite that changed nothing real can be told apart:
                 Gerber       the copper/artwork geometry after parsing (union of all objects, on a 0.1 um grid),
                              plus the X2 file function. Comments, attributes, aperture numbers and object order
                              do not count.
                 Excellon     every hole (plating, diameter, position, slot extent) and the file function.
                              Tool numbers and hole order do not count.
                 Gerber job   the .gbrjob JSON without ``Header.CreationDate`` / ``Header.GenerationSoftware``.
                 KiCad files  the S-expression with UUID values, ``tstamp``/``uuid``/``generator`` entries and the
                 (.kicad_pcb, .kicad_sch, .kicad_sym, .kicad_mod)
                              order of sibling items removed (order is kept where it carries meaning: polygon
                              points, the stack-up, table cells). Numbers are compared as numbers, quoted /
                              unquoted words as words, and ``(at x y 0)`` as ``(at x y)``.
                 other files  bytes only.

Check statuses:

    SAME        bytes identical
    EQUIVALENT  bytes differ, fingerprint identical (e.g. a regenerated file with new UUIDs or a new date)
    CHANGED     fingerprint differs, or bytes differ and the file has no fingerprint
    MISSING     a recorded file is gone
    NEW         a file appeared in a recorded folder

A fingerprint that cannot be computed never counts as a match: the file then falls back to bytes only.
"""
from __future__ import annotations

import collections
import datetime as _dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import sexpdata
from shapely.ops import unary_union

from . import __version__
from .gerber import UnsupportedInput, file_function, parse_drill, parse_gerber

FORMAT = 1
GRID_MM = 0.0001
SEXPR_SUFFIXES = {".kicad_pcb", ".kicad_sch", ".kicad_sym", ".kicad_mod"}

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DROP = {"uuid", "tstamp", "generator", "generator_version"}
_KEEP_ORDER = {"pts", "stackup", "cells"}


# ----------------------------------------------------------------------------- fingerprints
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kind_of(path: Path, head: bytes) -> str:
    if path.suffix.lower() in SEXPR_SUFFIXES:
        return "kicad"
    if path.suffix.lower() == ".gbrjob":
        return "gbrjob"
    text = head.decode("latin-1")
    if text.lstrip().startswith("M48") or re.search(r"^M48\s*$", text, re.M):
        return "drill"
    if "%FS" in text or "%MO" in text:
        return "gerber"
    return "bytes"


def _sexpr_canon(node) -> str | None:
    if isinstance(node, list):
        key = _key(node)
        if key in _DROP:
            return None
        if key == "at" and len(node) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in node[1:]) \
                and node[3] == 0:
            node = node[:3]  # (at x y 0) == (at x y): KiCad writes the zero angle on some items when it re-saves
        pairs = [(c, p) for c, p in ((c, _sexpr_canon(c)) for c in node) if p is not None]
        if key in _KEEP_ORDER:
            parts = [p for _, p in pairs]
        else:  # positional atoms keep their order; child expressions are a set (KiCad may reorder them)
            parts = [p for c, p in pairs if not isinstance(c, list)] + sorted(p for c, p in pairs if isinstance(c, list))
        return "(" + " ".join(parts) + ")"
    if isinstance(node, sexpdata.Symbol):
        node = node.value()
    if isinstance(node, bool) or node is None:
        return repr(node)
    if isinstance(node, (int, float)):
        v = float(node)
        return "n" + repr(0.0 if v == 0 else v)
    return json.dumps(_UUID.sub("<uuid>", str(node)), ensure_ascii=False)


def _kicad_fingerprint(path: Path) -> tuple[str, dict]:
    exprs = sexpdata.parse(path.read_text(encoding="utf-8"), nil=None, true=None)
    canon = "\n".join(c for c in (_sexpr_canon(e) for e in exprs) if c is not None)
    counts: collections.Counter = collections.Counter()
    for e in exprs:
        for c in e[1:] if isinstance(e, list) else []:
            if _key(c) is not None and _key(c) not in _DROP:
                counts[_key(c)] += 1
    summary = dict(sorted(counts.items()))
    if any(_key(e) == "kicad_pcb" for e in exprs):
        names, form = _net_names(exprs)
        summary["net_names_used"], summary["net_ref_form"] = len(names), form
    return _sha256(canon.encode()), summary


def _key(node) -> str | None:
    return node[0].value() if isinstance(node, list) and node and isinstance(node[0], sexpdata.Symbol) else None


def _net_names(exprs) -> tuple[set[str], str]:
    """Net names a board refers to, whatever the format, and the form of its references.

    KiCad <= 9 declares ``(net 3 "GND")`` at the top and refers by number (``(net 3 "GND")`` or ``(net 3)``);
    KiCad 10 refers by name (``(net "GND")``) and has no declarations. A format upgrade changes the declaration
    count but not the set of names."""
    decl, names, stack = {}, set(), [c for e in exprs if isinstance(e, list) for c in e[1:]]
    numbered, by_number, by_name = [], 0, 0
    top = {id(c) for e in exprs if isinstance(e, list) for c in e[1:]}
    while stack:
        n = stack.pop()
        if not isinstance(n, list):
            continue
        if _key(n) == "net" and len(n) >= 2:
            a = n[1].value() if isinstance(n[1], sexpdata.Symbol) else n[1]
            if isinstance(a, int):
                if len(n) >= 3 and isinstance(n[2], str):
                    names.add(n[2])
                    if id(n) in top:
                        decl.setdefault(a, n[2])
                else:
                    numbered.append(a)
                by_number += id(n) not in top
            elif isinstance(a, str):
                names.add(a)
                by_name += 1
        stack.extend(n[1:])
    names.update(decl[i] for i in numbered if i in decl)
    names.discard("")
    form = {(False, False): "none", (True, False): "number", (False, True): "name"}.get((by_number > 0, by_name > 0), "mixed")
    return names, form


def _ring_key(coords, grid: float, ccw: bool) -> tuple:
    pts = [(round(x / grid), round(y / grid)) for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    changed = True
    while changed and len(pts) >= 3:  # drop repeated and exactly collinear points (union order can leave extra nodes)
        changed = False
        out = []
        n = len(pts)
        for i in range(n):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
            if b == a or (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) == 0:
                changed = True
                continue
            out.append(b)
        pts = out
    if len(pts) < 3:
        return ()
    area2 = sum(pts[i - 1][0] * pts[i][1] - pts[i][0] * pts[i - 1][1] for i in range(len(pts)))
    if (area2 > 0) != ccw:
        pts.reverse()
    k = pts.index(min(pts))
    return tuple(pts[k:] + pts[:k])


def _geometry_digest(geom, grid: float) -> str:
    polys = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type != "Polygon" or g.is_empty:
            if hasattr(g, "geoms"):
                polys.append(_geometry_digest(g, grid))
            continue
        ext = _ring_key(g.exterior.coords, grid, True)
        if not ext:
            continue
        holes = sorted(h for h in (_ring_key(i.coords, grid, False) for i in g.interiors) if h)
        polys.append(repr((ext, holes)))
    return _sha256("\n".join(sorted(polys)).encode())


def _gerber_fingerprint(path: Path, grid: float) -> tuple[str, dict]:
    layer = parse_gerber(path)
    geom = unary_union(layer.objects) if layer.objects else None
    digest = _sha256(repr((layer.function, _geometry_digest(geom, grid) if geom is not None else "")).encode())
    summary = {"function": ",".join(layer.function) if layer.function else None, "objects": len(layer.objects),
               "area_mm2": round(geom.area, 4) if geom is not None else 0.0,
               "bounds_mm": [round(v, 4) for v in geom.bounds] if geom is not None else None}
    return digest, summary


def _gbrjob_fingerprint(path: Path) -> tuple[str, dict]:
    job = json.loads(path.read_text(encoding="utf-8"))
    header = job.get("Header") if isinstance(job, dict) else None
    if isinstance(header, dict):
        header = {k: v for k, v in header.items() if k not in ("CreationDate", "GenerationSoftware")}
        job = {**job, "Header": header}
    files = job.get("FilesAttributes", []) if isinstance(job, dict) else []
    return _sha256(json.dumps(job, sort_keys=True).encode()), {"files_listed": len(files)}


def _drill_fingerprint(path: Path, grid: float) -> tuple[str, dict]:
    holes = parse_drill(path)
    q = lambda v: round(v / grid)  # noqa: E731
    items = sorted((h.plated, q(h.diameter), q(h.xy[0]), q(h.xy[1]), tuple(q(b) for b in h.geom.bounds)) for h in holes)
    ff = file_function(path.read_text(errors="replace")[:6000])
    sizes = collections.Counter(f"{round(h.diameter, 4)} {'PTH' if h.plated else 'NPTH'}" for h in holes)
    summary = {"function": ",".join(ff) if ff else None, "holes": len(holes), "sizes": dict(sorted(sizes.items()))}
    return _sha256(repr((ff, items)).encode()), summary


def fingerprint(path: Path, grid: float = GRID_MM) -> dict:
    """``{sha256, bytes, kind, fingerprint, summary, note}`` for one file. ``fingerprint`` is None when the file
    has none (unknown kind) or it could not be computed (``note`` says why)."""
    data = path.read_bytes()
    kind = kind_of(path, data[:6000])
    rec = {"sha256": _sha256(data), "bytes": len(data), "kind": kind, "fingerprint": None, "summary": None, "note": None}
    try:
        if kind == "kicad":
            rec["fingerprint"], rec["summary"] = _kicad_fingerprint(path)
        elif kind == "gerber":
            rec["fingerprint"], rec["summary"] = _gerber_fingerprint(path, grid)
        elif kind == "drill":
            rec["fingerprint"], rec["summary"] = _drill_fingerprint(path, grid)
        elif kind == "gbrjob":
            rec["fingerprint"], rec["summary"] = _gbrjob_fingerprint(path)
    except UnsupportedInput as e:
        rec["note"] = f"no fingerprint (bytes only): {e}"
    except Exception as e:  # a parser failure must fall back to bytes only, never to a match
        rec["note"] = f"no fingerprint (bytes only): {type(e).__name__}: {e}"
    return rec


# ----------------------------------------------------------------------------- record / check
def _dir_files(d: Path, skip: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith(".") and p.resolve() != skip)


def _rel(p: Path, base: Path) -> str:
    return Path(os.path.relpath(p.resolve(), base)).as_posix()


def record(paths: list[str | Path], freeze_path: str | Path, grid: float = GRID_MM) -> dict:
    """Fingerprint ``paths`` (files, or folders: their files, not recursive, hidden files skipped).
    Paths are stored relative to the freeze file's folder."""
    freeze_path = Path(freeze_path)
    base = freeze_path.resolve().parent
    skip = freeze_path.resolve()
    files: dict[str, dict] = {}
    dirs, skipped_dirs = [], []
    for p in map(Path, paths):
        if p.is_dir():
            dirs.append(_rel(p, base))
            skipped_dirs += [_rel(s, base) for s in sorted(p.iterdir()) if s.is_dir() and not s.name.startswith(".")]
            targets = _dir_files(p, skip)
        elif p.is_file():
            targets = [p]
        else:
            raise FileNotFoundError(f"{p} does not exist")
        for f in targets:
            files[_rel(f, base)] = fingerprint(f, grid)
    if not files:
        raise ValueError("nothing to record: no files in the given paths")
    return {"pcb_guardrails_freeze": FORMAT, "tool_version": __version__,
            "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "grid_mm": grid, "dirs": sorted(set(dirs)), "subdirs_not_recorded": skipped_dirs, "files": files}


@dataclass
class FreezeResult:
    entries: list[dict] = field(default_factory=list)   # {path, status, note, changes}
    allow_equivalent: bool = False

    def count(self, status: str) -> int:
        return sum(e["status"] == status for e in self.entries)

    @property
    def ok(self) -> bool:
        good = {"SAME", "EQUIVALENT"} if self.allow_equivalent else {"SAME"}
        return all(e["status"] in good for e in self.entries)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "allow_equivalent": self.allow_equivalent,
                "counts": {s: self.count(s) for s in ("SAME", "EQUIVALENT", "CHANGED", "MISSING", "NEW")},
                "files": self.entries}


def _summary_changes(old: dict | None, new: dict | None) -> list[str]:
    if not old or not new:
        return []
    out = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out.append(f"{k} {old.get(k, 0)} -> {new.get(k, 0)}")
    if old.get("net") != new.get("net") and "net_names_used" in old and old.get("net_names_used") == new.get("net_names_used"):
        out.append(f"net_names_used {old['net_names_used']} (unchanged)")
    return out


def load_freeze(freeze_path: str | Path) -> dict:
    try:
        rec = json.loads(Path(freeze_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise UnsupportedInput(f"{freeze_path}: not a freeze record ({e})") from e
    if not isinstance(rec, dict) or rec.get("pcb_guardrails_freeze") != FORMAT or not isinstance(rec.get("files"), dict):
        raise UnsupportedInput(f"{freeze_path}: not a pcb-guardrails freeze record (format {FORMAT})")
    return rec


def check(freeze_path: str | Path, allow_equivalent: bool = False) -> FreezeResult:
    freeze_path = Path(freeze_path)
    rec = load_freeze(freeze_path)
    base = freeze_path.resolve().parent
    grid = float(rec.get("grid_mm", GRID_MM))
    res = FreezeResult(allow_equivalent=allow_equivalent)
    for rel, old in sorted(rec["files"].items()):
        p = base / rel
        if not p.is_file():
            res.entries.append({"path": rel, "status": "MISSING", "note": None, "changes": []})
            continue
        data = p.read_bytes()
        if _sha256(data) == old["sha256"]:
            res.entries.append({"path": rel, "status": "SAME", "note": None, "changes": []})
            continue
        new = fingerprint(p, grid)
        if old.get("fingerprint") and new["fingerprint"] == old["fingerprint"]:
            status, note = "EQUIVALENT", "bytes differ, " + {
                "kicad": "content identical apart from UUIDs, timestamps, generator and item order",
                "gerber": "geometry identical (attributes, comments, aperture numbers or order differ)",
                "drill": "holes identical (comments, tool numbers or order differ)",
                "gbrjob": "job identical apart from its creation date / software version"}[new["kind"]]
        else:
            status = "CHANGED"
            note = old.get("note") or new["note"] or (None if old.get("fingerprint") else "bytes differ (no fingerprint for this file type)")
        res.entries.append({"path": rel, "status": status, "note": note,
                            "changes": _summary_changes(old.get("summary"), new["summary"])})
    skip = freeze_path.resolve()
    for d in rec.get("dirs", []):
        dp = base / d
        if dp.is_dir():
            for f in _dir_files(dp, skip):
                rel = _rel(f, base)
                if rel not in rec["files"]:
                    res.entries.append({"path": rel, "status": "NEW", "note": f"not in the record of folder {d}", "changes": []})
    return res
