"""compound_registry.naming: what is sent to the naming program and which answers are stored.

The naming program is always a fake ``ask`` here; ChemDraw is never started.
"""

from __future__ import annotations

import json

import pytest
from rdkit import Chem

from compound_registry import chem, chemdraw, naming
from compound_registry.chem import _inchi, _parse_molblock
from compound_registry.chemdraw import ChemDrawAnswer, ChemDrawError
from compound_registry.naming import NameOutcome, NamingSkipped, check_answer, generate, prepare

AMINE_R = "C[C@H](N)c1ccccc1"
THREONINE_LIKE = "C[C@@H](O)[C@H](N)C(=O)O"

# A wedged molfile of (R)-1-phenylethan-1-amine (as the extension would send it).
AMINE_MOLFILE = Chem.MolToMolBlock(Chem.MolFromSmiles(AMINE_R))


def norm(smiles: str, mode: str | None = None):
    return chem.normalize_smiles(smiles, mode)


def counts_line(molfile: str) -> str:
    return next(line for line in molfile.splitlines() if line.rstrip().endswith("V2000"))


def chiral_flag(molfile: str) -> int:
    if "V3000" in molfile:
        counts = next(l for l in molfile.splitlines() if l.startswith("M  V30 COUNTS"))
        return int(counts.split()[-1])
    return int(counts_line(molfile)[12:15])


def stereo_groups(molfile: str) -> list[str]:
    return [l.split()[2].split("/")[1] for l in molfile.splitlines()
            if l.startswith("M  V30 MDLV30/STE")]


def reread_inchi(molfile: str) -> str:
    return _inchi(_parse_molblock(molfile))[0]


def answer_for(prepared, name: str, inchi: str | None = None) -> ChemDrawAnswer:
    return ChemDrawAnswer(name=name, inchi=prepared.inchi if inchi is None else inchi, atoms=9)


# --------------------------------------------------------------------------- prepare

SINGLE_CASES = [
    ("no stereo", "CCO", None, "none"),
    ("absolute", AMINE_R, None, "abs"),
    ("undefined override", AMINE_R, "undefined", "undefined"),
    ("flat drawing", "CC(N)c1ccccc1", None, "undefined"),
    ("partial", "C[C@@H](O)C(N)C(=O)O", None, "partial"),
    ("absolute, two centres", THREONINE_LIKE, None, "abs"),
]


@pytest.mark.parametrize("label,smiles,mode,expected_mode", SINGLE_CASES,
                         ids=[c[0] for c in SINGLE_CASES])
def test_single_stereoisomer_is_sent_as_absolute(label, smiles, mode, expected_mode):
    n = norm(smiles, mode)
    assert n.stereo_mode == expected_mode
    p = prepare(n.molblock, n.stereo_key)
    assert p.kind == "single"
    assert "V2000" in p.molfile and chiral_flag(p.molfile) == 1
    assert stereo_groups(p.molfile) == []
    # What is sent is exactly the registered stereoisomer.
    assert [[p.inchi]] == json.loads(n.stereo_key)
    assert reread_inchi(p.molfile) == p.inchi


def test_undefined_override_of_a_wedged_molfile_drops_the_wedges():
    n = chem.normalize(AMINE_MOLFILE, "undefined")
    assert n.stereo_mode == "undefined" and "/t" not in n.stereo_key
    p = prepare(n.molblock, n.stereo_key)
    assert p.kind == "single" and "/t" not in p.inchi
    assert reread_inchi(p.molfile) == p.inchi        # no wedge survives in the sent file
    assert chiral_flag(p.molfile) == 1


@pytest.mark.parametrize("smiles,mode,kind,group", [
    (AMINE_R, "rac", "rac", "STERAC1"),
    (AMINE_R, "rel", "rel", "STEREL1"),
    (THREONINE_LIKE, "rac", "rac", "STERAC1"),
    (THREONINE_LIKE, "rel", "rel", "STEREL1"),
    (THREONINE_LIKE + " |&1:1,3|", None, "rac", "STERAC1"),   # enhanced, over all centres
    (THREONINE_LIKE + " |o1:1,3|", None, "rel", "STEREL1"),
])
def test_racemic_and_relative_are_sent_with_one_group(smiles, mode, kind, group):
    n = norm(smiles, mode)
    assert n.stereo_mode == kind
    p = prepare(n.molblock, n.stereo_key)
    assert p.kind == kind
    assert "V3000" in p.molfile and chiral_flag(p.molfile) == 0
    assert stereo_groups(p.molfile) == [group]
    n_centres = 1 if smiles == AMINE_R else 2
    line = next(l for l in p.molfile.splitlines() if l.startswith("M  V30 MDLV30/STE"))
    assert f"ATOMS=({n_centres} " in line
    members = {m for g in json.loads(n.stereo_key) for m in g}
    assert p.inchi in members and len(members) == 2
    assert reread_inchi(p.molfile) == p.inchi


def test_mixed_groups_are_skipped():
    n = norm(THREONINE_LIKE + " |&1:1|")      # one racemic centre, one absolute
    assert n.stereo_mode == "mixed"
    with pytest.raises(NamingSkipped) as info:
        prepare(n.molblock, n.stereo_key)
    assert "mixed stereo groups" in info.value.message


def test_two_alternatives_of_mixtures_are_skipped():
    key = json.dumps([["InChI=1S/a", "InChI=1S/b"], ["InChI=1S/c", "InChI=1S/d"]])
    with pytest.raises(NamingSkipped) as info:
        prepare(norm(AMINE_R).molblock, key)
    assert "mixed stereo groups" in info.value.message


def test_atropisomer_keys_are_skipped():
    n = norm(AMINE_R)
    member = json.loads(n.stereo_key)[0][0]
    key = json.dumps([[member + "|atrop=1:2:+"]])
    with pytest.raises(NamingSkipped) as info:
        prepare(n.molblock, key)
    assert "chiral axis" in info.value.message


@pytest.mark.parametrize("key", ["", "not json", "[]", "[[]]", "{}", "[[1]]", "[\"x\"]", None])
def test_unreadable_stereo_keys_are_skipped(key):
    with pytest.raises(NamingSkipped) as info:
        prepare(norm(AMINE_R).molblock, key)
    assert "stereo description could not be read" in info.value.message


def test_a_drawing_that_does_not_match_its_key_is_skipped():
    other = norm("C[C@@H](N)c1ccccc1")        # the (S) key with the (R) drawing
    with pytest.raises(NamingSkipped) as info:
        prepare(norm(AMINE_R).molblock, other.stereo_key)
    assert "does not reproduce" in info.value.message


def test_an_unreadable_molfile_is_skipped():
    with pytest.raises(NamingSkipped) as info:
        prepare("not a molfile", norm(AMINE_R).stereo_key)
    assert "could not be read" in info.value.message


# --------------------------------------------------------------------------- check_answer

@pytest.fixture
def single():
    n = norm(AMINE_R)
    return prepare(n.molblock, n.stereo_key)


@pytest.fixture
def rac():
    n = norm(AMINE_R, "rac")
    return prepare(n.molblock, n.stereo_key)


@pytest.fixture
def rel():
    n = norm(AMINE_R, "rel")
    return prepare(n.molblock, n.stereo_key)


def test_accepts_matching_answers(single, rac, rel):
    assert check_answer(single, answer_for(single, "(R)-1-phenylethan-1-amine")) == \
        NameOutcome("(R)-1-phenylethan-1-amine", None, "ok")
    assert check_answer(rac, answer_for(rac, "rac-(R)-1-phenylethan-1-amine")).name == \
        "rac-(R)-1-phenylethan-1-amine"
    assert check_answer(rel, answer_for(rel, "REL-(R)-1-phenylethan-1-amine")).code == "ok"


def test_whitespace_in_the_name_is_collapsed(single):
    outcome = check_answer(single, answer_for(single, "  (R)-1-phenyl\n ethan-1-amine  "))
    assert outcome.name == "(R)-1-phenyl ethan-1-amine" and outcome.code == "ok"


def test_rejects_a_different_structure(single):
    outcome = check_answer(single, answer_for(single, "(R)-1-phenylethan-1-amine",
                                              inchi="InChI=1S/C8H11N/c1-7(9)8-5-3-2-4-6-8"))
    assert outcome.name is None and outcome.code == "rejected"
    assert "read the structure differently" in outcome.message
    assert check_answer(single, answer_for(single, "x", inchi="")).code == "rejected"


@pytest.mark.parametrize("which,name", [
    ("single", "rac-(R)-1-phenylethan-1-amine"),
    ("single", "rel-(R)-1-phenylethan-1-amine"),
    ("rac", "(R)-1-phenylethan-1-amine"),
    ("rac", "rel-(R)-1-phenylethan-1-amine"),
    ("rel", "(R)-1-phenylethan-1-amine"),
    ("rel", "rac-(R)-1-phenylethan-1-amine"),
])
def test_rejects_a_name_that_says_something_else_about_stereo(which, name, request):
    prepared = request.getfixturevalue(which)
    outcome = check_answer(prepared, answer_for(prepared, name))
    assert outcome.name is None and outcome.code == "rejected"
    assert "does not say the same about the stereo" in outcome.message


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_empty_name_is_no_name(single, name):
    outcome = check_answer(single, answer_for(single, name))
    assert outcome.name is None and outcome.code == "failed"
    assert "has no name for this structure" in outcome.message


def test_overlong_or_control_names_are_rejected(single):
    long = "x" * (naming.MAX_GENERATED_NAME + 1)
    assert check_answer(single, answer_for(single, long)).code == "rejected"
    assert check_answer(single, answer_for(single, "a\x07b")).code == "rejected"
    ok = "x" * naming.MAX_GENERATED_NAME
    assert check_answer(single, answer_for(single, ok)).name == ok


# --------------------------------------------------------------------------- generate

def test_generate_success_sends_the_prepared_file():
    n = norm(AMINE_R, "rac")
    sent: list[str] = []

    def ask(molfile: str) -> ChemDrawAnswer:
        sent.append(molfile)
        return ChemDrawAnswer("rac-(R)-1-phenylethan-1-amine", reread_inchi(molfile), 9)

    outcome = generate(n.molblock, n.stereo_key, ask=ask)
    assert outcome == NameOutcome("rac-(R)-1-phenylethan-1-amine", None, "ok", "ChemDraw")
    assert len(sent) == 1 and sent[0] == prepare(n.molblock, n.stereo_key).molfile


@pytest.mark.parametrize("code,expected", [("unavailable", "unavailable"),
                                           ("timeout", "failed"), ("failed", "failed")])
def test_generate_never_raises_on_chemdraw_errors(code, expected):
    def ask(molfile):
        raise ChemDrawError(code, f"message for {code}.")

    n = norm(AMINE_R)
    outcome = generate(n.molblock, n.stereo_key, ask=ask)
    assert outcome.name is None and outcome.code == expected
    assert outcome.message.startswith(f"message for {code}.")
    assert "type or paste" in outcome.message


def test_generate_never_raises_on_other_errors():
    def ask(molfile):
        raise RuntimeError("boom")

    n = norm(AMINE_R)
    outcome = generate(n.molblock, n.stereo_key, ask=ask)
    assert outcome.name is None and outcome.code == "failed" and "RuntimeError" in outcome.message


def test_generate_skips_without_asking():
    asked = []
    n = norm(AMINE_R)
    key = json.dumps([[json.loads(n.stereo_key)[0][0] + "|atrop=1:2:+"]])
    outcome = generate(n.molblock, key, ask=asked.append)
    assert outcome.name is None and outcome.code == "skipped" and "chiral axis" in outcome.message
    mixed = norm(THREONINE_LIKE + " |&1:1|")
    assert generate(mixed.molblock, mixed.stereo_key, ask=asked.append).code == "skipped"
    assert asked == []


def test_generate_reports_a_rejected_answer():
    n = norm(AMINE_R)
    outcome = generate(n.molblock, n.stereo_key,
                       ask=lambda m: ChemDrawAnswer("rac-(R)-x", reread_inchi(m), 9))
    assert outcome.name is None and outcome.code == "rejected"


def test_generate_with_preparation_crash(monkeypatch):
    def broken(molfile, key):
        raise ValueError("unexpected")

    monkeypatch.setattr(naming, "prepare", broken)
    outcome = generate("x", "[]", ask=lambda m: pytest.fail("must not ask"))
    assert outcome.name is None and outcome.code == "failed" and "ValueError" in outcome.message


def test_generate_without_ask_uses_chemdraw_which_is_switched_off_in_tests():
    n = norm(AMINE_R)
    outcome = generate(n.molblock, n.stereo_key)
    assert outcome.name is None and outcome.code == "unavailable"
    assert "ChemDraw was not found" in outcome.message


def test_generator_info(monkeypatch):
    assert naming.generator_info() == {"available": False, "label": None}
    monkeypatch.setattr(chemdraw, "available", lambda: True)
    assert naming.generator_info() == {"available": True, "label": "ChemDraw"}
