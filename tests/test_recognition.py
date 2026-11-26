"""Capture reaction, host side: compound_registry.recognition and the four
operations in the host.

No test needs torch: the worker is replaced by tests/data/fake_recognition_worker.py.
The real models are only used by tests/test_recognition_live.py (opt-in).
All structures are invented, simple molecules.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import logging
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest
from rdkit import Chem

from compound_registry import host, protocol, recognition, recognition_install
from compound_registry.errors import ConfigError, RegistryError
from compound_registry.host import HomeChoice
from compound_registry.recognition import Recognition, Worker, WorkerError

FAKE = Path(__file__).parent / "data" / "fake_recognition_worker.py"
WORKER = Path(recognition.__file__).with_name("recognition_worker.py")


def fake(behaviour: str = "ok", folder: Path | None = None) -> list[str]:
    return [sys.executable, "-I", str(FAKE), behaviour] + ([str(folder)] if folder else [])


def png(width: int = 400, height: int = 200) -> str:
    """A real (white) PNG of this size, as base64."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    rows = (b"\x00" + b"\xff" * width) * height
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return base64.b64encode(data).decode("ascii")


def molfile(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    from rdkit.Chem import AllChem

    AllChem.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


def inchikey(molblock: str) -> str:
    return Chem.MolToInchiKey(Chem.MolFromMolBlock(molblock))


def wait_until(condition, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


@pytest.fixture
def service():
    made: list[Recognition] = []

    def make(behaviour: str = "ok", folder: Path | None = None, **kwargs) -> Recognition:
        made.append(Recognition(command=fake(behaviour, folder), **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.shutdown()


@pytest.fixture
def worker():
    made: list[Worker] = []

    def make(behaviour: str = "ok", folder: Path | None = None, **kwargs) -> Worker:
        made.append(Worker(fake(behaviour, folder), **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.stop()


REQUEST = {"op": "recognize", "mode": "reaction", "image": "AAAA"}


# --------------------------------------------------------------------------- where it is

def test_home_is_the_per_user_folder_or_the_variable(tmp_path):
    windows = recognition.recognition_home({"LOCALAPPDATA": str(tmp_path)}, "win32")
    assert windows == tmp_path / "CompoundRegistry" / "recognition"
    assert recognition.recognition_home({recognition.HOME_ENV_VAR: str(tmp_path / "x")},
                                        "win32") == tmp_path / "x"
    linux = recognition.recognition_home({"XDG_DATA_HOME": str(tmp_path)}, "linux")
    assert linux == tmp_path / "CompoundRegistry" / "recognition"
    assert recognition.recognition_home({}, "darwin").parts[-3:] == (
        "Application Support", "CompoundRegistry", "recognition")
    assert recognition.env_python(tmp_path, "win32") == tmp_path / "env" / "Scripts" / "python.exe"
    assert recognition.env_python(tmp_path, "linux") == tmp_path / "env" / "bin" / "python"


def install_fake_home(home: Path) -> Path:
    python = recognition.env_python(home)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    for path in recognition.model_files(home):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
    recognition_install._write_marker(home)
    return home


def test_install_state_needs_every_part(tmp_path):
    home = tmp_path / "recognition"
    assert recognition.install_state(home) == {
        "installed": False, "home": str(home), "problem": "its Python environment is missing"}
    install_fake_home(home)
    assert recognition.install_state(home)["installed"] is True
    (home / recognition.MARKER_NAME).unlink()
    assert recognition.install_state(home)["problem"] == "its installation was not finished"
    recognition_install._write_marker(home)
    (recognition.models_dir(home) / recognition.MOL_CHECKPOINT).unlink()
    assert recognition.install_state(home)["problem"] == "a model file is missing"


def test_an_installation_made_for_other_pins_counts_as_not_installed(tmp_path):
    """After an update of ChemLinker alone, the new worker must not run in the
    environment of the old version without a word."""
    home = install_fake_home(tmp_path / "recognition")
    marker = home / recognition.MARKER_NAME
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert recognition.install_state(home)["installed"] is True

    marker.write_text(json.dumps({**record, "requirements_sha256": "0" * 64}), encoding="utf-8")
    state = recognition.install_state(home)
    assert state["installed"] is False and state["problem"] == recognition.OUTDATED
    status = Recognition(home).status(start=True)
    assert status["state"] == "not_installed" and "another version" in status["detail"]
    assert recognition.INSTALL_COMMAND in status["detail"]

    models = dict(record["models"], **{recognition.MOL_CHECKPOINT: "f" * 64})
    marker.write_text(json.dumps({**record, "models": models}), encoding="utf-8")
    assert recognition.install_state(home)["problem"] == recognition.OUTDATED

    for unreadable in ("{}", "not json", "[1, 2]"):
        marker.write_text(unreadable, encoding="utf-8")
        assert recognition.install_state(home)["installed"] is False
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert recognition.install_state(home)["installed"] is True


def test_the_record_does_not_depend_on_line_endings(tmp_path, monkeypatch):
    home = install_fake_home(tmp_path / "recognition")
    text = recognition_install.requirements_path().read_bytes().replace(b"\r\n", b"\n")
    copy = tmp_path / "requirements.txt"
    copy.write_bytes(text.replace(b"\n", b"\r\n"))
    monkeypatch.setattr(recognition_install, "requirements_path", lambda: copy)
    assert recognition.install_state(home)["installed"] is True


def test_install_state_follows_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(recognition.HOME_ENV_VAR, str(install_fake_home(tmp_path / "r")))
    assert recognition.install_state() == {"installed": True, "home": str(tmp_path / "r"),
                                           "problem": None}


# --------------------------------------------------------------------------- the picture

def test_check_png_reads_the_size():
    assert recognition.check_png(png(400, 200)) == (400, 200)
    side = protocol.MIN_IMAGE_SIDE
    assert recognition.check_png(png(side, side)) == (side, side)


@pytest.mark.parametrize("image", [
    "", "not base64 !!", "AAA", "AAAA", base64.b64encode(b"GIF89a" + b"\x00" * 60).decode(),
    base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 60).decode(),
    png(protocol.MIN_IMAGE_SIDE - 1, 100), png(100, protocol.MIN_IMAGE_SIDE - 1),
    " " + png(), png() + "\n",
])
def test_check_png_refuses_what_is_no_usable_png(image):
    with pytest.raises(RegistryError) as info:
        recognition.check_png(image)
    assert info.value.code == "image_invalid"


def test_check_png_refuses_a_huge_picture_by_its_header_alone():
    header = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
              + struct.pack(">IIBBBBB", protocol.MAX_IMAGE_SIDE + 1, 100, 8, 0, 0, 0, 0)
              + b"\x00" * 30)
    with pytest.raises(RegistryError) as info:
        recognition.check_png(base64.b64encode(header).decode())
    assert info.value.code == "image_invalid" and "8001 x 100" in info.value.message


# --------------------------------------------------------------------------- warnings

@pytest.mark.parametrize("smiles, expected", [
    ("CCO", []),
    ("c1ccccc1C(=O)O", []),
    ("*CC", ["placeholder"]),
    ("[13CH3]O", ["isotope"]),
    ("[2H]C([2H])O", ["isotope"]),
    ("CC[Mg]Br", ["unusual_element"]),
    ("C[Pb](C)(C)C", ["unusual_element"]),
    ("C[C@H](N)O", ["stereo"]),
    ("CC(N)O", ["stereo"]),                 # a centre without wedge: still to be checked
    ("C/C=C/C", ["stereo"]),
    ("CC=CC", ["stereo"]),                  # could be E or Z and is neither: to be checked
    ("N#CC=Cc1ccccc1", ["stereo"]),
    ("CC=NO", ["stereo"]),
    ("C=CC", []),                           # no geometry possible
    ("CC(C)=CC", []),
    ("C1CCCC=C1", []),
    ("CC(C)C(C)=N", []),                    # nobody draws the geometry of =NH
    ("CC(=O)[O-].[Na+]", ["charged_or_salt"]),
    ("C[N+](C)(C)C", ["charged_or_salt"]),
    ("CCO.Cl", ["charged_or_salt"]),
    ("C[N+](=O)[O-]", []),                  # a nitro group is no salt
    ("C[N+](C)(C)[O-]", []),                # nor an N-oxide
    ("[12NH2]C[C@H](C)[O-].[Y+]", ["isotope", "unusual_element", "stereo", "charged_or_salt"]),
])
def test_structure_warnings(smiles, expected):
    assert recognition.structure_warnings(Chem.MolFromSmiles(smiles)) == expected


def test_every_common_element_is_a_real_element():
    table = Chem.GetPeriodicTable()
    assert all(table.GetAtomicNumber(symbol) > 0 for symbol in recognition.COMMON_ELEMENTS)
    assert {"C", "H", "N", "O"} <= recognition.COMMON_ELEMENTS
    assert not {"Pb", "Pa", "Rf", "V", "Mg", "Y"} & recognition.COMMON_ELEMENTS


def test_describe_structures_gives_canonical_smiles_and_warnings():
    result = recognition.describe_structures([
        {"molfile": None, "smiles": "OCC"},
        {"molfile": molfile("C[C@H](N)c1ccccc1"), "smiles": None},
        {"molfile": None, "smiles": "C(C)(C)(C)(C)(C)C"},
        {"molfile": "this is no molfile", "smiles": None},
        {"molfile": None, "smiles": "[Na+].[O-]C(C)=O"},
    ])
    assert result == {"structures": [
        {"smiles": "CCO", "warnings": []},
        {"smiles": "C[C@H](N)c1ccccc1", "warnings": ["stereo"]},
        {"smiles": None, "warnings": ["unparsable"]},
        {"smiles": None, "warnings": ["unparsable"]},
        {"smiles": "CC(=O)[O-].[Na+]", "warnings": ["charged_or_salt"]},
    ]}
    for entry in result["structures"]:
        assert set(entry["warnings"]) <= set(protocol.STRUCTURE_WARNING_CODES)


def test_describe_structures_reads_a_placeholder_from_a_molfile():
    block = molfile("CC").replace(" C   0", " R   0", 1)
    entry = recognition.describe_structures([{"molfile": block, "smiles": None}])["structures"][0]
    assert entry == {"smiles": "*C", "warnings": ["placeholder"]}


def test_a_double_bond_marked_unknown_in_a_molfile_is_to_be_checked():
    block = molfile("CC=CC")
    assert "  2  3  2  3" in block                               # RDKit's "either" mark
    entry = recognition.describe_structures([{"molfile": block, "smiles": None}])["structures"][0]
    assert entry == {"smiles": "CC=CC", "warnings": ["stereo"]}


def bond_block(first_bond: str) -> str:
    """Ethanol as a molfile whose C-C bond line is ``first_bond``."""
    return ("\n  test\n\n  3  2  0  0  0  0  0  0  0  0999 V2000\n"
            "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
            "    1.3000    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
            "    2.6000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
            f"{first_bond}\n  2  3  1  0\nM  END\n")


def test_a_query_bond_is_a_placeholder():
    """Bond type 8 ("any") or 5 ("single or double") gives a SMILES with "~":
    no real SMILES, so the user must be told."""
    plain, any_bond, either = bond_block("  1  2  1  0"), bond_block("  1  2  8  0"), \
        bond_block("  1  2  5  0")
    result = recognition.describe_structures(
        [{"molfile": text, "smiles": None} for text in (plain, any_bond, either)])["structures"]
    assert result[0] == {"smiles": "CCO", "warnings": []}
    assert "~" in result[1]["smiles"] and result[1]["warnings"] == ["placeholder"]
    assert result[2]["warnings"] == ["placeholder"]


def test_a_file_that_is_not_one_structure_is_unparsable():
    one = bond_block("  1  2  1  0")
    two = one + "$$$$\n" + molfile("c1ccccc1") + "$$$$\n"
    polymer = one.replace("M  END", "M  STY  1   1 SRU\nM  SAL   1  2   1   2\n"
                          "M  SBL   1  1   2\nM  SMT   1 n\nM  END")
    reaction = "$RXN\n\n\n\n  1  1\n$MOL\n" + one
    result = recognition.describe_structures(
        [{"molfile": text, "smiles": None} for text in (one + "$$$$\n", two, polymer, reaction)]
    )["structures"]
    assert result[0] == {"smiles": "CCO", "warnings": []}      # one record with its end mark
    assert result[1:] == [{"smiles": None, "warnings": ["unparsable"]}] * 3


def test_a_huge_structure_is_unparsable_and_does_not_end_the_process(tmp_path):
    """RDKit overflows the C stack when it writes the SMILES of a chain of
    about 3000 atoms: the process is gone without an answer. Run in a process
    of its own, so that a regression fails this test instead of ending the
    whole test run."""
    limit = recognition.MAX_STRUCTURE_ATOMS
    chain = Chem.MolFromSmiles("C" * 3000)
    from rdkit.Chem import AllChem

    AllChem.Compute2DCoords(chain)
    (tmp_path / "big.mol").write_text(Chem.MolToMolBlock(chain), encoding="utf-8")
    program = (
        "import json, sys, threading\n"
        f"sys.path.insert(0, {str(Path(recognition.__file__).parents[1])!r})\n"
        "from compound_registry import recognition\n"
        f"big = open({str(tmp_path / 'big.mol')!r}, encoding='utf-8').read()\n"
        f"asked = [{{'smiles': 'C' * 5000, 'molfile': None}}, {{'smiles': 'C' * {limit + 1}, "
        f"'molfile': None}}, {{'smiles': None, 'molfile': big}}, {{'smiles': 'C' * {limit}, "
        "'molfile': None}]\n"
        "out = []\n"
        # In a thread as well: the host answers ``recognize`` from one.
        "t = threading.Thread(target=lambda: out.append(recognition.describe_structures(asked)))\n"
        "t.start(); t.join()\n"
        "out.append(recognition.describe_structures(asked))\n"
        "out.append(recognition._read_molecule('C' * 5000, big)['warnings'])\n"
        "print(json.dumps(out))\n")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr[-500:]
    in_thread, direct, read = json.loads(done.stdout)
    assert in_thread == direct and read == ["unparsable"]
    assert direct["structures"][:3] == [{"smiles": None, "warnings": ["unparsable"]}] * 3
    assert direct["structures"][3] == {"smiles": "C" * limit, "warnings": []}
    request = protocol.parse_request({"id": 1, "v": protocol.PROTOCOL_VERSION,
                                      "op": "describe_structures",
                                      "params": {"structures": [{"smiles": "C" * 5000}]}})
    assert request.op == "describe_structures"                 # the protocol lets it through


def test_smiles_is_the_one_of_chem():
    from compound_registry import chem

    assert recognition._smiles_of(Chem.MolFromSmiles("OC(=O)c1ccccc1")) == \
        chem.normalize_smiles("OC(=O)c1ccccc1").smiles


# --------------------------------------------------------------------------- SMILES or molfile

#: What MolScribe gives for a drawn "OMe" on a benzene ring: the ring and an
#: R atom with an alias (invented example in its format).
ANISOLE_ABBREVIATED = """
     RDKit          2D

  7  7  0  0  0  0  0  0  0  0999 V2000
    0.0000    2.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.7321    1.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.7321   -1.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.0000   -2.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.7321   -1.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.7321    1.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.0000    4.0000    0.0000 R   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  2  0
  2  3  1  0
  3  4  2  0
  4  5  1  0
  5  6  2  0
  6  1  1  0
  1  7  1  0
A    7
OMe
M  END
"""


def atom_places(molblock: str) -> list[tuple[str, float, float]]:
    mol = Chem.MolFromMolBlock(molblock, sanitize=False, removeHs=False)
    conformer = mol.GetConformer()
    return [(a.GetSymbol(), round(conformer.GetAtomPosition(a.GetIdx()).x, 2),
             round(conformer.GetAtomPosition(a.GetIdx()).y, 2)) for a in mol.GetAtoms()]


def test_equal_answers_keep_the_places_of_the_models_molfile():
    """The molfile is written again (bond length, wedges from the SMILES, see
    depiction.py), but every atom stays where the model saw it: here RDKit's
    own zigzag of ethanol, O on the right."""
    given = molfile("CCO")
    read = recognition._read_molecule("OCC", given)
    assert read["smiles"] == "CCO" and read["warnings"] == []
    assert inchikey(read["molfile"]) == inchikey(given)
    # The same zigzag: the middle carbon on top, the O on the right, bonds of 1.5.
    # (The atoms come in the order of the SMILES that was read: O, C, C.)
    (o, x3, y3), (_c2, x2, y2), (_c1, x1, y1) = atom_places(read["molfile"])
    assert o == "O" and x1 < x2 < x3 and y2 > y1 and y2 > y3
    assert ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 == pytest.approx(1.5, abs=0.02)
    assert ((x3 - x2) ** 2 + (y3 - y2) ** 2) ** 0.5 == pytest.approx(1.5, abs=0.02)


def test_different_answers_take_the_smiles_and_redraw_it_on_the_models_coordinates():
    read = recognition._read_molecule("COc1ccccc1", ANISOLE_ABBREVIATED)
    assert read["smiles"] == "COc1ccccc1"
    # An abbreviation the SMILES has expanded is no disagreement: no warning.
    assert read["warnings"] == []
    drawn = Chem.MolFromMolBlock(read["molfile"])
    assert Chem.MolToSmiles(drawn) == "COc1ccccc1" and "V2000" in read["molfile"]
    # The ring keeps its drawn orientation (a hexagon standing on a corner,
    # scaled to RDKit's bond length of 1.5): two ring atoms on the vertical axis.
    conformer = drawn.GetConformer()
    ring = [a.GetIdx() for a in drawn.GetAtoms() if a.IsInRing()]
    xs = sorted(round(conformer.GetAtomPosition(i).x, 2) for i in ring)
    assert xs == [-1.3, -1.3, 0.0, 0.0, 1.3, 1.3]


def test_a_stereo_difference_is_reported_and_the_molfile_follows_the_smiles():
    read = recognition._read_molecule("C[C@H](N)c1ccccc1", molfile("C[C@@H](N)c1ccccc1"))
    assert read["smiles"] == "C[C@H](N)c1ccccc1"
    assert read["warnings"] == ["stereo", "smiles_molfile_differ"]
    assert Chem.MolToSmiles(Chem.MolFromMolBlock(read["molfile"])) == "C[C@H](N)c1ccccc1"


@pytest.mark.parametrize("smiles, drawn, differ", [
    # the drawing leaves "CO2Me" / "Boc" as a placeholder: the same structure
    ("COC(=O)c1ccccc1", "*c1ccccc1", False),
    ("C[C@H](NC(=O)OC(C)(C)C)C(=O)O", "C[C@H](N*)C(=O)O", False),
    ("COC(=O)/C=C/c1ccccc1", "*/C=C/c1ccccc1", False),
    ("[1*]c1ccccc1", "*c1ccccc1", False),                 # an R group written in two ways
    # real disagreements behind an abbreviation
    ("C[C@H](NC(=O)OC(C)(C)C)C(=O)O", "C[C@@H](N*)C(=O)O", True),      # the other wedge
    ("CC(NC(=O)OC(C)(C)C)C(=O)O", "C[C@H](N*)C(=O)O", True),           # a wedge only drawn
    ("C[C@H](NC(=O)OC(C)(C)C)C(=O)O", "CC(N*)C(=O)O", True),           # a wedge only in the SMILES
    ("COC(=O)/C=C/c1ccccc1", "*/C=C\\c1ccccc1", True),                 # the other geometry
    ("COC(=O)C=Cc1ccccc1", "*/C=C/c1ccccc1", True),                    # geometry only drawn
    ("COC(=O)c1ccccc1Cl", "*c1ccccc1", True),                          # an atom more
    ("COC(=O)c1ccccc1.Cl", "*c1ccccc1", True),                         # a second fragment
    ("COC(=O)c1ccccn1", "*c1ccccc1", True),                            # another atom
    ("COC(=O)CC[NH3+]", "*CCN", True),                                 # another charge
])
def test_only_a_real_disagreement_gets_the_warning(smiles, drawn, differ):
    # RDKit writes "*" into a molfile as an R atom, like MolScribe an abbreviation.
    read = recognition._read_molecule(smiles, molfile(drawn))
    assert ("smiles_molfile_differ" in read["warnings"]) is differ
    # Whatever the drawing was, the molfile that is returned is the SMILES structure.
    back = recognition._mol_from_molfile(read["molfile"])
    assert recognition._smiles_of(back) == read["smiles"]


@pytest.mark.parametrize("smiles, drawn", [
    ("C/C=C/C", "C/C=C\\C"),
    ("C/C=C\\C", "C/C=C/C"),
    ("C(=C/c1ccccc1)\\c1ccccc1", "C(=C\\c1ccccc1)\\c1ccccc1"),
    ("COC(=O)/C=C/c1ccccc1", "*C(=O)/C=C\\c1ccccc1"),
])
def test_the_molfile_never_shows_the_other_double_bond_geometry(smiles, drawn):
    """On the drawing's coordinates the molfile would have the drawing's E/Z
    (in a molfile the coordinates ARE the geometry) and contradict the SMILES:
    Copy would give E and "Open in ChemDraw" would give Z."""
    read = recognition._read_molecule(smiles, molfile(drawn))
    assert read["smiles"] == Chem.MolToSmiles(Chem.MolFromSmiles(smiles))
    assert read["warnings"] == ["stereo", "smiles_molfile_differ"]
    assert Chem.MolToSmiles(Chem.MolFromMolBlock(read["molfile"])) == read["smiles"]


def test_an_unreadable_smiles_falls_back_to_the_molfile():
    given = molfile("CCO")
    read = recognition._read_molecule("C(C)(C)(C)(C)(C)C", given)
    assert read["smiles"] == "CCO" and inchikey(read["molfile"]) == inchikey(given)
    assert [symbol for symbol, _x, _y in atom_places(read["molfile"])] == ["C", "C", "O"]
    assert read["warnings"] == ["smiles_molfile_differ"]


def test_a_molfile_that_is_the_only_reading_keeps_its_placeholder_label():
    read = recognition._read_molecule("not a smiles(", ANISOLE_ABBREVIATED)
    assert read["smiles"] == "*c1ccccc1" and "placeholder" in read["warnings"]
    assert "OMe" in read["molfile"].split("\n")
    assert recognition._smiles_of(recognition._mol_from_molfile(read["molfile"])) == "*c1ccccc1"


def test_every_returned_molfile_reads_back_as_its_smiles():
    """The rule for the whole result: card, editor and ChemDraw get the
    molfile, Copy gives the SMILES; the two must be one structure, stereo
    included."""
    cases = [("C[C@H](N)c1ccccc1", molfile("C[C@H](N)c1ccccc1")),
             ("C[C@H](N)c1ccccc1", molfile("C[C@@H](N)c1ccccc1")),
             ("C[C@H](N)c1ccccc1", molfile("CC(N)c1ccccc1")),
             ("C[C@H](N)c1ccccc1", None),
             ("C/C=C/C(=O)O", molfile("C/C=C\\C(=O)O")),
             ("CC=CC(=O)O", molfile("C/C=C/C(=O)O")),
             ("O=C([O-])c1ccccc1.[Na+]", molfile("O=C([O-])c1ccccc1.[Na+]")),
             ("C[C@H]1CC[C@@H](C(C)C)[C@H](O)C1", molfile("C[C@H]1CC[C@@H](C(C)C)[C@H](O)C1")),
             ("COc1ccccc1", ANISOLE_ABBREVIATED)]
    for smiles, drawn in cases:
        read = recognition._read_molecule(smiles, drawn)
        assert read["smiles"] == Chem.MolToSmiles(Chem.MolFromSmiles(smiles))
        back = recognition._mol_from_molfile(read["molfile"])
        assert recognition._smiles_of(back) == read["smiles"], smiles
        assert Chem.MolToSmiles(Chem.MolFromMolBlock(read["molfile"])) == read["smiles"], smiles


def test_a_smiles_alone_gets_a_drawn_molfile_and_no_difference():
    read = recognition._read_molecule("CCO", None)
    assert read["warnings"] == [] and inchikey(read["molfile"]) == inchikey(molfile("CCO"))


def test_nothing_readable_is_unparsable():
    assert recognition._read_molecule("C(C)(C)(C)(C)(C)C", "junk") == {
        "smiles": None, "molfile": None, "warnings": ["unparsable"], "identity": None}
    assert recognition._read_molecule(None, None)["warnings"] == ["unparsable"]


def test_a_placeholder_stays_in_the_smiles():
    read = recognition._read_molecule("*c1ccccc1", None)
    assert read["smiles"] == "*c1ccccc1" and read["warnings"] == ["placeholder"]


# --------------------------------------------------------------------------- the result

def raw_scheme() -> dict:
    """Two steps A -> B -> C, B drawn once, plus a molecule in no step."""
    return {
        "width": 1000, "height": 300,
        "molecules": [
            {"box": [10, 50, 200, 250], "smiles": "O=Cc1ccccc1", "molfile": None, "confidence": 0.95},
            {"box": [400.4, 50, 600, 250], "smiles": "OCc1ccccc1", "molfile": molfile("OCc1ccccc1"),
             "confidence": 0.71},
            {"box": [800, 50, 1200, 400], "smiles": "ClCc1ccccc1", "molfile": None, "confidence": 0.9},
            {"box": [250, 5, 350, 60], "smiles": "CO", "molfile": None, "confidence": None},
            {"box": [5, 5, 5, 60], "smiles": "CC", "molfile": None, "confidence": 0.9},   # no area
        ],
        "reactions": [
            {"reactants": [0], "agents": [3], "products": [1],
             "texts": [{"box": [220, 100, 380, 140], "role": "conditions",
                        "lines": [" NaBH4 ", "", "MeOH,\t0 \u00b0C", 7]},
                       {"box": [220, 150, 380, 190], "role": "conditions", "lines": []}]},
            {"reactants": [1], "agents": [], "products": [2, 2, 4, 17], "texts": []},
            {"reactants": [], "agents": [], "products": [], "texts": []},
        ],
    }


def test_build_result_numbers_the_molecules_and_the_steps():
    result = recognition.build_result(raw_scheme(), "reaction", 12.3456)
    assert (result["width"], result["height"], result["seconds"]) == (1000, 300, 12.35)
    assert [m["n"] for m in result["molecules"]] == [1, 2, 3, 4]
    assert [m["box"] for m in result["molecules"]] == [
        [10, 50, 200, 250], [400, 50, 600, 250], [800, 50, 1000, 300], [250, 5, 350, 60]]
    assert [m["smiles"] for m in result["molecules"]] == [
        "O=Cc1ccccc1", "OCc1ccccc1", "ClCc1ccccc1", "CO"]
    assert [m["confidence"] for m in result["molecules"]] == [0.95, 0.71, 0.9, None]
    assert [m["warnings"] for m in result["molecules"]] == [[], ["low_confidence"], [], []]
    assert all("V2000" in m["molfile"] for m in result["molecules"])
    assert result["reactions"] == [
        {"reactants": [1], "agents": [4], "products": [2],
         "conditions": [{"box": [220, 100, 380, 140], "lines": ["NaBH4", "MeOH, 0 \u00b0C"]}],
         "reaction_smiles": "O=Cc1ccccc1>CO>OCc1ccccc1"},
        {"reactants": [2], "agents": [], "products": [3], "conditions": [],
         "reaction_smiles": "OCc1ccccc1>>ClCc1ccccc1"},
    ]
    assert result["unassigned"] == [] and result["warnings"] == []
    for m in result["molecules"]:
        x0, y0, x1, y1 = m["box"]
        assert 0 <= x0 < x1 <= result["width"] and 0 <= y0 < y1 <= result["height"]
        assert set(m["warnings"]) <= set(protocol.MOLECULE_WARNING_CODES)
    json.dumps(result, allow_nan=False)


def test_build_result_reports_unassigned_molecules_and_duplicates():
    raw = raw_scheme()
    raw["molecules"].append({"box": [0, 260, 90, 299], "smiles": "c1ccccc1C=O", "molfile": None,
                             "confidence": 0.99})
    result = recognition.build_result(raw, "reaction", 1)
    assert result["unassigned"] == [5] and result["warnings"] == ["unassigned_molecules"]
    assert [m["warnings"] for m in result["molecules"]] == [
        ["duplicate"], ["low_confidence"], [], [], ["duplicate"]]


def test_duplicate_means_the_same_smiles_like_on_the_page():
    """The page decides "duplicate" by equal SMILES after an edit; the host
    must use the same rule, or the flag vanishes from molecules nobody
    touched. An InChIKey would call a pyridone and its hydroxypyridine, or
    the two drawings of sodium acetate, the same structure."""
    def scheme(*smiles: str) -> dict:
        return {"width": 900, "height": 100, "reactions": [],
                "molecules": [{"box": [i * 100, 0, i * 100 + 90, 90], "smiles": text,
                               "molfile": None, "confidence": 0.95}
                              for i, text in enumerate(smiles)]}

    def flagged(*smiles: str) -> list[bool]:
        result = recognition.build_result(scheme(*smiles), "molecule", 1)
        return ["duplicate" in m["warnings"] for m in result["molecules"]]

    assert flagged("Oc1ccccn1", "O=c1cccc[nH]1") == [False, False]
    assert flagged("CC(=O)[O-].[Na+]", "CC(=O)O[Na]") == [False, False]
    assert flagged("OC(=O)c1ccccc1", "c1ccccc1C(O)=O", "CCO") == [True, True, False]
    assert flagged("*c1ccccc1", "c1ccccc1*") == [True, True]
    assert flagged("C[C@H](N)O", "C[C@@H](N)O") == [False, False]
    result = recognition.build_result(scheme("OC(=O)c1ccccc1", "c1ccccc1C(O)=O"), "molecule", 1)
    assert result["molecules"][0]["smiles"] == result["molecules"][1]["smiles"]


def test_reaction_smiles_is_null_without_a_readable_reactant_or_product():
    raw = raw_scheme()
    raw["molecules"][0]["smiles"] = "C(C)(C)(C)(C)(C)C"      # the reactant of step 1
    raw["molecules"][3]["smiles"] = "not a smiles"            # the agent of step 1
    result = recognition.build_result(raw, "reaction", 1)
    assert result["molecules"][0]["smiles"] is None
    assert result["molecules"][0]["warnings"] == ["unparsable"]
    assert result["reactions"][0]["reaction_smiles"] is None
    assert result["reactions"][1]["reaction_smiles"] == "OCc1ccccc1>>ClCc1ccccc1"
    raw = raw_scheme()
    raw["molecules"][3]["smiles"] = "not a smiles"            # an unreadable agent is left out
    result = recognition.build_result(raw, "reaction", 1)
    assert result["reactions"][0]["reaction_smiles"] == "O=Cc1ccccc1>>OCc1ccccc1"
    assert recognition.reaction_smiles([], [], []) is None
    assert recognition.reaction_smiles(["CC"], [], []) == "CC>>"
    assert recognition.reaction_smiles([], ["O"], ["CC", "CO"]) == ">O>CC.CO"


def test_build_result_without_reaction_or_molecule():
    raw = raw_scheme()
    raw["reactions"] = []
    result = recognition.build_result(raw, "reaction", 1)
    assert result["warnings"] == ["no_reaction"] and result["unassigned"] == [1, 2, 3, 4]
    empty = recognition.build_result({"width": 50, "height": 40, "molecules": [], "reactions": []},
                                     "reaction", 1)
    assert empty["warnings"] == ["no_molecules"] and empty["molecules"] == []


def test_mode_molecule_never_gives_reactions():
    result = recognition.build_result(raw_scheme(), "molecule", 1)
    assert result["reactions"] == [] and result["unassigned"] == [] and result["warnings"] == []
    assert len(result["molecules"]) == 4


def test_build_result_keeps_at_most_the_allowed_number_of_molecules():
    limit = protocol.MAX_RECOGNITION_MOLECULES
    raw = {"width": 5000, "height": 100, "reactions": [{"reactants": [0], "agents": [],
                                                       "products": [limit + 4], "texts": []}],
           "molecules": [{"box": [i * 50, 0, i * 50 + 40, 90], "smiles": "C" * (i + 1),
                          "molfile": None, "confidence": 0.9} for i in range(limit + 5)]}
    result = recognition.build_result(raw, "reaction", 1)
    assert len(result["molecules"]) == limit and "truncated" in result["warnings"]
    assert result["reactions"] == [{"reactants": [1], "agents": [], "products": [],
                                    "conditions": [], "reaction_smiles": "C>>"}]


def test_build_result_stays_under_the_size_limit():
    result = recognition.build_result(raw_scheme(), "reaction", 1, max_bytes=2500)
    assert 0 < len(result["molecules"]) < 4 and "truncated" in result["warnings"]
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 2500
    numbers = {m["n"] for m in result["molecules"]}
    for reaction in result["reactions"]:
        assert set(reaction["reactants"] + reaction["agents"] + reaction["products"]) <= numbers


@pytest.mark.parametrize("raw", [None, [], {}, {"molecules": [], "reactions": []},
                                 {"width": "x", "height": 1, "molecules": [], "reactions": []},
                                 {"width": 1, "height": 1, "molecules": "none", "reactions": []}])
def test_build_result_refuses_an_answer_of_another_shape(raw):
    with pytest.raises(RegistryError) as info:
        recognition.build_result(raw, "reaction", 1)
    assert info.value.code == "recognition_failed"


# --------------------------------------------------------------------------- the worker

def test_worker_starts_loads_and_answers(worker):
    w = worker("ok")
    assert w.state == "stopped" and w.pid is None
    w.start()
    assert w.state in ("loading", "ready")
    result = w.request(dict(REQUEST), timeout=20)
    assert w.state == "ready" and w.load_seconds == 0.2
    assert result["width"] == 400 and result["timing"]["image_chars"] == 4
    assert w.request({**REQUEST, "mode": "molecule"}, timeout=20)["reactions"] == []
    assert w.starts == 1
    assert any("stderr" in line for line in w.stderr_tail)     # drained, kept in memory only


def test_worker_request_starts_it_when_needed(worker):
    w = worker("ok")
    assert w.request(dict(REQUEST), timeout=20)["height"] == 200
    w.stop()
    assert w.state == "stopped"
    assert w.request(dict(REQUEST), timeout=20)["height"] == 200 and w.starts == 2


def test_worker_stop_ends_the_process(worker):
    w = worker("ok")
    w.request(dict(REQUEST), timeout=20)
    run = w._running
    w.stop()
    assert run.proc.poll() is not None and w.pid is None and w.state == "stopped"
    w.stop()                                                   # twice is fine


def test_worker_ignores_garbage_on_its_output(worker):
    w = worker("garbage")
    assert w.request(dict(REQUEST), timeout=20)["width"] == 400
    assert w.request(dict(REQUEST), timeout=20)["width"] == 400
    assert w.garbage_lines >= 4 and w.state == "ready"


def test_worker_that_fails_to_load(worker):
    w = worker("fail_load")
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=20)
    assert (info.value.kind, info.value.error) == ("load", "ImportError")
    assert w.state == "failed" and w.failure_error == "ImportError"


def test_worker_that_dies_while_loading(worker):
    w = worker("die_loading")
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=20)
    assert info.value.kind == "load" and w.state == "failed"


def test_worker_that_cannot_be_started(tmp_path):
    w = Worker([str(tmp_path / "no-such-python.exe"), "-I", "x.py"])
    with pytest.raises(WorkerError) as info:
        w.start()
    assert info.value.kind == "start" and w.state == "failed"


def test_worker_still_loading_at_the_deadline_keeps_loading(worker):
    w = worker("slow_load")
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=0.2)
    assert info.value.kind == "loading" and w.state == "loading"
    assert w.request(dict(REQUEST), timeout=20)["width"] == 400 and w.starts == 1


def test_worker_that_never_gets_ready_is_stopped_after_the_load_timeout(worker):
    w = worker("never_ready", load_timeout=0.3)
    w.start()
    run = w._running
    assert wait_until(lambda: w.state == "failed", 5)
    assert w.failure_error == "LoadTimeout"
    assert wait_until(lambda: run.proc.poll() is not None, 10)


def test_worker_request_timeout_stops_the_worker(worker):
    w = worker("hang")
    w.start()
    assert wait_until(lambda: w.state == "ready")
    run = w._running
    started = time.monotonic()
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=0.5)
    assert info.value.kind == "timeout" and time.monotonic() - started < 5
    assert wait_until(lambda: run.proc.poll() is not None, 10) and w.state == "stopped"


def test_worker_that_dies_during_a_request(worker):
    w = worker("crash")
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=20)
    assert info.value.kind == "died" and w.state == "stopped"


def test_worker_reports_a_model_error_by_name_only(worker):
    w = worker("model_error")
    with pytest.raises(WorkerError) as info:
        w.request(dict(REQUEST), timeout=20)
    assert (info.value.kind, info.value.stage, info.value.error) == ("model", "layout", "RuntimeError")
    assert w.state == "ready"                                  # it lives on


def test_an_unsafe_error_name_is_not_passed_on():
    assert recognition._safe_name("RuntimeError") == "RuntimeError"
    assert recognition._safe_name("torch.cuda.OutOfMemoryError") == "torch.cuda.OutOfMemoryError"
    for unsafe in ("CCO was read", "a\nb", "x" * 200, "", None, 5, "<b>"):
        assert recognition._safe_name(unsafe) is None


def test_the_worker_ends_when_its_host_is_killed(tmp_path):
    """No orphan: the host process is killed (not closed), and the worker
    notices that its stdin closed."""
    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(recognition.__file__).parents[1])!r})\n"
        "from compound_registry.recognition import Worker\n"
        f"w = Worker({fake('ok', tmp_path)!r})\n"
        "w.request({'op': 'recognize', 'mode': 'reaction', 'image': 'AAAA'}, 30)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n")
    parent = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE)
    try:
        assert parent.stdout.readline().strip() == b"ready"
        assert not (tmp_path / "ended-1").exists()
        parent.kill()
        parent.wait(10)
        assert wait_until(lambda: (tmp_path / "ended-1").exists(), 15)
    finally:
        parent.kill()
        parent.stdout.close()


# --------------------------------------------------------------------------- the real worker file

def test_the_worker_file_is_standalone():
    """Standard library at the top, nothing of this package anywhere."""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module)
    assert top == {"os", "sys", "time"}
    for node in ast.walk(tree):
        names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        assert not any(name.split(".")[0] == "compound_registry" for name in names)
        assert not (isinstance(node, ast.ImportFrom) and node.level)


def test_the_real_worker_reports_a_failed_load_without_its_libraries(tmp_path):
    """In the registry's own environment torch is missing: the real worker
    must say so in the protocol and end."""
    pytest.importorskip("rdkit")
    try:
        import torch  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("torch is installed in this environment")
    w = Worker([sys.executable, "-I", str(WORKER), "--models", str(tmp_path)])
    try:
        with pytest.raises(WorkerError) as info:
            w.request(dict(REQUEST), timeout=60)
        assert (info.value.kind, info.value.error) == ("load", "ModuleNotFoundError")
        assert w.state == "failed" and w.garbage_lines == 0
    finally:
        w.stop()


def test_the_real_worker_ends_when_its_stdin_closes():
    program = (
        "import importlib.util, queue, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('worker', {str(WORKER)!r})\n"
        "worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)\n"
        "requests_in, lines_out = worker._protect_streams()\n"
        "print('this must not reach the protocol stream')\n"
        "worker._start_reader(requests_in, queue.Queue())\n"
        "worker._Channel(lines_out).send({'event': 'hello'})\n"
        "time.sleep(120)\n")
    proc = subprocess.Popen([sys.executable, "-I", "-c", program], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert json.loads(proc.stdout.readline()) == {"event": "hello"}
        assert proc.poll() is None
        proc.stdin.close()
        assert proc.wait(15) == 0
        assert proc.stdout.read() == b""
        assert b"must not reach" in proc.stderr.read()
    finally:
        proc.kill()
        proc.stdout.close()
        proc.stderr.close()


# --------------------------------------------------------------------------- the service

def test_status_when_nothing_is_installed(tmp_path):
    r = Recognition(tmp_path / "nothing")
    status = r.status(start=True)
    assert status["installed"] is False and status["state"] == "not_installed"
    assert recognition.INSTALL_COMMAND in status["detail"] and r.worker is None
    with pytest.raises(RegistryError) as info:
        r.recognize(png(), "reaction")
    assert info.value.code == "recognition_not_installed"
    assert recognition.INSTALL_COMMAND in info.value.message


def test_the_texts_for_people_call_it_chemrecogniser(tmp_path):
    """The installer asks "Install ChemResolver?", so every text a person
    reads uses that name; commands and operations keep "recognition"."""
    from compound_registry import setup_wizard

    name = recognition.RECOGNISER_NAME
    assert name == setup_wizard.RECOGNISER_NAME == "ChemResolver"
    assert recognition.NOT_INSTALLED.startswith(f"{name}, the optional part of ChemLinker")
    assert recognition.INSTALL_COMMAND == "compound-registry install-recognition"
    home = install_fake_home(tmp_path / "r")
    (home / recognition.MARKER_NAME).unlink()
    assert Recognition(home).status()["detail"].startswith(f"{name} in {home} is not complete: ")
    r = Recognition(home)
    for kind in ("start", "load", "loading", "timeout", "died", "model"):
        text = r._failure_text(WorkerError(kind))
        assert name in text and "recognition program" not in text and "recognition part" not in text
    assert name in r._failure_text(WorkerError("load", error="LoadTimeout"))


def test_status_of_an_unfinished_installation(tmp_path):
    home = install_fake_home(tmp_path / "r")
    (home / recognition.MARKER_NAME).unlink()
    status = Recognition(home).status()
    assert status["state"] == "not_installed" and "not complete" in status["detail"]
    assert "its installation was not finished" in status["detail"]


def test_status_start_returns_at_once_and_the_models_load_in_the_background(service):
    r = service("slow_load")
    assert r.status() == {"installed": True, "state": "stopped"}
    started = time.monotonic()
    status = r.status(start=True)
    assert time.monotonic() - started < 0.9
    assert status["installed"] is True and status["state"] == "loading"
    assert "load_seconds" in status
    assert wait_until(lambda: r.status()["state"] == "ready")
    assert r.status() == {"installed": True, "state": "ready", "load_seconds": 0.2}
    assert r.worker.starts == 1
    r.status(start=True)                                       # already running: no second one
    assert r.worker.starts == 1


def test_status_of_a_failed_worker_says_why_without_details_of_a_picture(service):
    r = service("fail_load")
    r.status(start=True)
    assert wait_until(lambda: r.status()["state"] == "failed")
    detail = r.status()["detail"]
    assert "could not be loaded (ImportError)" in detail and recognition.INSTALL_COMMAND in detail
    r.status(start=True)                                       # asking again tries again
    assert r.worker.starts == 2


def test_recognize_returns_the_protocol_result(service):
    r = service("ok")
    result = r.recognize(png(), "reaction")
    assert set(result) == {"width", "height", "molecules", "reactions", "unassigned", "seconds",
                           "warnings"}
    assert [m["smiles"] for m in result["molecules"]] == ["CC=O", "CCO"]
    assert result["molecules"][1]["warnings"] == ["low_confidence"]
    assert result["reactions"] == [{
        "reactants": [1], "agents": [], "products": [2],
        "conditions": [{"box": [130, 30, 270, 60], "lines": ["NaBH4", "MeOH, 0 C"]}],
        "reaction_smiles": "CC=O>>CCO"}]
    assert result["seconds"] >= 0 and result["warnings"] == []
    single = r.recognize(png(), "molecule")
    assert single["reactions"] == [] and single["molecules"][0]["box"] == [0, 0, 400, 200]


def test_recognize_checks_the_picture_before_anything_is_started(service):
    r = service("ok")
    with pytest.raises(RegistryError) as info:
        r.recognize("AAAA", "reaction")
    assert info.value.code == "image_invalid" and r.worker is None


def test_recognize_restarts_a_worker_that_died_once(service, tmp_path, caplog):
    r = service("crash_once", tmp_path)
    with caplog.at_level(logging.INFO, logger="compound_registry.host"):
        result = r.recognize(png(), "reaction")
    assert result["molecules"] and r.worker.starts == 2
    assert (tmp_path / "starts").read_text() == "2"
    assert "restarting it once" in caplog.text


def test_recognize_gives_up_after_one_restart(service, tmp_path):
    r = service("crash", tmp_path)
    with pytest.raises(RegistryError) as info:
        r.recognize(png(), "reaction")
    assert info.value.code == "recognition_failed" and "ended unexpectedly" in info.value.message
    assert (tmp_path / "starts").read_text() == "2"


def test_recognize_time_out(service):
    r = service("hang")
    with pytest.raises(RegistryError) as info:
        r.recognize(png(), "reaction", timeout=1.0)
    assert info.value.code == "recognition_failed" and "no answer in time" in info.value.message
    assert wait_until(lambda: r.worker.state == "stopped")


def test_recognize_while_the_models_still_load(service):
    r = service("slow_load")
    with pytest.raises(RegistryError) as info:
        r.recognize(png(), "reaction", timeout=0.2)
    assert info.value.code == "recognition_failed" and "still loading" in info.value.message
    assert r.recognize(png(), "reaction")["molecules"] and r.worker.starts == 1


def test_recognize_errors_of_the_model(service):
    with pytest.raises(RegistryError) as info:
        service("model_error").recognize(png(), "reaction")
    assert info.value.code == "recognition_failed"
    assert "finding the reactions (RuntimeError)" in info.value.message
    with pytest.raises(RegistryError) as info:
        service("image_error").recognize(png(), "reaction")
    assert info.value.code == "image_invalid"
    with pytest.raises(RegistryError) as info:
        service("bad_shape").recognize(png(), "reaction")
    assert info.value.code == "recognition_failed"
    with pytest.raises(RegistryError) as info:
        service("fail_load").recognize(png(), "reaction")
    assert info.value.code == "recognition_failed" and "(ImportError)" in info.value.message


def test_the_worker_is_stopped_after_the_idle_time(service):
    r = service("ok", idle_seconds=1.0)
    r.recognize(png(), "reaction")
    run = r.worker._running
    assert wait_until(lambda: r.worker.state == "stopped", 15)
    assert wait_until(lambda: run.proc.poll() is not None, 10)
    assert r.recognize(png(), "reaction")["molecules"]          # and starts again when needed


def test_an_idle_timer_that_fires_early_still_stops_the_worker(service):
    """On Windows a timer can fire a few milliseconds before its time by
    another clock. The check must not answer "not yet" and leave: nothing
    would ever look again, and the worker (2.5 GB) would stay for good."""
    r = service("ok", idle_seconds=300)
    r.recognize(png(), "reaction")
    timer = r._timer
    timer.cancel()
    assert r.worker.state == "ready"
    timer.function(*timer.args)                                 # the newest timer, fired "early"
    assert r.worker.state == "stopped"


def test_only_the_newest_idle_timer_stops_the_worker(service):
    r = service("ok", idle_seconds=300)
    r.recognize(png(), "reaction")
    old = r._timer
    r.recognize(png(), "reaction")                              # used again: a new timer
    assert r._timer is not old
    old.function(*old.args)                                     # the old one fires late
    assert r.worker.state == "ready"
    r._busy = 1                                                 # nor during a request
    r._timer.function(*r._timer.args)
    assert r.worker.state == "ready"
    r._busy = 0


def test_asking_for_a_start_counts_as_use_also_when_the_worker_runs(service):
    """A capture page that opens gets "ready"; the worker must then not be
    stopped a moment later because the last recognition was long ago."""
    r = service("ok", idle_seconds=300)
    r.status(start=True)
    assert wait_until(lambda: r.status()["state"] == "ready")
    first = r._timer
    assert r.status()["state"] == "ready" and r._timer is first       # a plain question: no use
    assert r.status(start=True)["state"] == "ready"
    assert r._timer is not first and r.worker.starts == 1
    first.function(*first.args)                                 # the idle time of the first use
    assert r.worker.state == "ready"


def test_no_new_worker_is_started_while_the_host_ends(service, tmp_path):
    r = service("crash", tmp_path)
    worker = r._get_worker()
    r._closed = True
    with pytest.raises(WorkerError) as info:
        r._ask(worker, {"op": "recognize", "mode": "reaction", "image": "AAAA"},
               time.monotonic() + 20)
    assert info.value.kind == "died" and worker.starts == 1


def test_shutdown_stops_the_worker(service):
    r = service("ok")
    r.recognize(png(), "reaction")
    run = r.worker._running
    r.shutdown()
    assert run.proc.poll() is not None and r.worker is None
    r.shutdown()


def test_nothing_of_a_picture_is_logged(service, caplog):
    r = service("ok")
    with caplog.at_level(logging.DEBUG):
        r.status(start=True)
        r.recognize(png(), "reaction")
    for forbidden in ("CCO", "CC=O", "NaBH4", "MeOH", "V2000", png()[:40]):
        assert forbidden not in caplog.text


# --------------------------------------------------------------------------- the host

@pytest.fixture
def host_service(monkeypatch):
    """The host's recognition, with the fake worker."""
    made: list[Recognition] = []

    def install(behaviour: str = "ok", **kwargs) -> Recognition:
        made.append(Recognition(command=fake(behaviour), **kwargs))
        monkeypatch.setattr(recognition, "_default", made[-1])
        return made[-1]

    yield install
    for item in made:
        item.shutdown()


def ask(op: str, params: dict, choice: HomeChoice | None = None, **kwargs) -> dict:
    body = json.dumps({"id": 5, "v": protocol.PROTOCOL_VERSION, "op": op,
                       "params": params}).encode("utf-8")
    choice = choice or HomeChoice(Path("Z:/no/such/registry"), "host-config")
    return host.handle_message(body, choice, **kwargs).response


def result_of(response: dict) -> dict:
    assert response["ok"] is True, response
    return response["result"]


def code_of(response: dict) -> str:
    assert response["ok"] is False, response
    return response["error"]["code"]


def no_registry(home):
    raise AssertionError("a capture operation must not open a registry")


def test_every_capture_operation_has_a_handler():
    assert set(recognition.OPERATIONS) == set(protocol.CAPTURE_OPS)
    assert not set(recognition.OPERATIONS) & set(host._OPERATIONS)


@pytest.mark.parametrize("choice", [
    HomeChoice(Path("Z:/no/such/registry"), "host-config"),
    HomeChoice(None, "host-config", error=ConfigError("the host configuration is invalid")),
])
def test_the_capture_operations_need_no_registry(host_service, choice, tmp_path, monkeypatch):
    host_service("ok")
    monkeypatch.setattr(recognition, "chemdraw_folder", lambda: tmp_path / "out")
    assert code_of(ask("ping", {}, choice)) in ("registry_missing", "config_invalid")
    status = result_of(ask("recognition_status", {}, choice, open_service=no_registry))
    assert status == {"installed": True, "state": "stopped"}
    assert result_of(ask("recognition_status", {"start": True}, choice,
                         open_service=no_registry))["state"] in ("loading", "ready")
    read = result_of(ask("recognize", {"image": png(), "mode": "reaction"}, choice,
                         open_service=no_registry))
    assert read["reactions"][0]["reaction_smiles"] == "CC=O>>CCO"
    described = result_of(ask("describe_structures", {"structures": [{"smiles": "OCC"}]}, choice,
                              open_service=no_registry))
    assert described == {"structures": [{"smiles": "CCO", "warnings": []}]}
    drawn = result_of(ask("reaction_to_chemdraw", {"steps": [
        {"reactants": [molfile("CC=O")], "products": [molfile("CCO")]}]}, choice,
        open_service=no_registry))
    assert Path(drawn["path"]).is_file() and drawn["opened"] is False


def test_recognize_errors_reach_the_extension_with_their_codes(host_service, monkeypatch, tmp_path):
    host_service("model_error")
    assert code_of(ask("recognize", {"image": png(), "mode": "reaction"})) == "recognition_failed"
    assert code_of(ask("recognize", {"image": "AAAA", "mode": "reaction"})) == "image_invalid"
    assert code_of(ask("recognize", {"image": png(), "mode": "table"})) == "bad_request"
    assert code_of(ask("recognize", {"image": png()})) == "bad_request"
    monkeypatch.setattr(recognition, "_default", Recognition(tmp_path / "nothing"))
    response = ask("recognize", {"image": png(), "mode": "reaction"})
    assert code_of(response) == "recognition_not_installed"
    assert result_of(ask("recognition_status", {"start": True}))["state"] == "not_installed"


def test_the_host_log_names_the_operation_only(host_service, caplog):
    host_service("ok")
    body = json.dumps({"id": 1, "v": 1, "op": "recognize",
                       "params": {"image": png(), "mode": "reaction"}}).encode()
    frame = struct.pack("=I", len(body)) + body
    out = io.BytesIO()
    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        assert host.serve(io.BytesIO(frame), out, HomeChoice(Path("Z:/none"), "host-config")) == 0
    assert "op=recognize id=1 ok" in caplog.text
    for forbidden in ("CCO", "NaBH4", "V2000", png()[:40]):
        assert forbidden not in caplog.text


def test_a_changed_registry_configuration_does_not_stop_capture_requests(host_service):
    host_service("ok")
    choice = HomeChoice(Path("Z:/one"), "host-config")
    changed = HomeChoice(Path("Z:/two"), "host-config")

    def frame(op, params):
        body = json.dumps({"id": 1, "v": 1, "op": op, "params": params}).encode()
        return struct.pack("=I", len(body)) + body

    out = io.BytesIO()
    data = frame("recognition_status", {}) + frame("describe_structures",
                                                   {"structures": [{"smiles": "CC"}]})
    assert host.serve(io.BytesIO(data), out, choice, resolve=lambda: changed) == 0
    answers = []
    raw = out.getvalue()
    while raw:
        (n,) = struct.unpack_from("=I", raw)
        answers.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    assert [a["ok"] for a in answers] == [True, True]
    out = io.BytesIO()
    assert host.serve(io.BytesIO(frame("ping", {})), out, choice, resolve=lambda: changed) == 0
    assert json.loads(out.getvalue()[4:])["error"]["code"] == "registry_changed"


def frames_of(raw: bytes) -> list[dict]:
    answers = []
    while raw:
        (n,) = struct.unpack_from("=I", raw)
        answers.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    return answers


def test_short_capture_requests_are_answered_while_a_recognize_runs(host_service, caplog):
    """A recognition takes 10 to 60 seconds, minutes with the first model
    load; the extension gives the other capture operations 30 seconds. They
    must not wait behind it (a second capture page, an edited molecule)."""
    host_service("slow_load")                                   # recognize waits 1 s for the models

    def frame(request_id, op, params):
        body = json.dumps({"id": request_id, "v": 1, "op": op, "params": params}).encode()
        return struct.pack("=I", len(body)) + body

    data = (frame("slow", "recognize", {"image": png(), "mode": "reaction"})
            + frame("status", "recognition_status", {"start": True})
            + frame("describe", "describe_structures", {"structures": [{"smiles": "OCC"}]})
            + frame("bad", "recognize", {"image": "AAAA", "mode": "reaction"}))
    out = io.BytesIO()
    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        assert host.serve(io.BytesIO(data), out, HomeChoice(Path("Z:/none"), "host-config")) == 0
    answers = frames_of(out.getvalue())
    order = [a["id"] for a in answers]
    assert sorted(order) == ["bad", "describe", "slow", "status"]
    assert order.index("status") < order.index("slow")
    assert order.index("describe") < order.index("slow")
    by_id = {a["id"]: a for a in answers}
    assert by_id["status"]["result"]["state"] == "loading"
    assert by_id["describe"]["result"] == {"structures": [{"smiles": "CCO", "warnings": []}]}
    assert by_id["slow"]["result"]["reactions"][0]["reaction_smiles"] == "CC=O>>CCO"
    assert by_id["bad"]["error"]["code"] == "image_invalid"
    assert "op=recognize id='slow' ok" in caplog.text           # written before the loop ended
    assert threading.active_count() < 20


def test_many_recognize_requests_at_once_are_all_answered(host_service):
    host_service("ok")

    def frame(request_id):
        body = json.dumps({"id": request_id, "v": 1, "op": "recognize",
                           "params": {"image": png(), "mode": "molecule"}}).encode()
        return struct.pack("=I", len(body)) + body

    count = host.MAX_RECOGNITIONS_ASIDE + 3
    out = io.BytesIO()
    data = b"".join(frame(n) for n in range(count))
    assert host.serve(io.BytesIO(data), out, HomeChoice(Path("Z:/none"), "host-config")) == 0
    answers = frames_of(out.getvalue())
    assert sorted(a["id"] for a in answers) == list(range(count))
    assert all(a["ok"] for a in answers)


def test_the_host_stops_the_worker_when_it_ends(host_service, monkeypatch):
    r = host_service("ok")
    r.recognize(png(), "reaction")
    run = r.worker._running
    host._stop_recognition()
    assert run.proc.poll() is not None and recognition._default is None


def test_a_large_recognize_answer_fits_the_frame(host_service):
    """The largest allowed number of molecules stays far below the limit."""
    raw = {"width": 6000, "height": 100, "reactions": [],
           "molecules": [{"box": [i * 90, 0, i * 90 + 80, 90],
                          "smiles": "CC(C)(C)OC(=O)N1CCC[C@H]1C(=O)Nc1ccc(cc1)C(F)(F)F" + "C" * i,
                          "molfile": None, "confidence": 0.9}
                         for i in range(protocol.MAX_RECOGNITION_MOLECULES)]}
    result = recognition.build_result(raw, "reaction", 1)
    assert len(result["molecules"]) == protocol.MAX_RECOGNITION_MOLECULES
    encoded = host.encode_response(protocol.ok_response(1, result), "recognize")
    assert json.loads(encoded)["ok"] is True and len(encoded) < protocol.MAX_RESPONSE_BYTES


# --------------------------------------------------------------------------- the real host process

def test_a_recognize_is_answered_while_the_host_waits_for_the_next_frame(tmp_path):
    """A real host process on real pipes, the input left open. ``recognize``
    runs in a thread while the loop waits in a read on the input pipe. On
    Windows a library that touches the standard input during its import (numpy,
    first imported here in that thread) then blocks for good, unless the host
    keeps the pipe to itself (``host._isolate_stdio``)."""
    import os

    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(recognition.__file__).parents[1])!r})\n"
        "from compound_registry import host, recognition\n"
        f"recognition._default = recognition.Recognition(command={fake('ok')!r})\n"
        "assert 'numpy' not in sys.modules and 'rdkit' not in sys.modules\n"
        "sys.exit(host.main([]))\n")
    env = dict(os.environ)
    env["COMPOUND_REGISTRY_HOME"] = str(tmp_path / "no-registry")
    env["USERPROFILE"] = env["HOME"] = str(tmp_path / "user")
    body = json.dumps({"id": "one", "v": protocol.PROTOCOL_VERSION, "op": "recognize",
                       "params": {"image": png(), "mode": "reaction"}}).encode("utf-8")
    proc = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    got: list[bytes] = []

    def read() -> None:
        head = proc.stdout.read(4)
        if len(head) == 4:
            got.append(proc.stdout.read(struct.unpack("=I", head)[0]))

    reader = threading.Thread(target=read, daemon=True)
    try:
        reader.start()
        proc.stdin.write(struct.pack("=I", len(body)) + body)
        proc.stdin.flush()                                      # and the input stays open
        reader.join(90)
        assert got, "no answer while the host waits for the next frame"
        answer = json.loads(got[0])
        assert answer["id"] == "one" and answer["ok"] is True
        assert answer["result"]["reactions"][0]["reaction_smiles"] == "CC=O>>CCO"
        proc.stdin.close()
        assert proc.wait(30) == 0
    finally:
        proc.kill()
        proc.wait(10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()



def test_the_real_host_serves_capture_operations_without_a_registry(tmp_path):
    """``python -m compound_registry.host`` with NO registry and no recognition
    part: the capture operations answer, nothing but frames reaches stdout,
    and no registry folder appears."""
    import os

    env = dict(os.environ)
    env["COMPOUND_REGISTRY_HOME"] = str(tmp_path / "no-registry")
    env[recognition.HOME_ENV_VAR] = str(tmp_path / "no-recognition")
    env["USERPROFILE"] = env["HOME"] = str(tmp_path / "user")
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp_path)

    def frame(request_id, op, params):
        body = json.dumps({"id": request_id, "v": protocol.PROTOCOL_VERSION, "op": op,
                           "params": params}).encode("utf-8")
        return struct.pack("=I", len(body)) + body

    data = (frame(1, "recognition_status", {"start": True})
            + frame(2, "describe_structures", {"structures": [{"smiles": "OCC"},
                                                              {"molfile": molfile("C[C@H](N)O")}]})
            + frame(3, "recognize", {"image": png(), "mode": "reaction"})
            + frame(4, "recognize", {"image": "AAAA", "mode": "molecule"})
            + frame(5, "reaction_to_chemdraw", {"steps": [
                {"reactants": [molfile("CC=O")], "products": [molfile("CCO")],
                 "conditions": ["NaBH4"]}]})
            + frame(6, "ping", {}))
    done = subprocess.run([sys.executable, "-m", "compound_registry.host"], input=data,
                          capture_output=True, env=env, timeout=120)
    assert done.returncode == 0
    answers, raw = [], done.stdout
    while raw:
        (n,) = struct.unpack_from("=I", raw)
        answers.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    # ``recognize`` is answered beside the loop: its answer may come later.
    answers.sort(key=lambda a: a["id"])
    assert [a["id"] for a in answers] == [1, 2, 3, 4, 5, 6]
    assert answers[0]["result"]["state"] == "not_installed"
    assert answers[1]["result"] == {"structures": [
        {"smiles": "CCO", "warnings": []}, {"smiles": "C[C@H](N)O", "warnings": ["stereo"]}]}
    assert answers[2]["error"]["code"] == "recognition_not_installed"
    assert answers[3]["error"]["code"] == "image_invalid"
    written = Path(answers[4]["result"]["path"])
    assert written.is_file() and written.parent == tmp_path / "ChemLinker"
    assert answers[4]["result"]["opened"] is False
    assert answers[5]["error"]["code"] == "registry_missing"
    assert not (tmp_path / "no-registry").exists() and not (tmp_path / "no-recognition").exists()


# --------------------------------------------------------------------------- worker: no reaction found

class _FakeModel:
    def __init__(self, answer):
        self.answer = answer

    def predict_images(self, images, **_kwargs):
        return [self.answer for _ in images]


def _worker_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("recognition_worker_under_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("confidence, found", [(0.9, True), (0.0, False)])
def test_reaction_mode_reads_a_lone_structure_as_one_molecule(confidence, found):
    """A selection around one structure has no reaction: the whole picture is
    read as one molecule, unless that reading is the nonsense a blank picture
    gives (score 0)."""
    from types import SimpleNamespace

    from PIL import Image

    worker = _worker_module()
    models = SimpleNamespace(
        rxn=_FakeModel([]),
        molscribe=_FakeModel({"smiles": "CCO", "molfile": "M  END", "confidence": confidence}),
        ocr=None)
    raw = worker.recognize_reaction(models, Image.new("RGB", (40, 30), "white"))
    assert raw["reactions"] == [] and (raw["width"], raw["height"]) == (40, 30)
    if found:
        assert [m["smiles"] for m in raw["molecules"]] == ["CCO"]
        assert raw["molecules"][0]["box"] == [0, 0, 40, 30]
    else:
        assert raw["molecules"] == []


def test_the_worker_reports_the_rectangle_molscribe_looks_at():
    """MolScribe cuts the white border away (and puts 5 pixels back) before it
    reads a picture; its coordinates are fractions of that rectangle. The
    worker reports it as ``seen``, in pixels of the molecule's box."""
    import numpy as np
    from types import SimpleNamespace

    from PIL import Image

    worker = _worker_module()
    picture = np.full((60, 100, 3), 255, dtype=np.uint8)
    picture[20:35, 40:70] = 0                      # ink: rows 20 to 34, columns 40 to 69
    assert worker.seen_box(picture) == [35, 15, 75, 40]
    blank = np.full((60, 100, 3), 255, dtype=np.uint8)
    assert worker.seen_box(blank) == [-5, -5, 105, 65]       # nothing to cut away
    grey = np.full((60, 100, 3), 250, dtype=np.uint8)        # no pure white: nothing is cut
    assert worker.seen_box(grey) == [-5, -5, 105, 65]
    assert worker.seen_box(None) is None

    # Single molecule: the box is the whole picture.
    models = SimpleNamespace(
        rxn=_FakeModel([]), ocr=None,
        molscribe=_FakeModel({"smiles": "CCO", "molfile": "M  END", "confidence": 0.9}))
    raw = worker.recognize_molecule(models, Image.fromarray(picture))
    assert raw["molecules"][0]["box"] == [0, 0, 100, 60]
    assert raw["molecules"][0]["seen"] == [35, 15, 75, 40]

    # Reaction: one rectangle per molecule, in pixels of ITS box.
    layout = [{"reactants": [{"bbox": [0.3, 0.2, 0.9, 0.8], "category_id": 1}], "conditions": [],
               "products": []}]
    models = SimpleNamespace(
        rxn=_FakeModel(layout), ocr=None,
        molscribe=_FakeModel({"smiles": "CCO", "molfile": "M  END", "confidence": 0.9}))
    raw = worker.recognize_reaction(models, Image.fromarray(picture))
    assert raw["molecules"][0]["box"] == [30, 12, 90, 48]
    assert raw["molecules"][0]["seen"] == [5, 3, 45, 28]
