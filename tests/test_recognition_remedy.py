"""The worker's remedies for molecules RxnScribe misses: "peek" decoding and
"leftover ink" (``recognition_worker.py``, see its docstring and
tools/recognition-e2e/missed/DECISION.md).

No torch here: the decoding runs on a scripted fake model and a small copy of
RxnScribe's token grammar. The image geometry needs OpenCV, which only the
recognition environment has: that test runs a script under its Python and is
skipped when the optional part is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from compound_registry import recognition

WORKER = Path(recognition.__file__).with_name("recognition_worker.py")
GEOMETRY = Path(__file__).parent / "data" / "leftover_ink_picture.py"


def _worker_module():
    spec = importlib.util.spec_from_file_location("recognition_worker_remedy_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def worker():
    return _worker_module()


# --------------------------------------------------------------------------- a small RxnScribe grammar

class Grammar:
    """RxnScribe's ReactionTokenizer (pix2seq form) with 100 coordinate bins:
    x tokens 0-99, y tokens 100-199, then the special tokens; the end token is
    the second last of the vocabulary, as in RxnScribe."""

    def __init__(self):
        names = ["<pad>", "<sos>", "<eos>", "<unk>", "<mask>", "[Rxn]", "[Rct]", "[Prd]", "[Cnd]", "[Idt]",
                 "[Mol]", "[Txt]", "[Sup]", "[Nos]"]
        self.size = 216
        self.stoi = {name: 200 + i for i, name in enumerate(names)}
        self.stoi["<eos>"] = self.size - 2
        self.itos = {v: k for k, v in self.stoi.items()}
        self.EOS_ID = self.stoi["<eos>"]
        self.max_len = 256
        self.category = {"[Mol]": 1, "[Txt]": 2, "[Idt]": 3, "[Sup]": 4}

    def is_x(self, idx):
        return 0 <= idx < 100

    def update_state(self, state, idx):
        if state is None:
            return ("[Rxn]", "e")
        step = {"x1": "y1", "y1": "x2", "x2": "y2", "y2": "c"}
        if state[1] in step:
            return (state[0], step[state[1]])
        if state[1] == "c":
            return (state[0], "x1") if self.is_x(idx) else (state[0], "e")
        if state[0] == "[Rct]":
            return ("[Cnd]", "x1") if self.is_x(idx) else ("[Cnd]", "e")
        if state[0] == "[Cnd]":
            return ("[Prd]", "x1")
        if state[0] == "[Prd]":
            return ("[Rxn]", "e")
        if state[0] == "[Rxn]":
            return ("[Rct]", "x1") if self.is_x(idx) else ("<eos>", "e")
        return ("<eos>", "e")

    def output_mask(self, state):
        mask = np.ones(self.size, dtype=bool)
        if state[1] in ("y1", "c"):
            mask[0:100] = False
        if state[1] in ("x1", "x2"):
            mask[100:200] = False
        if state[1] == "y2":
            for name in ("[Idt]", "[Mol]", "[Txt]", "[Sup]"):
                mask[self.stoi[name]] = False
        if state[1] == "c":
            mask[self.stoi[state[0]]] = False
        if state[1] == "e":
            if state[0] in ("[Rct]", "[Cnd]", "[Rxn]"):
                mask[0:100] = False
            if state[0] == "[Rct]":
                mask[self.stoi["[Cnd]"]] = False
            if state[0] == "[Prd]":
                mask[self.stoi["[Rxn]"]] = False
                mask[self.stoi["[Nos]"]] = False
            if state[0] in ("[Rxn]", "<eos>"):
                mask[self.EOS_ID] = False
        return mask

    def update_states_and_masks(self, states, ids):
        new = [self.update_state(s, i) for s, i in zip(states, ids)]
        return new, np.array([self.output_mask(s) for s in new])

    def sequence_to_bbox(self, seq, scale=(1, 1)):
        if len(seq) < 5:
            return None
        x1, x2 = seq[0] / 99 / scale[0], seq[2] / 99 / scale[0]
        y1, y2 = (seq[1] - 100) / 99 * scale[1], (seq[3] - 100) / 99 * scale[1]
        if not (self.is_x(seq[0]) and self.is_x(seq[2]) and 100 <= seq[1] < 200 and 100 <= seq[3] < 200) \
                or x1 >= x2 or y1 >= y2 or seq[4] not in self.itos:
            return None
        name = self.itos[seq[4]]
        if name not in self.category:
            return None
        return {"category": name, "bbox": (x1, y1, x2, y2), "category_id": self.category[name]}

    def sequence_to_data(self, seq, scores=None, scale=(1, 1)):
        reactions, current, flag, i = [], {"reactants": [], "conditions": [], "products": []}, "reactants", 0
        while i < len(seq):
            if seq[i] == self.EOS_ID:
                break
            if seq[i] in self.itos:
                name = self.itos[seq[i]]
                if name in ("[Rxn]", "[Nos]"):
                    if current["reactants"] and current["products"]:
                        reactions.append(current)
                    current, flag = {"reactants": [], "conditions": [], "products": []}, "reactants"
                elif name == "[Rct]":
                    flag = "conditions"
                elif name == "[Cnd]":
                    flag = "products"
                elif name == "[Prd]":
                    flag = None
            elif i + 5 <= len(seq) and flag is not None:
                box = self.sequence_to_bbox(seq[i:i + 5], scale)
                if box is not None:
                    current[flag].append(box)
                    i += 4
            i += 1
        return reactions


G = Grammar()


def box(x1, y1, x2, y2, kind):
    return [x1, 100 + y1, x2, 100 + y2, G.stoi[kind]]


def T(name):
    return G.stoi[name]


class ScriptedModel:
    """A decoder that knows one main sequence and, at one place, the
    alternative it would choose with the best token forbidden.

    ``advance(state, token)``: the state is the tuple of tokens fed so far."""

    def __init__(self, main, side_at=None, side=(), gap=11.9):
        self.main, self.side_at, self.side, self.gap = list(main), side_at, list(side), gap
        self.steps = 0

    def log_probs(self, history):
        out = np.full(G.size, -30.0, dtype=np.float32)
        n = len(history)
        if self.side_at is not None and n >= self.side_at \
                and history[:self.side_at] == tuple(self.main[:self.side_at]) \
                and history[self.side_at:] == tuple(self.side[:n - self.side_at]) and n > self.side_at:
            if n - self.side_at < len(self.side):
                out[self.side[n - self.side_at]] = -0.01
            return out
        if history == tuple(self.main[:n]):
            out[self.main[n] if n < len(self.main) else G.EOS_ID] = -0.01
            if n == self.side_at and self.side:
                out[self.side[0]] = -self.gap
        return out

    def advance(self, state, token):
        self.steps += 1
        history = () if token is None else state + (token,)
        return self.log_probs(history), history

    def rows(self, seq):
        return [self.log_probs(tuple(seq[:i])) for i in range(len(seq) + 1)]


# The user's scheme: amine + glyoxal, text at the arrow, the label "1" as the
# only box of the product part; the product itself is the alternative.
AMINE, GLYOXAL = box(1, 17, 18, 80, "[Mol]"), box(28, 34, 37, 63, "[Mol]")
TEXT, LABEL = box(41, 42, 52, 49, "[Txt]"), box(55, 50, 57, 59, "[Idt]")
PRODUCT = box(59, 6, 97, 91, "[Mol]")
MAIN = AMINE + GLYOXAL + [T("[Rct]")] + TEXT + [T("[Cnd]")] + LABEL + [T("[Prd]"), T("[Rxn]")]
CLOSE_AT = MAIN.index(T("[Prd]"))


# --------------------------------------------------------------------------- peek decoding

def test_the_main_decoding_is_plain_greedy_decoding(worker):
    plain = AMINE + GLYOXAL + [T("[Rct]")] + TEXT + [T("[Cnd]")] + PRODUCT + [T("[Prd]"), T("[Rxn]")]
    model = ScriptedModel(plain)
    decoded = worker.decode_peek(model.advance, G)
    assert decoded["seq"] == plain and decoded["eos"] is True and decoded["peeks"] == []
    assert worker.main_sequence_holds(model.rows(plain), plain, G)
    assert worker.splice(decoded, G, (1, 1))[0] == plain


def test_a_side_branch_without_a_usable_box_changes_nothing(worker):
    # The model has no alternative to closing the part: the side branch names
    # nonsense boxes, which are refused.
    decoded = worker.decode_peek(ScriptedModel(MAIN).advance, G)
    assert decoded["seq"] == MAIN and len(decoded["peeks"]) == 1
    seq, scores, accepted = worker.splice(decoded, G, (1, 1))
    assert seq == MAIN and accepted == [] and len(scores) == len(MAIN)


def test_peek_runs_where_a_product_part_without_molecule_is_closed_and_inserts_the_box(worker):
    model = ScriptedModel(MAIN, side_at=CLOSE_AT, side=PRODUCT)
    decoded = worker.decode_peek(model.advance, G)
    assert decoded["seq"] == MAIN                       # the main sequence is untouched
    assert [p["at"] for p in decoded["peeks"]] == [CLOSE_AT]
    assert [b["tokens"] for b in decoded["peeks"][0]["boxes"]] == [PRODUCT]
    assert decoded["peeks"][0]["boxes"][0]["min_score"] == pytest.approx(-11.9, abs=0.01)
    seq, scores, accepted = worker.splice(decoded, G, (1, 1))
    assert seq == MAIN[:CLOSE_AT] + PRODUCT + MAIN[CLOSE_AT:] and len(scores) == len(seq)
    assert [b["category"] for b in accepted] == ["[Mol]"]
    reactions = G.sequence_to_data(seq, scores)
    assert [b["category"] for b in reactions[0]["products"]] == ["[Idt]", "[Mol]"]


def test_no_peek_when_the_product_part_holds_a_molecule(worker):
    main = AMINE + [T("[Rct]"), T("[Cnd]")] + PRODUCT + LABEL + [T("[Prd]"), T("[Rxn]")]
    model = ScriptedModel(main, side_at=main.index(T("[Prd]")), side=GLYOXAL)
    assert worker.decode_peek(model.advance, G)["peeks"] == []


def test_no_peek_when_the_model_does_not_close_the_part(worker):
    # The label is followed by a molecule box anyway: nothing to look for.
    main = AMINE + [T("[Rct]"), T("[Cnd]")] + LABEL + PRODUCT + [T("[Prd]"), T("[Rxn]")]
    assert worker.decode_peek(ScriptedModel(main).advance, G)["peeks"] == []


def test_no_peek_in_the_reactant_part(worker):
    # A reactant part holding only a label is left alone (leftover ink covers it).
    main = LABEL + [T("[Rct]"), T("[Cnd]")] + PRODUCT + [T("[Prd]"), T("[Rxn]")]
    model = ScriptedModel(main, side_at=main.index(T("[Rct]")), side=AMINE)
    assert worker.decode_peek(model.advance, G)["peeks"] == []


def test_peek_switched_off_gives_the_same_sequence(worker):
    model = ScriptedModel(MAIN, side_at=CLOSE_AT, side=PRODUCT)
    decoded = worker.decode_peek(model.advance, G, peek=0)
    assert decoded["seq"] == MAIN and decoded["peeks"] == []


def test_a_side_box_overlapping_a_molecule_already_found_is_refused(worker):
    near_amine = box(2, 18, 19, 79, "[Mol]")
    decoded = worker.decode_peek(ScriptedModel(MAIN, side_at=CLOSE_AT, side=near_amine).advance, G)
    assert len(decoded["peeks"]) == 1
    seq, _scores, accepted = worker.splice(decoded, G, (1, 1))
    assert seq == MAIN and accepted == []
    # A box covered for half or more by one already found is refused too.
    inside = box(3, 30, 12, 60, "[Mol]")
    decoded = worker.decode_peek(ScriptedModel(MAIN, side_at=CLOSE_AT, side=inside).advance, G)
    assert worker.splice(decoded, G, (1, 1))[2] == []


def test_only_a_molecule_box_is_inserted_and_at_most_two_boxes_are_named(worker):
    other_label = box(60, 60, 62, 70, "[Idt]")
    side = other_label + PRODUCT
    decoded = worker.decode_peek(ScriptedModel(MAIN, side_at=CLOSE_AT, side=side).advance, G)
    assert [b["tokens"] for b in decoded["peeks"][0]["boxes"]] == [other_label, PRODUCT]
    seq, _scores, accepted = worker.splice(decoded, G, (1, 1))
    assert seq == MAIN[:CLOSE_AT] + PRODUCT + MAIN[CLOSE_AT:] and len(accepted) == 1
    # Two boxes that are not molecules: the side branch stops, nothing is inserted.
    side = other_label + box(63, 60, 65, 70, "[Txt]") + PRODUCT
    decoded = worker.decode_peek(ScriptedModel(MAIN, side_at=CLOSE_AT, side=side).advance, G)
    assert len(decoded["peeks"][0]["boxes"]) == 2 and decoded["side_steps"] == 10
    assert worker.splice(decoded, G, (1, 1))[0] == MAIN


def test_the_check_notices_a_sequence_that_greedy_decoding_would_not_give(worker):
    model = ScriptedModel(MAIN)
    rows = model.rows(MAIN)
    assert worker.main_sequence_holds(rows, MAIN, G)
    other = list(MAIN)
    other[0] = other[0] + 1
    assert not worker.main_sequence_holds(rows, other, G)
    rows[-1] = rows[-1].copy()
    rows[-1][G.EOS_ID] = -50.0                           # the model would not end there
    rows[-1][0] = -0.01
    assert not worker.main_sequence_holds(rows, MAIN, G)
    assert not worker.main_sequence_holds(rows[:3], MAIN, G)


def _fake_rxn(answer, tokenizer=G):
    calls = []

    def predict_images(pictures, **kwargs):
        calls.append(kwargs)
        return [answer]

    return SimpleNamespace(tokenizer={"reaction": tokenizer}, predict_images=predict_images, calls=calls)


def _decoder_for(model, check=None):
    def decoder(models, picture):
        return (model.advance, check or model.rows, (1, 1),
                lambda seq, scores, scale: G.sequence_to_data(seq, scores, scale))
    return decoder


def test_layout_uses_the_peek_decoding_when_it_checks_out(worker, monkeypatch):
    model = ScriptedModel(MAIN, side_at=CLOSE_AT, side=PRODUCT)
    monkeypatch.setattr(worker, "_decoder", _decoder_for(model))
    rxn = _fake_rxn("predict_images must not be used")
    reactions, peek_boxes, info = worker.layout(SimpleNamespace(rxn=rxn), None)
    assert rxn.calls == [] and info == {"peek_triggers": 1, "peek_added": 1}
    assert [b["category"] for b in reactions[0]["products"]] == ["[Idt]", "[Mol]"]
    assert peek_boxes == {tuple(reactions[0]["products"][1]["bbox"])}


@pytest.mark.parametrize("trouble", ["mismatch", "error", "no torch"])
def test_layout_falls_back_to_predict_images(worker, monkeypatch, capfd, trouble):
    model = ScriptedModel(MAIN, side_at=CLOSE_AT, side=PRODUCT)
    if trouble == "mismatch":
        wrong = model.rows(MAIN)
        wrong[2] = np.full(G.size, -30.0, dtype=np.float32)
        wrong[2][5] = -0.01
        monkeypatch.setattr(worker, "_decoder", _decoder_for(model, check=lambda seq: wrong))
    elif trouble == "error":
        def broken(models, picture):
            raise RuntimeError("Na2SO4 read from the picture")
        monkeypatch.setattr(worker, "_decoder", broken)
    # "no torch": the real _decoder, and torch is not in this environment.
    answer = [{"reactants": [], "conditions": [], "products": []}]
    rxn = _fake_rxn(answer)
    reactions, peek_boxes, info = worker.layout(SimpleNamespace(rxn=rxn), None)
    assert reactions is answer and peek_boxes == set() and info == {"peek": "off"}
    assert rxn.calls == [{"molscribe": False, "ocr": False}]
    err = capfd.readouterr().err
    assert err.count("\n") == 1 and "peek decoding skipped (" in err
    assert "Na2SO4" not in err


def test_peek_is_off_when_rxnscribe_is_not_the_commit_it_was_written_for(worker, monkeypatch, capfd):
    def must_not_run(models, picture):
        raise AssertionError("the peek decoding ran")

    monkeypatch.setattr(worker, "_decoder", must_not_run)
    answer = [{"reactants": [], "conditions": [], "products": []}]
    rxn = _fake_rxn(answer)
    reactions, peek_boxes, info = worker.layout(SimpleNamespace(rxn=rxn, peek=False), None)
    assert reactions is answer and peek_boxes == set() and info == {"peek": "off"}
    assert rxn.calls == [{"molscribe": False, "ocr": False}]
    assert capfd.readouterr().err == ""                 # said once, when the models are loaded


@pytest.mark.parametrize("direct_url, pinned", [
    ('{"url": "https://github.com/thomas0809/RxnScribe/archive/'
     'ad6b1c75d40e563e68deca0491918885948d69c7.zip", "archive_info": {}}', True),
    ('{"url": "https://github.com/thomas0809/RxnScribe/archive/'
     '0123456789abcdef0123456789abcdef01234567.zip", "archive_info": {}}', False),
    ('{"url": "https://github.com/thomas0809/RxnScribe.git", "vcs_info": {"vcs": "git"}}', False),
    ('{"url": "file:///C:/src/RxnScribe", "dir_info": {"editable": true}}', False),
    ("not json", False), ("[]", False), ('{"url": 5}', False),
])
def test_the_rxnscribe_commit_is_read_from_its_direct_url(worker, direct_url, pinned):
    assert worker.rxnscribe_is_pinned(direct_url) is pinned


def test_without_rxnscribe_installed_peek_counts_as_not_pinned(worker):
    # The registry's environment has no RxnScribe.
    assert worker.rxnscribe_is_pinned() is False


def test_the_commit_of_peek_is_the_pinned_rxnscribe(worker):
    from compound_registry import recognition_install

    text = recognition_install.requirements_path().read_text(encoding="utf-8")
    (line,) = [line for line in text.splitlines() if line.startswith("rxnscribe @ ")]
    assert line.endswith("/" + worker.RXNSCRIBE_COMMIT + ".zip")


def test_a_sequence_that_never_ends_gives_nothing_like_predict_images(worker, monkeypatch):
    endless = (AMINE + [T("[Rct]"), T("[Cnd]")] + PRODUCT + [T("[Prd]"), T("[Rxn]")]) * 30
    model = ScriptedModel(endless[:256])
    reactions, peek_boxes, info = worker.peek_layout(
        G, model.advance, model.rows, (1, 1), lambda seq, scores, scale: G.sequence_to_data(seq, scores, scale))
    assert reactions == [] and peek_boxes == set()


# --------------------------------------------------------------------------- the worker's answer

class FakeMolScribe:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def predict_images(self, images, **kwargs):
        self.calls.append(len(images))
        return [self.answers[min(i, len(self.answers) - 1)] for i in range(len(images))]


class FakeOcr:
    def readtext(self, crop, detail=1):
        return [([[0, 0], [1, 0], [1, 1], [0, 1]], "Na2SO4/HCOOH", 0.9)]


def _picture(width=400, height=100):
    from PIL import Image

    return Image.new("RGB", (width, height), "white")


def _models(layout_answer, mol_answers=({"smiles": "CCO", "molfile": "M  END", "confidence": 0.9},)):
    return SimpleNamespace(rxn=_fake_rxn(layout_answer), molscribe=FakeMolScribe(list(mol_answers)),
                           ocr=FakeOcr())


def _box(x0, y0, x1, y1, category):
    return {"bbox": (x0, y0, x1, y1), "category_id": category}


USER_LAYOUT = [{"reactants": [_box(0.01, 0.17, 0.18, 0.8, 1), _box(0.28, 0.34, 0.37, 0.63, 1)],
                "conditions": [_box(0.41, 0.42, 0.52, 0.49, 2)],
                "products": [_box(0.55, 0.5, 0.57, 0.59, 3)]}]


def test_the_peek_molecule_is_a_product_and_marked(worker, monkeypatch):
    model = ScriptedModel(MAIN, side_at=CLOSE_AT, side=PRODUCT)
    monkeypatch.setattr(worker, "_decoder", _decoder_for(model))
    monkeypatch.setattr(worker, "leftover_ink", lambda *args: [])
    raw = worker.recognize_reaction(_models("unused"), _picture())
    assert [m["source"] for m in raw["molecules"]] == ["layout", "layout", "peek"]
    reaction = raw["reactions"][0]
    assert (reaction["reactants"], reaction["agents"], reaction["products"]) == ([0, 1], [], [2])
    assert [t["lines"] for t in reaction["texts"]] == [["Na2SO4/HCOOH"]]
    assert raw["timing"]["peek_added"] == 1


def test_leftover_molecules_get_the_role_the_geometry_gives(worker, monkeypatch, capfd):
    seen = {}

    def leftover(models, image, boxes, context):
        seen.update(boxes=boxes, context=context)
        return [({"box": [300, 5, 395, 95], "smiles": "c1ccccc1", "molfile": None, "confidence": 0.88,
                  "seen": [1, 1, 90, 90], "source": "leftover"}, {"role": "product", "reaction": 0}),
                ({"box": [5, 80, 30, 99], "smiles": "CCN", "molfile": None, "confidence": 0.8,
                  "seen": None, "source": "leftover"}, {"role": None, "reaction": None})]

    monkeypatch.setattr(worker, "leftover_ink", leftover)
    raw = worker.recognize_reaction(_models(USER_LAYOUT), _picture())
    # Every box of the layout, of every kind, is ink the remedy must not read again.
    def px(b):
        x0, y0, x1, y1 = b["bbox"]
        return [int(x0 * 400), int(y0 * 100), int(x1 * 400), int(y1 * 100)]

    reaction = USER_LAYOUT[0]
    assert seen["boxes"] == [px(b) for b in reaction["reactants"] + reaction["conditions"] + reaction["products"]]
    assert seen["context"] == [{role: [px(b) for b in reaction[role]]
                                for role in ("reactants", "conditions", "products")}]
    assert [m["source"] for m in raw["molecules"]] == ["layout", "layout", "leftover", "leftover"]
    reaction = raw["reactions"][0]
    assert (reaction["reactants"], reaction["agents"], reaction["products"]) == ([0, 1], [], [2])
    # The one without a role is in no reaction: the host reports it as unassigned.
    assert raw["timing"]["leftover"] == 2
    assert "leftover" not in capfd.readouterr().err


def test_leftover_ink_never_fails_the_picture(worker, monkeypatch, capfd):
    def broken(models, image, boxes, context):
        raise ValueError("O=CC=O read from the picture")

    monkeypatch.setattr(worker, "leftover_ink", broken)
    raw = worker.recognize_reaction(_models(USER_LAYOUT), _picture())
    assert len(raw["molecules"]) == 2 and raw["reactions"][0]["reactants"] == [0, 1]
    assert raw["reactions"][0]["texts"][0]["lines"] == ["Na2SO4/HCOOH"]
    err = capfd.readouterr().err
    assert "leftover ink skipped (ValueError)" in err and "O=CC=O" not in err


def test_without_opencv_leftover_ink_is_skipped_quietly(worker):
    # The registry's environment has no OpenCV: the real function fails on
    # its import, and the picture still gets its answer.
    raw = worker.recognize_reaction(_models(USER_LAYOUT), _picture())
    assert len(raw["molecules"]) == 2 and raw["timing"]["leftover"] == 0


def test_no_box_at_all_reads_the_whole_picture_and_not_the_leftover_ink(worker, monkeypatch):
    called = []
    monkeypatch.setattr(worker, "leftover_ink", lambda *args: called.append(1) or [])
    models = _models([])
    raw = worker.recognize_reaction(models, _picture(40, 30))
    assert called == [] and raw["reactions"] == []
    assert [(m["smiles"], m["box"], m["source"]) for m in raw["molecules"]] == [("CCO", [0, 0, 40, 30], "picture")]
    assert models.molscribe.calls == [1]


TEXT_ONLY = [{"reactants": [_box(0.1, 0.1, 0.3, 0.3, 2)], "conditions": [], "products": [_box(0.6, 0.1, 0.7, 0.3, 3)]}]


def test_boxes_without_molecules_try_leftover_ink_first(worker, monkeypatch):
    found = ({"box": [250, 10, 390, 90], "smiles": "c1ccccc1", "molfile": None, "confidence": 0.9,
              "seen": None, "source": "leftover"}, {"role": "product", "reaction": 0})
    monkeypatch.setattr(worker, "leftover_ink", lambda *args: [found])
    models = _models(TEXT_ONLY)
    raw = worker.recognize_reaction(models, _picture())
    assert [m["source"] for m in raw["molecules"]] == ["leftover"]
    assert raw["reactions"][0]["products"] == [0]
    assert models.molscribe.calls == []                 # the whole picture was not read


def test_boxes_without_molecules_and_no_leftover_read_the_whole_picture(worker, monkeypatch):
    monkeypatch.setattr(worker, "leftover_ink", lambda *args: [])
    models = _models(TEXT_ONLY)
    raw = worker.recognize_reaction(models, _picture())
    assert [(m["box"], m["source"]) for m in raw["molecules"]] == [([0, 0, 400, 100], "picture")]
    assert raw["reactions"] == [] and models.molscribe.calls == [1]
    # A blank or unreadable picture stays empty.
    models = _models(TEXT_ONLY, [{"smiles": "C", "molfile": None, "confidence": 0.0}])
    assert worker.recognize_reaction(models, _picture())["molecules"] == []


class SmallTextBlindOcr:
    """Like EasyOCR, which drops text smaller than 10 pixels: reads nothing in
    a low crop."""

    def __init__(self, fail=False):
        self.shapes = []
        self.fail = fail

    def readtext(self, crop, detail=1):
        self.shapes.append(crop.shape)
        if self.fail:
            raise RuntimeError("Na2SO4")
        return [] if crop.shape[0] < 20 else [([[0, 0], [1, 0], [1, 1], [0, 1]], "Na2SO4/HCOOH", 0.6)]


def test_a_small_text_box_is_read_again_framed_and_enlarged(worker):
    ocr = SmallTextBlindOcr()
    crop = np.full((11, 56, 3), 255, dtype=np.uint8)
    assert worker.read_text(ocr, crop) == ["Na2SO4/HCOOH"]
    assert ocr.shapes == [(11, 56, 3), ((11 + 2 * 5) * 3, (56 + 2 * 5) * 3, 3)]
    # Text that is read at once is not read twice.
    ocr = SmallTextBlindOcr()
    assert worker.read_text(ocr, np.full((24, 90, 3), 255, dtype=np.uint8)) == ["Na2SO4/HCOOH"]
    assert len(ocr.shapes) == 1
    # An OCR error never fails the picture.
    assert worker.read_text(SmallTextBlindOcr(fail=True), crop) == []


class ScriptedOcr:
    """Gives the first answer for the first read, the second for the next."""

    def __init__(self, first, second):
        self.answers = [first, second]
        self.calls = 0

    def readtext(self, crop, detail=1):
        answer = self.answers[min(self.calls, 1)]
        self.calls += 1
        return [([[0, 0], [1, 0], [1, 1], [0, 1]], text, 0.5) for text in answer]


@pytest.mark.parametrize("first, second, lines, reads", [
    # Blank strings are no text: the host would drop them, so read again.
    ([""], ["Na2SO4/HCOOH"], ["Na2SO4/HCOOH"], 2),
    (["  ", ""], ["", "Na2SO4", " "], ["Na2SO4"], 2),
    # A bare compound number from the second read is no condition.
    ([], ["1"], [], 2),
    ([""], [" 12 ", "2a"], [], 2),
    ([], ["1", "HCOOH"], ["1", "HCOOH"], 2),
    ([], ["2 h"], ["2 h"], 2),
    ([], ["rt"], ["rt"], 2),
    ([], [""], [], 2),
    # What the first read gives is kept as it is, numbers included.
    (["1"], ["unused"], ["1"], 1),
    (["", "Na2SO4"], ["unused"], ["", "Na2SO4"], 1),
])
def test_what_the_second_read_of_a_text_box_keeps(worker, first, second, lines, reads):
    ocr = ScriptedOcr(first, second)
    assert worker.read_text(ocr, np.full((11, 56, 3), 255, dtype=np.uint8)) == lines
    assert ocr.calls == reads


def test_the_condition_text_of_a_small_capture_reaches_the_answer(worker, monkeypatch):
    """The text box of the arrow is 7 pixels high here: read again, enlarged,
    instead of leaving the step without conditions."""
    monkeypatch.setattr(worker, "leftover_ink", lambda *args: [])
    models = _models(USER_LAYOUT)
    models.ocr = SmallTextBlindOcr()
    raw = worker.recognize_reaction(models, _picture())
    assert raw["reactions"][0]["texts"][0]["lines"] == ["Na2SO4/HCOOH"]
    assert len(models.ocr.shapes) == 2


# --------------------------------------------------------------------------- leftover ink: the rules

def _group(w, h, bonds=6, angles=3, small=0.0):
    return {"w_L": w, "h_L": h, "n_bonds": bonds, "n_angles": angles, "small_ink_frac": small}


@pytest.mark.parametrize("group, ok", [
    (_group(2.0, 2.0), True),                       # a ring
    (_group(7.7, 4.7), True),                       # the user's product, about
    (_group(1.19, 2.0), False),                     # too narrow
    (_group(1.5, 1.7), False),                      # too small
    (_group(6.5, 1.0), False),                      # a line of text
    (_group(8.0, 1.25), False),                     # aspect ratio above 6
    (_group(2.0, 2.0, bonds=2), False),             # too few strokes
    (_group(2.0, 2.0, angles=1), False),            # strokes in one direction only (a hatching, a table)
    (_group(2.0, 2.0, small=0.9), True),            # the small-ink filter is off
])
def test_the_shape_filter(worker, group, ok):
    assert worker.shape_ok(group) is ok


@pytest.mark.parametrize("smiles, ok", [
    ("c1ccccc1", True), ("O=CC=O", True), ("CC", False), ("C[Pb](C)C", False), ("CC[Se]C", True),
    ("not a structure", False), (None, False), ("", False)])
def test_the_chemistry_filter(worker, smiles, ok):
    assert worker.chem_ok(smiles) is ok


def test_a_picture_within_the_pixel_budget_is_searched_as_it_is(worker):
    image = np.zeros((1000, 4000, 3), dtype=np.uint8)
    boxes, context = [[1, 2, 3, 4]], [{"reactants": [[1, 2, 3, 4]], "conditions": [], "products": []}]
    same = worker._reduced(image, boxes, context, worker.LEFTOVER_MAX_PIXELS)
    assert same[0] is image and same[1] is boxes and same[2] is context and same[3:] == (1.0, 1.0)
    # Every measured picture (the largest 2820 x 772) is below the budget.
    assert worker.LEFTOVER_MAX_PIXELS >= 2820 * 772 and worker.LEFTOVER_MAX_PIXELS == 4_000_000


def test_the_operating_point_is_the_measured_one(worker):
    assert worker.LEFTOVER["a"] == 0.2 and worker.LEFTOVER["min_conf"] == 0.70
    assert (worker.LEFTOVER["min_short"], worker.LEFTOVER["min_long"], worker.LEFTOVER["max_aspect"]) == (1.2, 1.8, 6.0)
    assert (worker.LEFTOVER["min_bonds"], worker.LEFTOVER["min_angles"], worker.LEFTOVER["min_heavy"]) == (3, 2, 3)
    assert (worker.LEFTOVER["max_groups"], worker.LEFTOVER["max_new"], worker.LEFTOVER["max_gap"]) == (16, 8, 8.0)
    assert worker.PEEK_BOXES == 2 and worker.PEEK_MAX_OVERLAP == 0.5


ARROW = {"tail": [200.0, 100.0], "head": [300.0, 100.0]}
L = 20.0


@pytest.mark.parametrize("where, role", [
    ([320, 40, 460, 160], "product"),               # past the head
    ([40, 40, 180, 160], "reactant"),               # before the tail
    ([215, 60, 285, 92], "agent"),                  # along the arrow, above it
    ([215, 108, 285, 140], "agent"),                # along the arrow, below it
    ([215, 60, 235, 92], "agent"),                  # over the first half: still along it
    ([205, 300, 245, 340], "reactant"),             # along it but far below: by its centre
])
def test_the_role_comes_from_the_nearest_arrow(worker, where, role):
    assert worker._relation(ARROW, where, L)[0] == role
    assert worker.guess_role(L, [ARROW], where, [])["role"] == (role if where[1] < 200 else None)


def test_no_role_without_an_arrow_or_far_from_it(worker):
    assert worker.guess_role(L, [], [320, 40, 460, 160], []) == {"role": None, "reaction": None}
    far = [300 + 9 * L, 40, 400 + 9 * L, 160]
    assert worker.guess_role(L, [ARROW], far, [])["role"] is None


def test_the_reaction_is_the_one_on_the_matching_sides_of_the_arrow(worker):
    other = {"reactants": [[600, 300, 650, 350]], "conditions": [], "products": [[700, 300, 750, 350]]}
    this = {"reactants": [[40, 40, 180, 160]], "conditions": [[215, 60, 285, 92]], "products": [[302, 90, 310, 110]]}
    guess = worker.guess_role(L, [ARROW], [320, 40, 460, 160], [other, this])
    assert guess == {"role": "product", "reaction": 1}
    backwards = {"reactants": [[320, 40, 460, 160]], "conditions": [], "products": [[40, 40, 180, 160]]}
    assert worker.arrow_reaction(L, ARROW, [backwards]) is None


# --------------------------------------------------------------------------- leftover ink: the picture

def test_leftover_ink_on_a_drawn_scheme():
    """The real image geometry (OpenCV) of the worker, under the Python of the
    recognition environment, on a drawn scheme: arrow, a big drawing right of
    it, the digit "1", text."""
    state = recognition.install_state()
    if not state["installed"]:
        pytest.skip(f"the recognition part is not installed: {state['problem']}")
    python = recognition.env_python(Path(state["home"]))
    done = subprocess.run([str(python), "-I", str(GEOMETRY), str(WORKER)], capture_output=True, text=True,
                          timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert len(out["arrows"]) == 1
    arrow = out["arrows"][0]
    assert arrow["head"][0] > arrow["tail"][0] and abs(arrow["head"][1] - 150) < 3
    assert 30 < out["L"] < 55
    # Only the product drawing is left over and passes the shape filter.
    assert [g["shape_ok"] for g in out["groups"]] == [True]
    (found,) = out["found"]
    x0, y0, x1, y1 = found["box"]
    assert x0 > 455 and x1 > 760 and y0 < 95 and y1 > 190
    assert found["guess"] == {"role": "product", "reaction": 0}
    assert found["source"] == "leftover" and found["smiles"] == "c1ccc2ccccc2c1"
    sx0, sy0, sx1, sy1 = found["seen"]
    assert 0 <= sx0 < sx1 <= x1 - x0 and 0 <= sy0 < sy1 <= y1 - y0
    assert out["molscribe_calls"] == 1
    assert out["found_low_confidence"] == 0             # MolScribe confidence below 0.70
    assert out["found_all_covered"] == 0                # nothing left over
    assert out["found_digit_uncovered"] == []           # a digit is no structure
    assert out["found_text_uncovered"] == []            # nor is a line of text
    assert out["no_arrow"] == [{"role": None, "reaction": None}]
    # Reduced to half the side: the same product, box and seen in the
    # picture's pixels, a crop of about half the size.
    (small,) = out["reduced"]
    assert small["guess"] == {"role": "product", "reaction": 0}
    assert all(abs(a - b) <= 6 for a, b in zip(small["box"], found["box"]))
    rx0, ry0, rx1, ry1 = small["box"]
    qx0, qy0, qx1, qy1 = small["seen"]
    assert 0 <= qx0 < qx1 <= rx1 - rx0 + 2 and 0 <= qy0 < qy1 <= ry1 - ry0 + 2
    # The same drawing inside: MolScribe's 5-pixel border is 10 pixels of the picture there.
    pad = 5
    assert abs((qx1 - qx0 - 4 * pad) - (sx1 - sx0 - 2 * pad)) <= 6
    assert abs((qy1 - qy0 - 4 * pad) - (sy1 - sy0 - 2 * pad)) <= 6
    (full_shape,), (small_shape,) = out["full_crop_shapes"][0], out["reduced_crop_shapes"][0]
    assert abs(small_shape[0] * 2 - full_shape[0]) <= 8 and abs(small_shape[1] * 2 - full_shape[1]) <= 8
    assert out["rxnscribe_pinned"] is True
