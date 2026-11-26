"""The real recognition (RxnScribe + MolScribe + EasyOCR) on drawn pictures.

Skipped unless asked for: it needs the optional recognition part installed
(``compound-registry install-recognition``), about 2.5 GB of memory and half a
minute to load::

    $env:COMPOUND_REGISTRY_LIVE_RECOGNITION = "1"
    .venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_recognition_live.py

``COMPOUND_REGISTRY_RECOGNITION_HOME`` names another installation than the
usual one. The pictures are drawn here with RDKit from invented, simple
molecules. Two tests also use a crop of a real scheme kept with the local
development aids (``tools/``, not in the repository) when it is there.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
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


#: A crop of a scheme in which RxnScribe reads the coefficient "1" before the
#: product as a label and misses the product (tools/recognition-e2e/missed).
#: Local development data, not in the repository: the test is skipped without it.
USER_CROP = Path(__file__).resolve().parents[1] / "tools" / "recognition-e2e" / "missed" / "verify" / "pics" / "vf_clean.png"
#: The product of that scheme, built by RDKit from its reactants (no stereo).
USER_PRODUCT = ("CC(C)(C)c1cc(-c2ccccc2C(N=CC=NC(c2ccccc2-c2cc(C(C)(C)C)cc(C(C)(C)C)c2)C(C)(C)C)"
                "C(C)(C)C)cc(C(C)(C)C)c1")


def _flat_key(smiles: str | None) -> str | None:
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None:
        return None
    Chem.RemoveStereochemistry(mol)
    return Chem.MolToInchiKey(mol)


def test_a_product_behind_a_coefficient_is_found_as_the_product(service):
    if not USER_CROP.is_file():
        pytest.skip("the crop of the scheme is not on this PC")
    image = base64.b64encode(USER_CROP.read_bytes()).decode("ascii")
    result = service.recognize(image, "reaction", timeout=float(protocol.RECOGNIZE_TIMEOUT_S))
    by_n = {m["n"]: m for m in result["molecules"]}
    (reaction,) = result["reactions"]
    products = [by_n[n]["smiles"] for n in reaction["products"]]
    assert [_flat_key(s) for s in products] == [_flat_key(USER_PRODUCT)]
    assert len(reaction["reactants"]) == 2 and reaction["conditions"]
    assert result["unassigned"] == [] and result["warnings"] == []


def test_the_peek_decoding_is_the_decoding_of_rxnscribe(tmp_path):
    """The re-implemented decoding against RxnScribe's own ``predict_images``
    (worker functions under the recognition environment's Python)."""
    state = recognition.install_state()
    if not state["installed"]:
        pytest.skip(f"the recognition part is not installed in {state['home']}")
    home = Path(state["home"])
    pictures = []
    scheme = Image.new("RGB", (1000, 320), "white")
    scheme.paste(Draw.MolToImage(Chem.MolFromSmiles("OC(=O)c1ccccc1"), size=(300, 300)), (10, 10))
    scheme.paste(Draw.MolToImage(Chem.MolFromSmiles("COC(=O)c1ccccc1"), size=(300, 300)), (680, 10))
    pen = ImageDraw.Draw(scheme)
    pen.line([(340, 160), (640, 160)], fill="black", width=3)
    pen.polygon([(640, 150), (665, 160), (640, 170)], fill="black")
    pen.text((420, 130), "MeOH, H2SO4", fill="black")
    pictures.append(tmp_path / "scheme.png")
    scheme.save(pictures[-1])
    if USER_CROP.is_file():
        pictures.append(USER_CROP)
    script = Path(__file__).parent / "data" / "peek_matches_predict_images.py"
    done = subprocess.run([str(recognition.env_python(home)), "-I", str(script), str(recognition.worker_script()),
                           str(recognition.models_dir(home))] + [str(p) for p in pictures],
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stderr[-2000:]
    rows = json.loads(done.stdout.strip().splitlines()[-1])
    assert len(rows) == len(pictures)
    for row in rows:
        assert row["same"] and row["holds"] and "peek" not in row["info"], row
    if USER_CROP.is_file():
        assert rows[-1]["triggers"] == 1 and rows[-1]["added"] == 1


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
