"""wrongnet: a track or via touching a pad of another net is reported; correct pad geometry keeps near misses quiet.

The board is written here so every pad shape and angle is known:

    U1  (10,10) rotated 90: pad 1 (net A) spans x 9.75..10.25, y 10.25..11.75; pad 2 (net B) y 8.25..9.75
    R1  (20,10) roundrect 2x2, rratio 0.5 (a disc of radius 1), net A
    J1  (30,10) custom: 0.5 mm square anchor plus a polygon to x = 32, net A
    NT1 (40,10) net tie: pads 1 (A) and 2 (B) touch each other on purpose
    R2  (17,22) round SMD pad, net A
    P1  (50,10) plated through-hole pad, net A
    O1  (60,10) through-hole pad 2 x 1 with drill offset (0.5, 0): hole at x 60, copper x 59.5..61.5, net A
    K1  (70,10) custom pad: an arc primitive through (71, 11), net A
"""
import json

import pytest

from pcb_guardrails.cli import main
from pcb_guardrails.wrongnet import run_wrongnet


def board(tmp_path, items, numbered=False):
    def net(n):
        if not n:
            return ""
        return f'(net {1 if n == "A" else 2} "{n}")' if numbered else f'(net "{n}")'

    def tnet(n):
        return f"(net {1 if n == 'A' else 2})" if numbered else f'(net "{n}")'

    head = ('(kicad_pcb (version 20240108) (generator "test")\n'
            ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n'
            + (' (net 0 "") (net 1 "A") (net 2 "B")\n' if numbered else ""))
    fps = f''' (footprint "T:U" (layer "F.Cu") (at 10 10 90) (property "Reference" "U1")
  (pad "1" smd rect (at -1 0 90) (size 1.5 0.5) (layers "F.Cu") {net("A")})
  (pad "2" smd rect (at 1 0 90) (size 1.5 0.5) (layers "F.Cu") {net("B")}))
 (footprint "T:R" (layer "F.Cu") (at 20 10) (property "Reference" "R1")
  (pad "1" smd roundrect (at 0 0) (size 2 2) (roundrect_rratio 0.5) (layers "F.Cu") {net("A")}))
 (footprint "T:J" (layer "F.Cu") (at 30 10) (property "Reference" "J1")
  (pad "1" smd custom (at 0 0) (size 0.5 0.5) (layers "F.Cu") {net("A")} (options (clearance outline) (anchor rect))
   (primitives (gr_poly (pts (xy 0 0) (xy 2 0) (xy 2 0.5) (xy 0 0.5)) (width 0) (fill yes)))))
 (footprint "T:NT" (layer "F.Cu") (at 40 10) (property "Reference" "NT1") (net_tie_pad_groups "1,2")
  (pad "1" smd rect (at -0.5 0) (size 1 1) (layers "F.Cu") {net("A")})
  (pad "2" smd rect (at 0.5 0) (size 1 1) (layers "F.Cu") {net("B")}))
 (footprint "T:R2" (layer "F.Cu") (at 17 22) (property "Reference" "R2")
  (pad "1" smd circle (at 0 0) (size 0.5 0.5) (layers "F.Cu") {net("A")}))
 (footprint "T:P" (layer "F.Cu") (at 50 10) (property "Reference" "P1")
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net("A")}))
 (footprint "T:O" (layer "F.Cu") (at 60 10) (property "Reference" "O1")
  (pad "1" thru_hole rect (at 0 0) (size 2 1) (drill 0.8 (offset 0.5 0)) (layers "*.Cu" "*.Mask") {net("A")}))
 (footprint "T:K" (layer "F.Cu") (at 70 10) (property "Reference" "K1")
  (pad "1" smd custom (at 0 0) (size 0.2 0.2) (layers "F.Cu") {net("A")} (options (clearance outline) (anchor circle))
   (primitives (gr_arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2)))))
'''
    body = ""
    for it in items:
        kind, n = it[0], it[1]
        if kind == "seg":
            (x1, y1), (x2, y2), layer = it[2], it[3], it[4] if len(it) > 4 else "F.Cu"
            body += f' (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.1) (layer "{layer}") {tnet(n)})\n'
        elif kind == "via":
            x, y = it[2]
            body += f' (via (at {x} {y}) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu") {tnet(n)})\n'
        elif kind == "arc":
            (a, m, b) = it[2]
            body += f' (arc (start {a[0]} {a[1]}) (mid {m[0]} {m[1]}) (end {b[0]} {b[1]}) (width 0.1) (layer "F.Cu") {tnet(n)})\n'
    p = tmp_path / ("n.kicad_pcb" if numbered else "b.kicad_pcb")
    p.write_text(head + fps + body + ")\n")
    return p


def hits(tmp_path, items, numbered=False):
    return [(h["item"], h["item_net"], h["pad"], h["where"]) for h in run_wrongnet(board(tmp_path, items, numbered)).hits]


def test_correct_tracks_pass(tmp_path):
    items = [("seg", "A", (10, 11.6), (10, 14)),           # into U1.1 along its long (rotated) axis
             ("seg", "B", (10, 8.4), (10, 6)),             # into U1.2
             ("seg", "A", (20.5, 10), (24, 10)),           # into R1.1
             ("seg", "A", (40, 10), (40, 13)),             # onto the net tie (touches both pads)
             ("via", "B", (10, 9)), ("via", "A", (31.5, 10.2))]
    assert hits(tmp_path, items) == []
    assert main(["wrongnet", str(board(tmp_path, items))]) == 0


@pytest.mark.parametrize("numbered", [False, True])  # KiCad 10 (net "A") and KiCad <= 9 (net 1 "A") / (net 1)
def test_end_on_neighbouring_pad_is_caught(tmp_path, numbered):
    assert hits(tmp_path, [("seg", "B", (10, 11.6), (10, 14))], numbered) == [("segment", "B", "U1.1", "end")]


def test_pad_rotation_is_applied(tmp_path):
    """Unrotated, U1.1 would span x 9.25..10.75 and reach this track end; rotated by 90 it does not."""
    assert hits(tmp_path, [("seg", "B", (10.6, 11.0), (14, 11.0))]) == []


def test_roundrect_corner_is_not_copper(tmp_path):
    assert hits(tmp_path, [("seg", "B", (20.85, 9.15), (23, 7))]) == []
    assert hits(tmp_path, [("seg", "B", (20.5, 10), (23, 7))]) == [("segment", "B", "R1.1", "end")]


def test_custom_pad_primitive_counts(tmp_path):
    assert hits(tmp_path, [("seg", "B", (31.8, 10.2), (31.8, 14))]) == [("segment", "B", "J1.1", "end")]


def test_via_in_wrong_pad(tmp_path):
    assert hits(tmp_path, [("via", "B", (30.1, 10.0))]) == [("via", "B", "J1.1", "end")]


def test_arc_passing_over_a_pad(tmp_path):
    assert hits(tmp_path, [("arc", "B", ((15, 20), (17, 22), (19, 20)))]) == [("arc", "B", "R2.1", "along")]


def test_layers(tmp_path):
    """An SMD pad on F.Cu is not touched by a B.Cu track; a plated through-hole pad is."""
    assert hits(tmp_path, [("seg", "B", (20.5, 10), (23, 7), "B.Cu")]) == []
    assert hits(tmp_path, [("seg", "B", (50.3, 10), (53, 10), "B.Cu")]) == [("segment", "B", "P1.1", "end")]


def test_json_and_exit_code(tmp_path):
    out = tmp_path / "w.json"
    assert main(["wrongnet", str(board(tmp_path, [("seg", "B", (10, 11.6), (10, 14))])), "--json", str(out)]) == 1
    r = json.loads(out.read_text())
    assert r["hits"][0]["pad_net"] == "A" and r["hits"][0]["at_kicad"] == [10.0, 11.6]


def test_unsupported_pad_cannot_pass(tmp_path, capsys):
    p = board(tmp_path, [])
    p.write_text(p.read_text().replace("smd circle (at 0 0) (size 0.5 0.5)", "smd blob (at 0 0) (size 0.5 0.5)"))
    assert main(["wrongnet", str(p)]) == 2
    assert "CANNOT CHECK" in capsys.readouterr().err


def test_drill_offset_moves_the_copper_not_the_hole(tmp_path):
    """Checked against KiCad's own Gerber output: the pad position is the hole, the copper sits at position + offset."""
    assert hits(tmp_path, [("seg", "B", (61.3, 10), (64, 10))]) == [("segment", "B", "O1.1", "end")]
    assert hits(tmp_path, [("seg", "B", (59.3, 10), (56, 10))]) == []


def test_custom_pad_arc_primitive(tmp_path):
    assert hits(tmp_path, [("seg", "B", (71, 11.05), (71, 14))]) == [("segment", "B", "K1.1", "end")]
    assert hits(tmp_path, [("seg", "B", (71, 10.2), (71.3, 10.3))]) == []   # inside the arc (radius 1 about (71,10)), not on it
