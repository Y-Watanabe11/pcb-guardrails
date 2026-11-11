"""The parts list (``parts.csv``): the human-reviewed source for symbols, footprints and DNP.

Columns (header row required, names case-insensitive, extra columns ignored):

    ref        R1, U3, ... (required, unique)
    symbol     symbol library id, ``Library:Name``
    footprint  footprint library id, ``Library:Name``
    dnp        yes / no (also 1/0, true/false, y/n, dnp; empty = no)

Anything else in the ``dnp`` column stops the check: a note like "DNP by default?" or "see D1" is exactly the kind
of wording that once kept a DNP flag from ever reaching the schematic and board.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .gerber import UnsupportedInput

DNP_TRUE = {"1", "yes", "y", "true", "dnp"}
DNP_FALSE = {"", "0", "no", "n", "false"}


@dataclass
class Part:
    ref: str
    symbol: str
    footprint: str
    dnp: bool | None      # None when the file has no dnp column
    line: int


def parse_dnp(value: str, where: str) -> bool:
    v = value.strip().lower()
    if v in DNP_TRUE:
        return True
    if v in DNP_FALSE:
        return False
    raise UnsupportedInput(f"{where}: dnp value {value!r} is not yes/no; write the decision explicitly")


def load_parts(path: str | Path) -> list[Part]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise UnsupportedInput(f"{path}: empty parts list")
    head = [h.strip().lower() for h in rows[0]]
    if "ref" not in head:
        raise UnsupportedInput(f"{path}: no 'ref' column in the header {rows[0]}")
    col = {h: i for i, h in enumerate(head)}

    def get(r, name):
        i = col.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    parts, seen = [], {}
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        ref = get(r, "ref")
        if not ref:
            raise UnsupportedInput(f"{path}:{n}: empty ref")
        if ref in seen:
            raise UnsupportedInput(f"{path}:{n}: ref {ref} already on line {seen[ref]}")
        seen[ref] = n
        dnp = parse_dnp(get(r, "dnp"), f"{path}:{n} ({ref})") if "dnp" in col else None
        parts.append(Part(ref, get(r, "symbol"), get(r, "footprint"), dnp, n))
    return parts


def natural_key(s: str):
    """R2 before R10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
