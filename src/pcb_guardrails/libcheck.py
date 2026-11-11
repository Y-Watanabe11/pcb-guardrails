"""Library preflight: every symbol and footprint a design names exists on disk, and their pin / pad numbers agree.

Run it on the parts list *before* generating a schematic (so a generator never invents a symbol or swaps in a
generic one), or on an existing schematic. Libraries are found the way KiCad finds them: the project's
``sym-lib-table`` / ``fp-lib-table``, then the global tables you point to, following nested tables
(``type "Table"``) and ``${VARIABLES}``.

KiCad sets its own library variables (``KICAD10_SYMBOL_DIR`` ...) at run time and does not store them; pass
``--kicad-share`` (the folder that holds ``symbols`` / ``footprints``) or ``--var NAME=PATH``. A library whose
location cannot be resolved stops the check (exit 2); it is never reported as present or missing.

Pin / pad numbers (KiCad joins symbol pins to footprint pads by number):
    pin without a pad   FAIL (the schematic connection can never reach copper)
    pad without a pin   WARN (often a mounting or shield pad; an exposed thermal pad here is a real error)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import sexpdata

from .gerber import UnsupportedInput
from .kicad import _S, _kids
from .parts import natural_key

_VAR = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")
_SHARE_DIRS = {"SYMBOL": "symbols", "FOOTPRINT": "footprints", "TEMPLATE": "template", "3DMODEL": "3dmodels"}


class Vars:
    def __init__(self, explicit: dict[str, str], kicad_share: str | None, project_dir: Path):
        self.explicit = dict(explicit)
        self.share = Path(kicad_share) if kicad_share else None
        self.project_dir = project_dir

    def get(self, name: str) -> str | None:
        if name in self.explicit:
            return self.explicit[name]
        if name == "KIPRJMOD":
            return str(self.project_dir)
        m = re.fullmatch(r"KICAD\d*_(SYMBOL|FOOTPRINT|TEMPLATE|3DMODEL)_DIR", name)
        if m and self.share:
            return str(self.share / _SHARE_DIRS[m.group(1)])
        return os.environ.get(name)

    def expand(self, uri: str, what: str) -> Path:
        def sub(m):
            name = m.group(1) or m.group(2)
            v = self.get(name)
            if v is None:
                hint = " or --kicad-share <KiCad's folder with symbols/ and footprints/>" if name.startswith("KICAD") else ""
                raise UnsupportedInput(f"{what}: ${{{name}}} is not defined; pass --var {name}=<path>{hint}")
            return v
        return Path(_VAR.sub(sub, uri)).expanduser()


@dataclass
class LibEntry:
    nick: str
    type: str
    uri: str
    disabled: bool
    table: Path


@dataclass
class LibTables:
    """Nickname -> library, first definition wins (project tables are read before global ones)."""
    vars: Vars
    entries: dict[str, LibEntry] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)   # nested tables that could not be read, with the reason
    has_global: bool = False

    def add_table(self, path: Path, depth: int = 0) -> None:
        if depth > 8:
            raise UnsupportedInput(f"{path}: library tables nested too deep")
        tree = sexpdata.loads(path.read_text(encoding="utf-8"))
        for lib in _kids(tree, "lib"):
            d = {str(_S(c[0])): (str(_S(c[1])) if len(c) > 1 else True) for c in lib[1:] if isinstance(c, list) and c}
            nick, typ, uri = d.get("name", ""), d.get("type", ""), d.get("uri", "")
            if typ == "Table":
                try:
                    sub = self.vars.expand(uri, f"{path} ({nick})")
                    self.add_table(sub, depth + 1)
                except (UnsupportedInput, OSError) as e:
                    self.unreadable.append(f"{uri} ({e})")
                continue
            self.entries.setdefault(nick, LibEntry(nick, typ, uri, "disabled" in d, path))

    def locate(self, nick: str, kind: str) -> Path:
        """Path of library ``nick``; raises LookupError (a FAIL) or UnsupportedInput (cannot check)."""
        e = self.entries.get(nick)
        if e is None:
            if self.unreadable:
                raise UnsupportedInput(f"{kind} library {nick!r} not found, and these nested tables could not be read: "
                                       + "; ".join(self.unreadable))
            if not self.has_global:
                raise UnsupportedInput(f"{kind} library {nick!r} is not in the project table; pass --global-tables "
                                       "<KiCad's user config folder> to look in the global tables too")
            raise LookupError(f"{kind} library {nick!r} is not in any library table")
        if e.disabled:
            raise LookupError(f"{kind} library {nick!r} is disabled in {e.table}")
        if e.type != "KiCad":
            raise UnsupportedInput(f"{kind} library {nick!r} has type {e.type!r}; only KiCad libraries are supported")
        return self.vars.expand(e.uri, f"{kind} library {nick!r} in {e.table}")


def load_tables(kind: str, project_dir: Path, extra: list[str], global_dir: str | None, vars_: Vars) -> LibTables:
    name = "sym-lib-table" if kind == "symbol" else "fp-lib-table"
    t = LibTables(vars_)
    for p in [project_dir / name, *map(Path, extra)]:
        if p.is_file():
            t.add_table(p)
    if global_dir:
        g = Path(global_dir) / name
        if not g.is_file():
            raise UnsupportedInput(f"{g} not found")
        t.add_table(g)
        t.has_global = True
    return t


# ----------------------------------------------------------------------------- library contents
class Libraries:
    def __init__(self, sym: LibTables, fp: LibTables):
        self.sym, self.fp = sym, fp
        self._sym_files: dict[Path, dict[str, list]] = {}

    def _symbols_in(self, path: Path) -> dict[str, list]:
        if path not in self._sym_files:
            if path.is_dir():
                raise UnsupportedInput(f"{path}: symbol libraries stored as a folder are not supported")
            if not path.is_file():
                raise LookupError(f"symbol library file {path} does not exist")
            tree = sexpdata.loads(path.read_text(encoding="utf-8"))
            self._sym_files[path] = {str(s[1]): s for s in _kids(tree, "symbol")}
        return self._sym_files[path]

    def symbol(self, lib_id: str) -> tuple[set[str], str]:
        """(pin numbers, default footprint) of ``Library:Name``; LookupError if it does not exist."""
        nick, name = _split(lib_id, "symbol")
        syms = self._symbols_in(self.sym.locate(nick, "symbol"))
        if name not in syms:
            raise LookupError(f"symbol {lib_id}: no symbol {name!r} in library {nick!r}")
        node, pins, chain = syms[name], set(), []
        footprint = ""
        while True:
            chain.append(str(node[1]))
            footprint = footprint or next((str(p[2]) for p in _kids(node, "property") if p[1] == "Footprint"), "")
            pins |= _pins(node)
            ext = _kids(node, "extends")
            if not ext:
                break
            parent = str(ext[0][1])
            if parent not in syms or parent in chain:
                raise LookupError(f"symbol {lib_id}: extends {parent!r}, which is not in library {nick!r}")
            node = syms[parent]
        return pins, footprint

    def footprint(self, fpid: str) -> set[str]:
        """Copper pad numbers of ``Library:Name``; LookupError if it does not exist."""
        nick, name = _split(fpid, "footprint")
        d = self.fp.locate(nick, "footprint")
        if not d.is_dir():
            raise LookupError(f"footprint library folder {d} does not exist")
        f = d / f"{name}.kicad_mod"
        if not f.is_file():
            raise LookupError(f"footprint {fpid}: no {f.name} in {d}")
        tree = sexpdata.loads(f.read_text(encoding="utf-8"))
        pads = set()
        for p in _kids(tree, "pad"):
            layers = [str(_S(v)) for lay in _kids(p, "layers") for v in lay[1:]]
            if _S(p[2]) != "np_thru_hole" and str(p[1]) and any(v.endswith(".Cu") for v in layers):
                pads.add(str(p[1]))
        return pads


def _split(lib_id: str, kind: str) -> tuple[str, str]:
    if lib_id.count(":") != 1 or not all(lib_id.split(":")):
        raise LookupError(f"{kind} {lib_id!r} is not of the form Library:Name")
    nick, name = lib_id.split(":")
    return nick, name


def _pins(sym) -> set[str]:
    out, stack = set(), list(sym[1:])
    while stack:
        n = stack.pop()
        if not isinstance(n, list) or not n:
            continue
        if _S(n[0]) == "pin":
            for num in _kids(n, "number"):
                out.add(str(num[1]))
            continue
        stack.extend(n[1:])
    return out


# ----------------------------------------------------------------------------- check
@dataclass
class Item:
    ref: str
    symbol: str
    footprint: str            # as given; empty = take the symbol's default
    needs_footprint: bool = True
    source: str = ""
    default_footprint_ok: bool = True   # parts list: an empty footprint means "the symbol's default"; schematic: none


@dataclass
class LibResult:
    items: int = 0
    symbols_ok: int = 0
    footprints_ok: int = 0
    fails: list[dict] = field(default_factory=list)
    warns: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fails

    def to_dict(self) -> dict:
        return {"ok": self.ok, "items": self.items, "symbols_ok": self.symbols_ok, "footprints_ok": self.footprints_ok,
                "fails": self.fails, "warnings": self.warns}


def run_libcheck(items: list[Item], libs: Libraries) -> LibResult:
    res = LibResult(items=len(items))
    for it in items:
        def fail(msg):
            res.fails.append({"ref": it.ref, "problem": msg, "source": it.source})
        if not it.symbol:
            fail("no symbol given")
            continue
        try:
            pins, default_fp = libs.symbol(it.symbol)
        except LookupError as e:
            fail(str(e))
            continue
        res.symbols_ok += 1
        if not it.needs_footprint:
            continue
        fpid = it.footprint or (default_fp if it.default_footprint_ok else "")
        if not fpid:
            fail("no footprint assigned in the schematic" if not it.default_footprint_ok
                 else f"no footprint given and symbol {it.symbol} has no default footprint")
            continue
        try:
            pads = libs.footprint(fpid)
        except LookupError as e:
            fail(str(e))
            continue
        res.footprints_ok += 1
        if pins - pads:
            fail(f"{it.symbol} pins {sorted(pins - pads, key=natural_key)} have no pad in {fpid}")
        if pads - pins:
            res.warns.append({"ref": it.ref, "problem": f"{fpid} pads {sorted(pads - pins, key=natural_key)} have no pin in "
                              f"{it.symbol} (no net will reach them)", "source": it.source})
    return res
