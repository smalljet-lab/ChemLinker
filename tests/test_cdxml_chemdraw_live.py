"""Opt-in: opens a rendered route in the ChemDraw REALLY installed on this PC.

Skipped unless ``COMPOUND_REGISTRY_LIVE_CHEMDRAW=1`` is set. It starts a
hidden ChemDraw (a ChemDraw window that is open is not touched), opens the
CDXML of the EX0004_01 route from tests/data/reactions_example.json, asks for its
SMILES, saves a PNG picture and a re-saved CDXML next to it (in pytest's
temporary folder) and quits ChemDraw again; a second test checks that a
radical (TEMPO) is read without an added hydrogen. Run it by hand::

    $env:COMPOUND_REGISTRY_LIVE_CHEMDRAW = "1"
    .venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_cdxml_chemdraw_live.py

ChemDraw reads every row of the scheme by its geometry when it opens the
file, whatever the file's ``<scheme>`` says: everything right of an arrow up
to the next arrow would be that arrow's products. The writer therefore draws
the other reactants of a continued step over its arrow; ChemDraw keeps a
structure over an arrow as an object above that arrow (a reagent: the middle
part of its reaction SMILES "A>B>C"). The test checks ChemDraw's own reading
of every step: its reactants (left of the arrow, or over it) and its product
are the compounds of the route. Its SMILES export describes the first arrow
only.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from compound_registry import cdxml, chemdraw, routes
from test_routes import example_route

pytestmark = pytest.mark.skipif(os.environ.get("COMPOUND_REGISTRY_LIVE_CHEMDRAW") != "1",
                                reason="set COMPOUND_REGISTRY_LIVE_CHEMDRAW=1 to use the "
                                       "installed ChemDraw")

_SCRIPT = r"""
param([string]$ProgId, [string]$Path, [string]$Png, [string]$Resaved)
$ErrorActionPreference = 'Stop'
$ids = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'ChemDraw' } |
         ForEach-Object { $_.Id })
[Console]::Out.WriteLine('before:' + ($ids -join ','))
[Console]::Out.Flush()
$app = $null
try {
    $app = New-Object -ComObject $ProgId
    $doc = $app.Documents.Open($Path)
    $smiles = [string]$doc.Objects.Data('chemical/x-smiles')
    $doc.SaveAs($Png)
    $doc.SaveAs($Resaved)
    $doc.Close($false)
    [Console]::Out.WriteLine('smiles:' + $smiles)
}
catch { [Console]::Out.WriteLine('error:' + [string]$_.Exception.Message) }
finally {
    if ($null -ne $app) {
        try { $app.Quit() } catch { }
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch { }
    }
}
exit 0
"""


@pytest.fixture(autouse=True)
def _real_chemdraw(_no_real_chemdraw, monkeypatch):
    # Undo tests/conftest.py's switch (it runs first: this fixture depends on it).
    monkeypatch.delenv("COMPOUND_REGISTRY_NO_CHEMDRAW", raising=False)
    if not chemdraw.available():
        pytest.fail("COMPOUND_REGISTRY_LIVE_CHEMDRAW=1, but no ChemDraw was found on this PC.")


def _open_in_chemdraw(tmp_path, text: str) -> tuple[str, ET.Element]:
    """``(SMILES ChemDraw reports, the document as ChemDraw saves it)``."""
    path = tmp_path / "route.cdxml"
    path.write_text(text, encoding="utf-8")
    script = tmp_path / "open_route.ps1"
    script.write_text(_SCRIPT, encoding="utf-8")
    png, resaved = tmp_path / "route.png", tmp_path / "resaved.cdxml"
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-ProgId", chemdraw.find_prog_id(), "-Path", str(path),
            "-Png", str(png), "-Resaved", str(resaved)]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        output = done.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        before = re.search(r"^before:(.*)$", output, re.M)
        if before is not None:     # stop only the hidden ChemDraw this test started
            chemdraw._stop_leftovers([int(p) for p in before.group(1).split(",") if p.isdigit()])
        pytest.fail("ChemDraw did not answer within 2 minutes.")
    error = re.search(r"^error:(.*)$", output, re.M)
    assert error is None, error.group(1)
    smiles = re.search(r"^smiles:(.*)$", output, re.M)
    assert smiles is not None, output
    assert png.stat().st_size > 1000
    return smiles.group(1).strip(), ET.parse(resaved).getroot()


def _signature(atomic_numbers, bonds: int) -> tuple:
    return tuple(sorted(collections.Counter(atomic_numbers).items())), bonds


def _signature_of_fragment(fragment: ET.Element) -> tuple:
    return _signature([int(n.get("Element", "6")) for n in fragment.iter("n")],
                      len(fragment.findall("b")))


def _signature_of_molfile(molfile: str) -> tuple:
    mol = routes.read_molfile(molfile)
    return _signature([a.GetAtomicNum() for a in mol.GetAtoms()], mol.GetNumBonds())


def _chemdraw_steps(saved: ET.Element, names: dict) -> list[tuple[list, list, list]]:
    """ChemDraw's own reading of every step: ``(reactants left of the arrow,
    structures over it, products)``, each compound by its name."""
    by_id = {el.get("id"): el for el in saved.iter() if el.get("id") and el.tag == "fragment"}

    def compounds(step: ET.Element, key: str) -> list:
        return sorted(names.get(_signature_of_fragment(by_id[i]), "?")
                      for i in step.get(key, "").split() if i in by_id)

    return [(compounds(s, "ReactionStepReactants"), compounds(s, "ReactionStepObjectsAboveArrow"),
             compounds(s, "ReactionStepProducts"))
            for s in saved.iter("step")]


def test_ex0004_route_opens_as_a_reaction_scheme(tmp_path):
    route = example_route()
    text = cdxml.render_route(route)
    smiles, saved = _open_in_chemdraw(tmp_path, text)
    reactants, _, products = smiles.partition(">>")
    assert len(reactants.split(".")) == 2    # the bromide and the boronic acid of ABC-001
    assert "." not in products               # EX0001_01 alone
    ours = ET.fromstring(text)
    assert len(list(saved.iter("fragment"))) == len(list(ours.iter("fragment"))) == 9
    assert len(list(saved.iter("arrow"))) == 4
    steps = list(saved.iter("step"))
    assert len(steps) == 4
    assert {s.get("ReactionStepArrows") for s in steps} == \
        {s.get("ReactionStepArrows") for s in ours.iter("step")}
    # Every compound of the route, by the structure ChemDraw read.
    names = {}
    for rstep in route.steps:
        for p in (*rstep.reactants, rstep.product):
            names[_signature_of_molfile(p.molfile)] = p.label or p.name
    assert len(set(names.values())) == len(names) == 8
    bromide, boronic = "4-Bromobenzaldehyde", "Phenylboronic acid"
    amine, meli = "(S)-(-)-1-Phenylethylamine", "Methyllithium"
    read = _chemdraw_steps(saved, names)
    assert [sorted(left + over) for left, over, _ in read] == [
        sorted([bromide, boronic]), sorted(["EX0001_01", amine]),
        sorted(["EX0002_01", meli]), ["EX0003_01"]]
    assert [product for _, _, product in read] == \
        [["EX0001_01"], ["EX0002_01"], ["EX0003_01"], ["EX0004_01"]]
    # Where ChemDraw put them: the reactant of a continued step left of its
    # arrow (the product drawn last), its other reactants over the arrow.
    assert read[1][:2] == (["EX0001_01"], [amine])
    for (left, over, _), drawn in zip(read, _drawn_steps(ours)):
        assert (len(left), len(over)) == drawn


def _drawn_steps(root: ET.Element) -> list[tuple[int, int]]:
    """Per step of our file: ``(reactant fragments left of the arrow, those
    drawn over it)``."""
    frags = {el.get("id") for el in root.iter("fragment")}
    out = []
    for s in root.iter("step"):
        over = set(s.get("ReactionStepObjectsAboveArrow", "").split())
        reactants = [i for i in s.get("ReactionStepReactants", "").split() if i in frags]
        out.append((sum(1 for i in reactants if i not in over),
                    sum(1 for i in reactants if i in over)))
    return out


def test_radical_is_read_without_an_added_hydrogen(tmp_path):
    from rdkit import Chem

    from compound_registry.route_model import Participant, Route, Step
    from test_routes import molfile_of

    product = Participant("cs:P", molfile_of("C1CC1"), None, "P1", "P1")
    tempo = Participant("ik:t", molfile_of("CC1(C)CCCC(C)(C)N1[O]"), "TEMPO", None, None)
    route = Route(product, (Step(product, (tempo,), (), (), ()),))
    smiles, _ = _open_in_chemdraw(tmp_path, cdxml.render_route(route))
    reactant = Chem.MolFromSmiles(smiles.split(">>")[0])
    expected = Chem.MolFromSmiles("CC1(C)CCCC(C)(C)N1[O]")       # N-O radical, not N-OH
    assert Chem.MolToSmiles(reactant) == Chem.MolToSmiles(expected)
