"""The real recognition (RxnScribe + MolScribe + EasyOCR) on drawn pictures.

Skipped unless asked for: it needs the optional recognition part installed
(``compound-registry install-recognition``), about 2.5 GB of memory and half a
minute to load::

    $env:COMPOUND_REGISTRY_LIVE_RECOGNITION = "1"
    .venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_recognition_live.py

``COMPOUND_REGISTRY_RECOGNITION_HOME`` names another installation than the
usual one. The pictures are drawn here with RDKit from invented, simple
molecules; nothing is read from anywhere else.
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from rdkit import Chem
from rdkit.Chem import Draw

from compound_registry import protocol, recognition

pytestmark = pytest.mark.skipif(
    not os.environ.get("COMPOUND_REGISTRY_LIVE_RECOGNITION"),
    reason="set COMPOUND_REGISTRY_LIVE_RECOGNITION=1 to run the real recognition")


def picture(smiles: str, size: int = 400) -> str:
    image = Draw.MolToImage(Chem.MolFromSmiles(smiles), size=(size, size))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture(scope="module")
def service():
    state = recognition.install_state()
    if not state["installed"]:
        pytest.skip(f"the recognition part is not installed in {state['home']}: "
                    f"{state['problem']}")
    service = recognition.Recognition()
    yield service
    service.shutdown()


def test_the_worker_loads_and_reads_one_molecule(service):
    assert service.status(start=True)["state"] in ("loading", "ready")
    result = service.recognize(picture("CC(=O)Oc1ccccc1C(=O)O"), "molecule",
                               timeout=float(protocol.RECOGNIZE_TIMEOUT_S))
    assert service.status()["state"] == "ready"
    assert (result["width"], result["height"]) == (400, 400)
    assert result["reactions"] == [] and len(result["molecules"]) == 1
    molecule = result["molecules"][0]
    assert molecule["smiles"] == "CC(=O)Oc1ccccc1C(=O)O"
    assert molecule["box"] == [0, 0, 400, 400] and 0 < molecule["confidence"] <= 1
    assert Chem.MolFromMolBlock(molecule["molfile"]) is not None


def test_a_reaction_picture_gives_a_result_of_the_agreed_shape(service):
    # One molecule is no reaction scheme; the point is the shape of the answer
    # of the reaction mode, whatever the model finds.
    result = service.recognize(picture("OCc1ccccc1", 480), "reaction",
                               timeout=float(protocol.RECOGNIZE_TIMEOUT_S))
    assert set(result) == {"width", "height", "molecules", "reactions", "unassigned", "seconds",
                           "warnings"}
    assert set(result["warnings"]) <= set(protocol.RECOGNITION_WARNING_CODES)
    for molecule in result["molecules"]:
        assert set(molecule["warnings"]) <= set(protocol.MOLECULE_WARNING_CODES)
