"""Opt-in: names one structure with the ChemDraw REALLY installed on this PC.

Skipped unless ``COMPOUND_REGISTRY_LIVE_CHEMDRAW=1`` is set. It starts a
hidden ChemDraw (a ChemDraw window that is open is not touched), so it is
slow and needs a working ChemDraw licence. Run it by hand::

    $env:COMPOUND_REGISTRY_LIVE_CHEMDRAW = "1"
    .venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_chemdraw_live.py
"""

from __future__ import annotations

import os

import pytest

from compound_registry import chem, chemdraw, naming

pytestmark = pytest.mark.skipif(os.environ.get("COMPOUND_REGISTRY_LIVE_CHEMDRAW") != "1",
                                reason="set COMPOUND_REGISTRY_LIVE_CHEMDRAW=1 to use the "
                                       "installed ChemDraw")

CAFFEINE = "Cn1cnc2c1c(=O)n(C)c(=O)n2C"
CAFFEINE_NAME = "1,3,7-trimethyl-3,7-dihydro-1H-purine-2,6-dione"


@pytest.fixture(autouse=True)
def _real_chemdraw(_no_real_chemdraw, monkeypatch):
    # Undo tests/conftest.py's switch (it runs first: this fixture depends on it).
    monkeypatch.delenv("COMPOUND_REGISTRY_NO_CHEMDRAW", raising=False)
    if not chemdraw.available():
        pytest.fail("COMPOUND_REGISTRY_LIVE_CHEMDRAW=1, but no ChemDraw was found on this PC.")


def test_caffeine_is_named_by_the_installed_chemdraw():
    norm = chem.normalize_smiles(CAFFEINE)
    outcome = naming.generate(norm.molblock, norm.stereo_key)
    assert outcome.code == "ok", outcome.message
    assert outcome.name == CAFFEINE_NAME
