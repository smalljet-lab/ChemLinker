"""``reaction_to_chemdraw``: the ChemDraw document of a captured reaction
(``cdxml.render_reaction``) and the file ``recognition.reaction_to_chemdraw``
writes. ChemDraw is never started: opening is switched off for every test
(tests/conftest.py) or replaced by a fake opener. Invented molecules only."""

from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from compound_registry import cdxml, recognition
from compound_registry.cdxml import ReactionStep
from compound_registry.errors import RegistryError
from test_cdxml import Doc


def mf(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    AllChem.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


ALDEHYDE, ALCOHOL, CHLORIDE = mf("O=Cc1ccccc1"), mf("OCc1ccccc1"), mf("ClCc1ccccc1")
BROMIDE, BORONIC, BIARYL = mf("Brc1ccccc1"), mf("OB(O)c1ccccc1"), mf("c1ccc(-c2ccccc2)cc1")
METHANOL = mf("CO")


def render(*steps: ReactionStep) -> Doc:
    return Doc(cdxml.render_reaction(list(steps)))


def elements(fragment) -> list[str]:
    """Element numbers of the hetero atoms of a drawn fragment."""
    return sorted(n.attrib["Element"] for n in fragment.iter("n") if "Element" in n.attrib)


def centre_y(doc: Doc, element_id: str) -> float:
    box = doc.box(doc.by_id[element_id])
    return (box[1] + box[3]) / 2


def test_one_step_with_two_reactants_and_conditions():
    doc = render(ReactionStep((BROMIDE, BORONIC), (), (BIARYL,),
                              ("Pd(PPh3)4, K2CO3", "dioxane, 80 °C", "12 h")))
    assert len(doc.steps) == 1
    step = doc.steps[0]
    reactants = doc.fragments(step["ReactionStepReactants"])
    products = doc.fragments(step["ReactionStepProducts"])
    assert [elements(f) for f in reactants] == [["35"], ["5", "8", "8"]]
    assert [len(list(f.iter("n"))) for f in products] == [12]
    assert doc.by_id[step["ReactionStepArrows"][0]].tag == "arrow"
    assert doc.texts(step["ReactionStepObjectsAboveArrow"]) == ["Pd(PPh3)4, K2CO3"]
    assert doc.texts(step["ReactionStepObjectsBelowArrow"]) == ["dioxane, 80 °C", "12 h"]
    plus = [el for el in doc.page if el.tag == "t" and doc.text_of(el) == "+"]
    assert len(plus) == 1
    # Left to right: reactant, "+", reactant, arrow, product.
    order = [doc.box(el)[0] for el in (reactants[0], plus[0], reactants[1],
                                       doc.by_id[step["ReactionStepArrows"][0]], products[0])]
    assert order == sorted(order)
    # The digits of a formula are subscripts.
    above = doc.by_id[step["ReactionStepObjectsAboveArrow"][0]]
    assert len(list(above.iter("s"))) > 1


def test_agents_are_drawn_over_the_arrow_and_the_text_goes_below():
    doc = render(ReactionStep((ALDEHYDE,), (METHANOL,), (ALCOHOL,), ("NaBH4", "0 °C")))
    step = doc.steps[0]
    over = step["ReactionStepObjectsAboveArrow"]
    assert [doc.by_id[i].tag for i in over] == ["fragment"]
    assert elements(doc.by_id[over[0]]) == ["8"]
    assert doc.texts(step["ReactionStepObjectsBelowArrow"]) == ["NaBH4", "0 °C"]
    arrow = doc.box(doc.by_id[step["ReactionStepArrows"][0]])
    agent = doc.box(doc.by_id[over[0]])
    assert arrow[0] <= agent[0] and agent[2] <= arrow[2] and agent[3] < arrow[1]
    assert len(doc.fragments(step["ReactionStepReactants"])) == 1


def test_consecutive_steps_continue_the_row_from_the_product_drawn():
    doc = render(ReactionStep((ALDEHYDE,), (), (ALCOHOL,), ("NaBH4",)),
                 ReactionStep((ALCOHOL,), (), (CHLORIDE,), ("SOCl2",)))
    first, second = doc.steps
    assert second["ReactionStepReactants"] == first["ReactionStepProducts"]
    assert len([el for el in doc.page if el.tag == "fragment"]) == 3
    assert len([el for el in doc.page if el.tag == "arrow"]) == 2
    arrows = [centre_y(doc, s["ReactionStepArrows"][0]) for s in doc.steps]
    assert arrows[0] == pytest.approx(arrows[1])              # one row


def test_a_step_with_another_reactant_starts_a_new_row():
    doc = render(ReactionStep((ALDEHYDE,), (), (ALCOHOL,)),
                 ReactionStep((ALCOHOL, BROMIDE), (), (CHLORIDE,)))
    first, second = doc.steps
    assert not set(second["ReactionStepReactants"]) & set(first["ReactionStepProducts"])
    assert len(doc.fragments(second["ReactionStepReactants"])) == 2
    assert len([el for el in doc.page if el.tag == "fragment"]) == 5
    arrows = [centre_y(doc, s["ReactionStepArrows"][0]) for s in doc.steps]
    assert arrows[1] > arrows[0] + 20                          # the row below


def test_two_products_and_no_continuation_after_them():
    doc = render(ReactionStep((ALDEHYDE,), (), (ALCOHOL, CHLORIDE)),
                 ReactionStep((ALCOHOL,), (), (BIARYL,)))
    first, second = doc.steps
    assert len(doc.fragments(first["ReactionStepProducts"])) == 2
    assert not set(second["ReactionStepReactants"]) & set(first["ReactionStepProducts"])


def test_a_step_may_lack_reactants_or_products():
    doc = render(ReactionStep((), (), (ALCOHOL,), ("from the shelf",)),
                 ReactionStep((ALDEHYDE,), (), (), ()))
    assert "ReactionStepReactants" not in doc.steps[0]
    assert "ReactionStepProducts" not in doc.steps[1]
    assert len([el for el in doc.page if el.tag == "arrow"]) == 2


def test_an_unreadable_molfile_is_written_as_a_question_mark():
    doc = render(ReactionStep(("this is no molfile",), (), (ALCOHOL,)))
    reactant = doc.by_id[doc.steps[0]["ReactionStepReactants"][0]]
    assert reactant.tag == "t" and doc.text_of(reactant) == "?"


def test_many_steps_wrap_onto_rows_inside_the_page():
    chain = [mf("C" * n + "O") for n in range(2, 15)]
    steps = [ReactionStep((a,), (), (b,), (f"step {i}",))
             for i, (a, b) in enumerate(zip(chain, chain[1:]), start=1)]
    assert len(steps) == 12
    doc = render(*steps)
    assert len(doc.steps) == 12
    width = float(doc.page.attrib["Width"])
    for el in doc.drawn():
        box = doc.box(el)
        assert 0 <= box[0] and box[2] <= width
    rows = {round(centre_y(doc, s["ReactionStepArrows"][0])) for s in doc.steps}
    assert len(rows) > 1


def test_the_document_is_deterministic_and_the_route_writer_is_unchanged():
    step = ReactionStep((ALDEHYDE,), (METHANOL,), (ALCOHOL,), ("NaBH4",))
    assert cdxml.render_reaction([step]) == cdxml.render_reaction((step,))


# --------------------------------------------------------------------------- the file

STEPS = [{"reactants": [ALDEHYDE], "agents": [], "products": [ALCOHOL], "conditions": ["NaBH4"]}]
NOW = datetime(2026, 11, 24, 15, 4, 5)


def test_the_file_is_written_new_every_time_and_not_opened_in_tests(tmp_path):
    first = recognition.reaction_to_chemdraw(STEPS, folder=tmp_path, now=lambda: NOW)
    second = recognition.reaction_to_chemdraw(STEPS, folder=tmp_path, now=lambda: NOW)
    assert Path(first["path"]) == tmp_path / "reaction_2026-11-24_150405.cdxml"
    assert Path(second["path"]) == tmp_path / "reaction_2026-11-24_150405_2.cdxml"
    assert first["opened"] is False and "COMPOUND_REGISTRY_NO_OPEN" in first["message"]
    doc = Doc(Path(first["path"]).read_text(encoding="utf-8"))
    assert len(doc.steps) == 1 and doc.texts(doc.steps[0]["ReactionStepObjectsAboveArrow"]) == ["NaBH4"]
    assert Path(second["path"]).read_bytes() == Path(first["path"]).read_bytes()


def test_the_default_folder_is_in_the_users_temporary_folder():
    assert recognition.chemdraw_folder() == Path(tempfile.gettempdir()) / "ChemLinker"


def test_the_file_is_opened_through_the_opener(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOUND_REGISTRY_NO_OPEN")
    opened: list[str] = []
    result = recognition.reaction_to_chemdraw(STEPS, folder=tmp_path / "new", opener=opened.append)
    assert result == {"path": opened[0], "opened": True}
    assert re.fullmatch(r"reaction_\d{4}-\d\d-\d\d_\d{6}\.cdxml", Path(opened[0]).name)

    def refuse(path: str) -> None:
        raise OSError("No application is associated with the specified file")

    result = recognition.reaction_to_chemdraw(STEPS, folder=tmp_path, opener=refuse)
    assert result["opened"] is False and Path(result["path"]).is_file()
    assert "No application is associated" in result["message"] and "ChemDraw" in result["message"]


def test_unreadable_structures_are_counted_in_the_message(tmp_path):
    steps = [{"reactants": ["junk", ALDEHYDE], "agents": ["more junk"], "products": [ALCOHOL],
              "conditions": []}]
    result = recognition.reaction_to_chemdraw(steps, folder=tmp_path, opener=lambda path: None)
    assert "2 structure(s) could not be read" in result["message"]


def test_a_folder_that_cannot_be_used_is_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(RegistryError) as info:
        recognition.reaction_to_chemdraw(STEPS, folder=blocker / "sub")
    assert info.value.code == "storage_error" and not list(tmp_path.glob("**/*.cdxml"))


# --------------------------------------------------------------------------- review fixes

def parsed(*steps: ReactionStep):
    import xml.etree.ElementTree as ET

    text = cdxml.render_reaction(list(steps))
    return text, ET.fromstring(text)


def read_back(text: str) -> list[str]:
    """The structures RDKit reads from the document (it takes E/Z from the
    coordinates unless the bond says otherwise, like ChemDraw)."""
    return sorted(Chem.MolToSmiles(mol) for mol in Chem.MolsFromCDXML(text))


@pytest.mark.parametrize("smiles", ["CC=CC", "N#CC=Cc1ccccc1", "CC=NO"])
def test_a_double_bond_of_unknown_geometry_stays_unknown_in_the_document(smiles):
    """The molfile marks it (bond stereo 3). Written as a plain double bond on
    definite coordinates it would become a definite E or Z in ChemDraw."""
    block = mf(smiles)
    assert re.search(r"^\s+\d+\s+\d+\s+2\s+3\s*$", block, re.M)
    text, root = parsed(ReactionStep((block,), (), (METHANOL,), ()))
    wavy = [b for b in root.iter("b") if b.get("Display") == "Wavy"]
    assert len(wavy) == 1 and wavy[0].get("Order") == "2"
    assert read_back(text) == sorted([smiles, "CO"])


def test_a_double_bond_with_a_geometry_keeps_it_in_the_document():
    for smiles in ("C/C=C/C", r"C/C=C\C"):
        text, root = parsed(ReactionStep((mf(smiles),), (), (METHANOL,), ()))
        assert not [b for b in root.iter("b") if b.get("Display") == "Wavy"]
        assert read_back(text) == sorted([Chem.MolToSmiles(Chem.MolFromSmiles(smiles)), "CO"])
    # A double bond without possible geometry gets no mark.
    text, root = parsed(ReactionStep((mf("CC(C)=CC(=O)O"),), (), (METHANOL,), ()))
    assert not [b for b in root.iter("b") if b.get("Display") == "Wavy"]


def test_numbered_r_groups_keep_their_number():
    """"[1*]" is how the recognition writes R1. Its number must be in the
    label; two groups must not both read "R"."""
    block = recognition._read_molecule("[1*]c1ccc(Cc2ccc([2*])cc2)cc1", None)["molfile"]
    text, root = parsed(ReactionStep((block,), (), (mf("*c1ccccc1"),), ()))
    groups = [n for n in root.iter("n") if n.get("NodeType") == "GenericNickname"]
    assert sorted(n.get("GenericNickname") for n in groups) == ["R", "R1", "R2"]
    assert sorted("".join(n.itertext()) for n in groups) == ["R", "R1", "R2"]
    assert not [n for n in groups if n.get("Isotope")]
    # A real isotope label is still written.
    text, root = parsed(ReactionStep((mf("[13CH3]O"),), (), (METHANOL,), ()))
    assert [n.get("Isotope") for n in root.iter("n") if n.get("Isotope")] == ["13"]


def arrow_width(root) -> float:
    x0, _, x1, _ = (float(v) for v in next(root.iter("arrow")).get("BoundingBox").split())
    return x1 - x0


def condition_lines(root) -> list[str]:
    return ["".join(t.itertext()) for t in root.iter("t")
            if t.get("InterpretChemically") == "no" and "".join(t.itertext()) != "+"]


def test_a_long_condition_line_without_commas_is_wrapped_at_its_spaces():
    """Text read from a picture or typed by the user often has no ", "."""
    line = "Pd(PPh3)4 (5 mol%) K2CO3 (2 equiv) dioxane/H2O 4:1 80 C 12 h under N2"
    text, root = parsed(ReactionStep((ALDEHYDE,), (), (ALCOHOL,), (line,)))
    lines = condition_lines(root)
    assert len(lines) > 1 and " ".join(lines) == line
    assert all(cdxml.text_width(piece) <= cdxml.MAX_LINE_WIDTH for piece in lines)
    assert arrow_width(root) <= cdxml.MAX_LINE_WIDTH + 40

    longest = " ".join(["stirred"] * 62)[:500]                     # the longest line allowed
    text, root = parsed(ReactionStep((ALDEHYDE,), (), (ALCOHOL,), (longest,)))
    assert arrow_width(root) <= cdxml.MAX_LINE_WIDTH + 40
    assert float(root.find("page").get("BoundingBox").split()[2]) == cdxml.SLIDE_WIDTH

    word = "x" * 500                                               # no space at all: cut
    text, root = parsed(ReactionStep((ALDEHYDE,), (), (ALCOHOL,), (word,)))
    assert "".join(condition_lines(root)) == word
    assert arrow_width(root) <= cdxml.MAX_LINE_WIDTH + 40


def test_a_line_with_commas_is_wrapped_as_before():
    line = ", ".join(["reagent number %d" % i for i in range(1, 9)])
    wrapped = [t.text for t in cdxml._wrap(cdxml.Text(line, True))]
    assert all(piece.endswith(",") for piece in wrapped[:-1])
    assert " ".join(wrapped) == line
    short = cdxml.Text("NaBH4, MeOH", True)
    assert cdxml._wrap(short) == [short]
