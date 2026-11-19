"""Tests for compound_registry.render (SVG thumbnails)."""

from __future__ import annotations

import re

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor

from compound_registry.render import placeholder_svg, svg_thumbnail


def molfile(smiles: str, v3000: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToV3KMolBlock(mol) if v3000 else Chem.MolToMolBlock(mol)


def mirrored(mb: str) -> str:
    """Same V2000 file with x -> -x and wedges swapped (same molecule, other drawing)."""
    lines = mb.splitlines()
    n_atoms, n_bonds = int(lines[3][0:3]), int(lines[3][3:6])
    for i in range(4, 4 + n_atoms):
        x = float(lines[i][0:10])
        lines[i] = f"{-x:10.4f}" + lines[i][10:]
    for i in range(4 + n_atoms, 4 + n_atoms + n_bonds):
        flag = int(lines[i][9:12])
        if flag in (1, 6):
            lines[i] = lines[i][:9] + f"{7 - flag:3d}" + lines[i][12:]
    return "\n".join(lines) + "\n"


def assert_plain_svg(svg: str) -> None:
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    low = svg.lower()
    assert "<?xml" not in low
    assert "<script" not in low
    assert "href" not in low
    assert "<foreignobject" not in low


def test_normal_molblock_gives_svg():
    svg = svg_thumbnail(molfile("C[C@H](N)c1ccccc1"))
    assert_plain_svg(svg)
    assert "no preview" not in svg
    assert "width='240px'" in svg and "height='160px'" in svg


def test_custom_size():
    svg = svg_thumbnail(molfile("CCO"), 300, 200)
    assert "width='300px'" in svg and "height='200px'" in svg


def test_cip_labels_are_drawn():
    svg = svg_thumbnail(molfile("C/C=C/C[C@H](C)O"))
    assert "CIP_Code" in svg  # RDKit's class for R/S and E/Z annotations


def test_enhanced_stereo_group_labels_are_drawn():
    svg = svg_thumbnail(molfile("C[C@H](N)C[C@H](C)O |o1:1,4|", v3000=True))
    assert_plain_svg(svg)
    assert "stereoGroup" in svg


def test_orientation_of_the_drawing_is_kept():
    mb = molfile("C[C@H](N)c1ccc(Cl)cc1")
    a, b = svg_thumbnail(mb), svg_thumbnail(mirrored(mb))
    assert_plain_svg(a)
    assert_plain_svg(b)
    assert a != b  # RDKit did not lay the molecule out again


def test_salt_with_two_fragments():
    assert_plain_svg(svg_thumbnail(molfile("C[C@H](N)c1ccccc1.Cl")))


def test_3d_molfile_is_drawn_in_2d():
    mol = Chem.AddHs(Chem.MolFromSmiles("C[C@H](N)c1ccccc1"))
    AllChem.EmbedMolecule(mol, randomSeed=42)
    svg = svg_thumbnail(Chem.MolToMolBlock(mol))
    assert_plain_svg(svg)
    assert "no preview" not in svg


@pytest.mark.parametrize("bad", ["", "   ", "garbage\nnot a molfile\n", "M  END", None, 42,
                                 "$RXN\n\n\n\n"])
def test_garbage_gives_placeholder(bad):
    svg = svg_thumbnail(bad)  # type: ignore[arg-type]
    assert_plain_svg(svg)
    assert "no preview" in svg


def test_invalid_size_falls_back_to_default():
    svg = svg_thumbnail(molfile("CCO"), width=-5, height="x")  # type: ignore[arg-type]
    assert "width='240px'" in svg and "height='160px'" in svg


def test_placeholder_is_small_and_plain():
    svg = placeholder_svg(100, 80)
    assert_plain_svg(svg)
    assert len(svg) < 1000
    assert re.search(r"viewBox='0 0 100 80'", svg)
