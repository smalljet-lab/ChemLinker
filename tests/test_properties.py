"""Display-only properties (compound_registry.properties): formula, masses, composition."""

from __future__ import annotations

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry.properties import (
    compute_properties,
    formula_counts,
    parse_formula,
    read_molblock,
)


def molfile(smiles: str, *, explicit_h: bool = False, v3000: bool = False) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if explicit_h:
        mol = Chem.AddHs(mol)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToV3KMolBlock(mol) if v3000 else Chem.MolToMolBlock(mol)


def composition(props: dict) -> dict[str, float]:
    return {c["element"]: c["percent"] for c in props["composition"]}


def test_ethanol():
    p = compute_properties(molfile("CCO"))
    assert p == {
        "sum_formula": "C2H6O",
        "mol_weight": 46.069,
        "exact_mass": 46.04186,
        "composition": [{"element": "C", "percent": 52.14}, {"element": "H", "percent": 13.13},
                        {"element": "O", "percent": 34.73}],
    }


def test_hydrochloride_counts_the_counter_ion():
    p = compute_properties(molfile("CCN.Cl"))
    assert p["sum_formula"] == "C2H8ClN"
    assert p["mol_weight"] == 81.546 and p["exact_mass"] == 81.03453
    assert [c["element"] for c in p["composition"]] == ["C", "H", "Cl", "N"]  # Hill order
    assert composition(p)["Cl"] == 43.48
    # drawn as an ion pair: the same values
    assert compute_properties(molfile("CC[NH3+].[Cl-]")) == p


@pytest.mark.parametrize("smiles", ["CCO", "CCN.Cl", "c1ccccc1C(=O)O", "CC(=O)[O-].[Na+]",
                                    "[2H]C([2H])([2H])O", "O=S(=O)([O-])[O-].[Cu+2]",
                                    "C[C@H](N)c1ccccc1.OC(=O)C(F)(F)F"])
def test_composition_sums_to_100(smiles):
    p = compute_properties(molfile(smiles))
    assert abs(sum(c["percent"] for c in p["composition"]) - 100) < 0.05
    assert all(c["percent"] > 0 for c in p["composition"])
    assert [c["element"] for c in p["composition"]] == list(
        dict.fromkeys(e for e in _symbols(p["sum_formula"])))


def _symbols(formula: str) -> list[str]:
    import re

    return re.findall(r"[A-Z][a-z]?", formula)


def test_explicit_and_implicit_hydrogens_give_the_same_values():
    assert compute_properties(molfile("CCO", explicit_h=True)) == compute_properties(molfile("CCO"))


def test_v3000_is_read():
    assert compute_properties(molfile("CCO", v3000=True))["sum_formula"] == "C2H6O"


def test_isotopes_use_their_own_mass():
    plain = compute_properties(molfile("CO"))
    deut = compute_properties(molfile("[2H]C([2H])([2H])O"))
    assert deut["sum_formula"] == "CH4O" == plain["sum_formula"]   # counted as H in the formula
    assert deut["mol_weight"] == 35.06 and plain["mol_weight"] == 32.042
    assert deut["exact_mass"] == 35.04504
    assert composition(deut)["H"] == pytest.approx(20.11, abs=0.01)
    assert composition(deut)["H"] > composition(plain)["H"]


def test_net_charge_is_shown_like_calcmolformula():
    assert compute_properties(molfile("C[N+](C)(C)C"))["sum_formula"] == "C4H12N+"
    assert compute_properties(molfile("CC(=O)[O-]"))["sum_formula"] == "C2H3O2-"
    assert compute_properties(molfile("CC(=O)[O-].[Na+]"))["sum_formula"] == "C2H3NaO2"


def test_rounding():
    p = compute_properties(molfile("c1ccccc1C(=O)O"))
    assert p["mol_weight"] == round(p["mol_weight"], 3)
    assert p["exact_mass"] == round(p["exact_mass"], 5)
    assert all(c["percent"] == round(c["percent"], 2) for c in p["composition"])


@pytest.mark.parametrize("bad", [None, "", "   ", "not a molfile", 42,
                                 "\n  x\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"])
def test_unreadable_or_empty_gives_none(bad):
    assert compute_properties(bad) is None


def test_placeholder_atom_gives_none():
    assert compute_properties(molfile("*CCO")) is None


def test_impossible_valence_gives_none_quietly(capfd):
    lines = molfile("C(C)(C)(C)C").split("\n")
    lines[4] = lines[4][:31] + "F  " + lines[4][34:]   # the central atom: a fluorine with 4 bonds
    assert compute_properties("\n".join(lines)) is None
    captured = capfd.readouterr()
    assert captured.err == "" and captured.out == ""


def test_header_lines_dropped_still_reads():
    mb = molfile("CCO")
    headless = "\n".join(mb.split("\n")[3:])
    assert compute_properties(headless)["sum_formula"] == "C2H6O"
    assert read_molblock(headless) is not None


# --------------------------------------------------------------------------- formula text

@pytest.mark.parametrize("text, expected", [
    ("C2H6O", ({"C": 2, "H": 6, "O": 1}, 0)),
    (" C2 H6 O ", ({"C": 2, "H": 6, "O": 1}, 0)),
    ("CH3CH2OH", ({"C": 2, "H": 6, "O": 1}, 0)),
    ("C2H7N.ClH", ({"C": 2, "H": 8, "N": 1, "Cl": 1}, 0)),
    ("C10H12N2.2ClH", ({"C": 10, "H": 14, "N": 2, "Cl": 2}, 0)),
    ("C4H12N+", ({"C": 4, "H": 12, "N": 1}, 1)),
    ("Fe+3", ({"Fe": 1}, 3)),
    ("O4S--", ({"O": 4, "S": 1}, -2)),
    ("C2H3O2-", ({"C": 2, "H": 3, "O": 2}, -1)),
])
def test_parse_formula(text, expected):
    assert parse_formula(text) == expected


@pytest.mark.parametrize("text", ["", "ethanol", "ABC-008", "+", "000042", "C2H6O.",
                                  "c2h6o", "Xx2", "C0", "C2H6O+-", None, 5])
def test_parse_formula_refuses(text):
    assert parse_formula(text) is None


def test_formula_counts_ignores_charge():
    assert formula_counts("C2H7N.ClH") == {"C": 2, "H": 8, "N": 1, "Cl": 1}
    assert formula_counts("nonsense") is None
