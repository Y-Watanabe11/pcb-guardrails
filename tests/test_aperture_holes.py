"""Standard apertures with a round hole (Gerber Layer Format Specification 2026.05, 4.4.1-4.4.6, pp. 52-57).

The hole is the last parameter (for P after the rotation), centred on the aperture and not part of it. KiCad never
writes one; other CAD tools may. A hole read as copper is copper the fab will not make.
"""
import math

import pytest

from pcb_guardrails.cli import main
from pcb_guardrails.gerber import parse_gerber
from synth import _gerber, _path, _xy


def _layer(tmp_path, ad: str, body: str = "D20*\nX0Y0D03*\n"):
    g = tmp_path / "l.gbr"
    g.write_text("%FSLAX46Y46*%\n%MOMM*%\n%TF.FileFunction,Copper,L1,Top*%\n" f"%ADD20{ad}*%\n{body}M02*\n")
    return g


@pytest.mark.parametrize("ad, solid", [("C,2X1", math.pi), ("R,2X3X1", 6.0), ("P,2X4X0X1", 2.0)])
def test_hole_is_not_copper(tmp_path, ad, solid):
    (obj,) = parse_gerber(_layer(tmp_path, ad)).objects
    assert obj.area == pytest.approx(solid - math.pi * 0.25, rel=2e-3)
    assert not obj.contains(obj.centroid.buffer(0.1))


def test_polygon_rotation_without_hole_is_solid(tmp_path):
    (obj,) = parse_gerber(_layer(tmp_path, "P,2X4X45")).objects
    assert obj.area == pytest.approx(2.0, rel=1e-6)


@pytest.mark.parametrize("ad", ["C,1X1", "C,1X1.5", "R,2X1X1", "C,2X0", "C,2X0.5X0.5", "R,2X2X0.5X0.5", "P,2X4X0X0.5X1"])
def test_hole_that_does_not_fit_or_deprecated_form_cannot_pass(tmp_path, ad):
    """A hole must strictly fit inside the aperture; the deprecated rectangular hole is an extra parameter."""
    from pcb_guardrails.gerber import UnsupportedInput
    with pytest.raises(UnsupportedInput):
        parse_gerber(_layer(tmp_path, ad))


def test_drawing_with_a_holed_aperture_cannot_pass(tmp_path):
    from pcb_guardrails.gerber import UnsupportedInput
    with pytest.raises(UnsupportedInput, match="has a hole"):
        parse_gerber(_layer(tmp_path, "C,0.5X0.2", "D20*\nX0Y0D02*\nX1000000Y0D01*\n"))


def test_ring_flash_is_not_a_full_plane(tmp_path):
    """A plane layer whose only copper is a ring: as a solid disc it would cover 13 % of the board, the ring 2.5 %."""
    g = tmp_path / "plane"
    g.mkdir()
    (g / "p-Edge_Cuts.gm1").write_text(_gerber("Profile,NP", _path([(0, 0), (30, 0), (30, 20), (0, 20), (0, 0)], ap="D14")))
    (g / "p-F_Cu.gtl").write_text(_gerber("Copper,L1,Top", "%ADD20C,10.000000X9.000000*%\nD20*\n" + _xy(15, 10, 3)))
    (g / "p-B_Cu.gbl").write_text(_gerber("Copper,L2,Bot", "D11*\n" + _xy(5, 5, 3)))
    assert main(["fill", str(g), "--min", "F.Cu=0.1"]) == 1
