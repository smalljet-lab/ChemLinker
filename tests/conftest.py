"""Shared fixtures for every test."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: Set for every test: the ChemDraw installed on a developer's PC must never be
#: started by a test (``chemdraw.find_prog_id`` then reports "not installed").
#: Tests inject fake runners / askers instead; tests/test_chemdraw_live.py is
#: the only (opt-in) exception and removes it again.
NO_CHEMDRAW_ENV = "COMPOUND_REGISTRY_NO_CHEMDRAW"


#: Set for every test: a file written by ``route`` (a ChemDraw document) must
#: never be opened on a developer's PC. Tests that check opening remove it and
#: inject a fake opener.
NO_OPEN_ENV = "COMPOUND_REGISTRY_NO_OPEN"


@pytest.fixture(autouse=True)
def _no_real_chemdraw(monkeypatch):
    monkeypatch.setenv(NO_CHEMDRAW_ENV, "1")


@pytest.fixture(autouse=True)
def _no_file_opening(monkeypatch):
    monkeypatch.setenv(NO_OPEN_ENV, "1")


# --------------------------------------------------------------------------- the example registry

#: Invented data in the shape of what the ELN and a registry hold (see its
#: ``_about``): the reactions of ABC-001..ABC-008, a four-step synthesis, and
#: the 7 batches made in them.
EXAMPLE_DATA_FILE = Path(__file__).parent / "data" / "reactions_example.json"


def example_data() -> dict:
    """A fresh copy of tests/data/reactions_example.json."""
    return json.loads(EXAMPLE_DATA_FILE.read_text(encoding="utf-8"))


def build_example_registry(home: Path, *, reactions: bool = True) -> Path:
    """A new registry in ``home`` with the ID format of the example data,
    holding its 7 batches (registered in list order, so they get the IDs the
    file lists) with their yields, and (``reactions``) the reactions of the 6
    experiments."""
    from compound_registry.cli import main
    from compound_registry.service import RegistryService

    data = example_data()
    assert main(["--home", str(home), "--json", "init", "--id-format", data["id_format"]]) == 0
    with RegistryService.open(home) as svc:
        for b in data["batches"]:
            r = svc.register(molfile=b["molfile"], experiment_ref=b["experiment_ref"],
                             experiment_code=b["experiment_code"],
                             experiment_url=b["experiment_url"],
                             product_index=b["product_index"], source="extension")
            assert r["id"] == b["full_id"], (r["id"], b["full_id"])
            svc.set_yield(r["id"], b["experiment_ref"], b["product_index"], b["isolated"],
                          b["analytical"])
        if reactions:
            for e in data["experiments"].values():
                svc.set_reaction(e["experiment_ref"], e["reaction"], e["experiment_code"],
                                 e["experiment_url"])
    return home


@pytest.fixture
def example_home(tmp_path, capsys) -> Path:
    """``build_example_registry`` in ``tmp_path / "example"`` (output of 'init' discarded)."""
    home = build_example_registry(tmp_path / "example")
    capsys.readouterr()
    return home
