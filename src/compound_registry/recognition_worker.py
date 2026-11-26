#!/usr/bin/env python
"""Recognition worker: reads reaction schemes and molecules from pictures.

This ONE file is the whole worker. The native host starts it with the Python
of the recognition environment (NOT the registry's own environment)::

    <recognition home>/env/Scripts/python.exe -I recognition_worker.py --models <recognition home>/models

It must stay standalone: standard library at the top, the model libraries
(torch, MolScribe, RxnScribe, EasyOCR) imported only inside :func:`load_models`,
and it never imports ``compound_registry``. The host side is
``compound_registry/recognition.py``.

What it does
------------
RxnScribe finds the reactions of a scheme (boxes and their roles), MolScribe
turns every molecule box into a SMILES, a molfile and a confidence, EasyOCR
reads the text boxes. CPU only. Everything is read from ``--models``; the
network is never used (``HF_HUB_OFFLINE``, no EasyOCR download, no torchvision
download). The answer is RAW model output: the host turns it into the
protocol result with the registry's own RDKit.

Talking to the host: JSON lines
-------------------------------
One JSON object per line, UTF-8. The real stdout is reserved for these lines:
file descriptor 1 is pointed at stderr before any library is imported, so a
``print`` or a C-level message of a library cannot break the stream.

Worker to host::

    {"event": "hello", "worker": 1}                    # the process runs
    {"event": "ready", "load_seconds": 23.4}           # models loaded
    {"event": "failed", "stage": "load", "error": "ImportError"}   # then exit code 3
    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "stage": "layout", "error": "RuntimeError"}

Host to worker::

    {"id": 7, "op": "recognize", "mode": "reaction" | "molecule", "image": "<base64 of a PNG>"}
    {"op": "shutdown"}

``result`` of ``recognize``::

    {"width": 800, "height": 300,
     "molecules": [{"box": [x0, y0, x1, y1], "smiles": str | null,
                    "molfile": str | null, "confidence": float | null,
                    "seen": [x0, y0, x1, y1] | null}],
     "reactions": [{"reactants": [i, ...], "agents": [i, ...], "products": [i, ...],
                    "texts": [{"box": [...], "lines": [str, ...], "role": "conditions" | "reactants"}]}],
     "timing": {...}}

``seen`` is the part of the molecule's box that MolScribe looked at, in
pixels of the box (it cuts the white border away and puts 5 pixels back, so
the values may lie a little outside the box). The coordinates of its molfile
are fractions of THAT rectangle, but MolScribe scales them with the shape of
the whole box: a drawing with unequal white margins comes out squeezed. With
``seen`` the host puts every atom back where it is in the picture.

``i`` are positions in ``molecules`` (from 0). ``agents`` are the molecule
boxes RxnScribe gave the role "conditions". Mode ``molecule``: the whole
picture is one molecule and ``reactions`` is empty.

Every molecule of mode ``reaction`` also carries ``"source"``: ``"layout"``
(a box of RxnScribe), ``"peek"`` or ``"leftover"`` (recovered, see below), or
``"picture"`` (the whole picture read as one molecule). The host does not use
it (the protocol has no field for it); it is there for tests and tools.

Molecules RxnScribe misses
--------------------------
A digit printed in front of a molecule (the coefficient "1" between the arrow
head and the product) is sometimes read as an identifier box that takes the
molecule's place: the product part is closed with that label only, and the
molecule is never named. Two remedies, measured on 78 pictures with nothing
lost anywhere (``tools/recognition-e2e/missed/DECISION.md``):

1. **Peek** (:func:`layout`). RxnScribe's greedy decoding is re-implemented
   step by step (:func:`decode_peek`, the same operations as its own loop).
   When the model is about to close a product part that holds boxes but no
   molecule box, a side branch is decoded from the same state with the
   closing token forbidden, for at most :data:`PEEK_BOXES` boxes. A molecule
   box it names that overlaps no molecule box already found is inserted
   there (:func:`splice`); the main sequence goes on untouched. The
   re-implementation is written for ONE commit of RxnScribe
   (:data:`RXNSCRIBE_COMMIT`); with any other installed, peek is off and
   every picture is read with ``predict_images``. Before it is used, one
   decoder pass over the whole main sequence checks the step-by-step
   arithmetic (:func:`main_sequence_holds`); it replays the worker's own
   grammar masks, so it does NOT compare with ``predict_images`` (that is
   the live test's job). On a mismatch or any error the picture is read
   with ``predict_images``.
2. **Leftover ink** (:func:`leftover_ink`). The ink that no box of RxnScribe
   covers is grouped (scale: the bond length of the picture), groups that do
   not look like a drawn structure are dropped, MolScribe reads the rest and
   only confident, chemically plain readings are kept (:data:`LEFTOVER`). Each
   gets its role from where it lies relative to the nearest arrow
   (:func:`guess_role`); without one it is in no reaction (the host then
   reports it as unassigned). This step never fails a picture. A picture
   above :data:`LEFTOVER_MAX_PIXELS` is searched on a reduced copy (the
   measured pictures were all below it), so a large capture costs no more
   than a measured one. It was measured on dark-on-light pictures only.

Nothing found at all: a picture where RxnScribe returns no box is read as
one molecule (a selection around one structure). When RxnScribe returns
boxes but no molecule box, leftover ink runs first and the whole picture is
read as one molecule only when it finds nothing either.

A text box in which EasyOCR reads nothing (no line with a visible
character) is read once more with a white border and enlarged
(:func:`read_text`): small condition text used to come back empty, and the
host leaves out a text without lines. What that second read gives is kept
only when it is more than a bare compound number.

Errors carry the NAME of the exception class and the stage only, never its
message: a message could quote something read from the picture.

The worker ends when it is told to (``shutdown``), and at once when its stdin
closes (the host ended or was killed), also in the middle of a picture.
"""
import os
import sys
import time

_T_START = time.perf_counter()

WORKER_VERSION = 1
RXN_CHECKPOINT = "pix2seq_reaction_full.ckpt"
MOL_CHECKPOINT = "swin_base_char_aux_1m680k.pth"
#: Longest request line accepted (a picture is at most 900 000 base64 characters).
MAX_LINE_BYTES = 2_000_000


def _models_dir(argv):
    for i, arg in enumerate(argv):
        if arg == "--models" and i + 1 < len(argv):
            return os.path.abspath(argv[i + 1])
        if arg.startswith("--models="):
            return os.path.abspath(arg.split("=", 1)[1])
    return None


def _set_environment(models):
    """Must run before torch / huggingface_hub / easyocr are imported."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = os.path.join(models, "hf")
    os.environ["TORCH_HOME"] = os.path.join(models, "torch")
    os.environ["EASYOCR_MODULE_PATH"] = os.path.join(models, "easyocr")
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["MPLCONFIGDIR"] = os.path.join(models, "mpl")
    os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _protect_streams():
    """Returns ``(requests in, protocol lines out)`` as private binary streams.

    Afterwards file descriptor 1 goes to stderr and file descriptor 0 reads
    the null device, so no library can write into the protocol or touch the
    request pipe. The second matters on Windows: while one thread waits for a
    line on a pipe, any other call on the same handle (a library asking
    ``sys.stdin.isatty()`` during its import) blocks until a line arrives,
    i.e. for ever."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    try:
        os.dup2(2, 1)
    except OSError:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, 1)
        os.close(null_fd)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.close(null_fd)
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(out_fd, os.O_BINARY)
        msvcrt.setmode(in_fd, os.O_BINARY)
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull, "r")
    return os.fdopen(in_fd, "rb"), os.fdopen(out_fd, "wb")


class _Channel:
    def __init__(self, stream):
        import threading

        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message):
        import json

        data = json.dumps(message, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
        with self._lock:
            self._stream.write(data)
            self._stream.flush()


def _start_reader(stdin, requests):
    """A thread that puts every request line of ``stdin`` into ``requests``
    and ends the whole process at once when it closes (the host is gone)."""
    import threading

    def run():
        while True:
            try:
                line = stdin.readline(MAX_LINE_BYTES + 1)
            except (OSError, ValueError):
                line = b""
            if not line:
                os._exit(0)
            requests.put(line)

    thread = threading.Thread(target=run, name="stdin", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- models

class Models:
    def __init__(self, molscribe, rxn, ocr, peek=True):
        self.molscribe = molscribe
        self.rxn = rxn
        self.ocr = ocr
        #: False: RxnScribe is not the commit peek was written for.
        self.peek = peek


#: The RxnScribe commit the peek decoding re-implements (the archive pinned
#: in recognition_requirements.txt; a test checks they are the same). When
#: the pin changes, read RxnScribe's inference loop again, change this, and
#: run tests/test_recognition_live.py.
RXNSCRIBE_COMMIT = "ad6b1c75d40e563e68deca0491918885948d69c7"


class RxnScribeNotPinned(Exception):
    """The installed RxnScribe is not :data:`RXNSCRIBE_COMMIT`."""


def rxnscribe_is_pinned(direct_url=None):
    """True when the installed RxnScribe came from the archive of
    :data:`RXNSCRIBE_COMMIT` (its ``direct_url.json``, which pip and uv
    write; ``direct_url`` is that file's text, for tests)."""
    import json

    try:
        if direct_url is None:
            from importlib import metadata

            direct_url = metadata.distribution("rxnscribe").read_text("direct_url.json")
        url = json.loads(direct_url).get("url")
        return isinstance(url, str) and url.endswith("/" + RXNSCRIBE_COMMIT + ".zip")
    except Exception:
        return False


def load_models(models_dir):
    """MolScribe, RxnScribe and EasyOCR from local files, on the CPU."""
    import warnings

    warnings.filterwarnings("ignore")
    import torch

    # torch >= 2.6 loads with weights_only=True by default; both checkpoints
    # hold other objects too (an argparse.Namespace, lightning state). The
    # files are checked against their SHA-256 when they are installed.
    original_load = torch.load

    def load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return original_load(*args, **kwargs)

    torch.load = load
    device = torch.device("cpu")

    from molscribe import MolScribe

    molscribe = MolScribe(os.path.join(models_dir, MOL_CHECKPOINT), device=device)

    import rxnscribe.pix2seq.backbone as backbone

    # RxnScribe builds its ResNet-50 backbone with pretrained=True (a download
    # from download.pytorch.org) although the checkpoint overwrites those
    # weights. Switched off: nothing is downloaded.
    backbone.is_main_process = lambda: False
    from rxnscribe import RxnScribe
    import easyocr

    ocr = easyocr.Reader(
        ["en"], gpu=False, download_enabled=False, verbose=False,
        model_storage_directory=os.path.join(models_dir, "easyocr"),
        user_network_directory=os.path.join(models_dir, "easyocr", "user_network"))

    # RxnScribe.__init__ would fetch MolScribe from the Hugging Face hub and
    # build EasyOCR with downloads allowed; it gets the local objects instead.
    class LocalRxnScribe(RxnScribe):
        def get_molscribe(self):
            return molscribe

        def get_ocr_model(self):
            return ocr

    rxn = LocalRxnScribe(os.path.join(models_dir, RXN_CHECKPOINT), device=device)
    peek = rxnscribe_is_pinned()
    if not peek:
        _note("peek decoding", RxnScribeNotPinned())
    return Models(molscribe, rxn, ocr, peek=peek)


# --------------------------------------------------------------------------- recognition

class StageError(Exception):
    def __init__(self, stage, cause):
        super().__init__(stage)
        self.stage = stage
        self.cause = cause


def _open_image(image_base64):
    import base64
    import io

    from PIL import Image

    data = base64.b64decode(image_base64, validate=True)
    picture = Image.open(io.BytesIO(data))
    picture.load()
    if picture.mode in ("RGBA", "LA") or "transparency" in picture.info:
        # A transparent background becomes white, not black.
        rgba = picture.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        picture = Image.alpha_composite(white, rgba)
    return picture.convert("RGB")


def _prediction(pred):
    confidence = pred.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    smiles = pred.get("smiles")
    molfile = pred.get("molfile")
    return {"smiles": smiles if isinstance(smiles, str) else None,
            "molfile": molfile if isinstance(molfile, str) else None,
            "confidence": confidence}


#: MolScribe's ``CropWhite(pad=5)``: the white border it puts back.
SEEN_PAD = 5


def seen_box(image):
    """The rectangle of ``image`` (height x width x 3) that MolScribe's first
    step, ``CropWhite``, keeps: the rows and columns that are not pure white,
    with :data:`SEEN_PAD` pixels of border. ``[x0, y0, x1, y1]`` or None."""
    try:
        import numpy as np

        height, width = image.shape[:2]
        ink = (image != 255).any(axis=2)
        left, top, right, bottom = 0, 0, width, height
        if ink.any():
            rows = np.flatnonzero(ink.any(axis=1))
            cols = np.flatnonzero(ink.any(axis=0))
            top, bottom = int(rows[0]), int(rows[-1]) + 1
            left, right = int(cols[0]), int(cols[-1]) + 1
        return [left - SEEN_PAD, top - SEEN_PAD, right + SEEN_PAD, bottom + SEEN_PAD]
    except Exception:
        return None


def recognize_molecule(models, picture):
    import numpy as np

    image = np.asarray(picture)
    height, width = image.shape[:2]
    started = time.perf_counter()
    try:
        pred = models.molscribe.predict_images([image], return_confidence=True)[0]
    except Exception as exc:
        raise StageError("molecule", exc)
    molecule = _prediction(pred)
    molecule["box"] = [0, 0, int(width), int(height)]
    molecule["seen"] = seen_box(image)
    return {"width": int(width), "height": int(height), "molecules": [molecule], "reactions": [],
            "timing": {"molscribe_s": round(time.perf_counter() - started, 2), "molecules": 1}}


#: A text box EasyOCR reads nothing in is read again with a white border of
#: half its height (at least this many pixels) ...
TEXT_PAD = 4
#: ... and this many times larger. EasyOCR drops text smaller than 10 pixels:
#: the condition text of a scheme captured at 0.6 times the size of the
#: verification crops (an 11-pixel-high box) came back empty, so the page's
#: Conditions box stayed empty; enlarged it is read (2026-11-26).
TEXT_ENLARGE = 3


def _bare_number(line):
    """A compound number such as "1", "12" or "2a" (and nothing else)."""
    import re

    return re.fullmatch(r"\d{1,2}[a-z]?", line.strip()) is not None


def read_text(ocr, crop):
    """The lines EasyOCR reads in a text box. When no line has a visible
    character (the host drops such lines, so the text would be lost), once
    more with a white border and enlarged; of that second read only the
    lines with a visible character are kept, and none when they are all bare
    compound numbers (a "1" of the scheme is no condition). Unreadable text
    never fails the picture: ``[]``."""
    try:
        lines = [str(item[1]) for item in ocr.readtext(crop, detail=1)]
        if any(line.strip() for line in lines):
            return lines
        import numpy as np
        from PIL import Image

        pad = max(TEXT_PAD, crop.shape[0] // 2)
        framed = Image.fromarray(np.pad(crop, ((pad, pad), (pad, pad), (0, 0)), constant_values=255))
        bigger = framed.resize((framed.width * TEXT_ENLARGE, framed.height * TEXT_ENLARGE), Image.BICUBIC)
        again = [str(item[1]) for item in ocr.readtext(np.asarray(bigger), detail=1)]
        again = [line for line in again if line.strip()]
        if all(_bare_number(line) for line in again):
            return []
        return again
    except Exception:
        return []


def _whole_picture(models, picture, timing):
    """The whole picture read as one molecule (a selection around one
    structure, with no arrow), instead of answering "nothing found". A blank
    or unreadable picture stays empty."""
    width, height = picture.size
    single = recognize_molecule(models, picture)
    found = single["molecules"][0]
    # An empty picture comes back as a nonsense structure with a score of 0.
    if found.get("smiles") and (found.get("confidence") or 0.0) >= 0.2:
        found["source"] = "picture"
        single["timing"] = dict(timing, **single["timing"])
        return single
    return {"width": int(width), "height": int(height), "molecules": [],
            "reactions": [], "timing": timing}


def recognize_reaction(models, picture):
    import numpy as np

    image = np.asarray(picture)
    height, width = image.shape[:2]
    timing = {}

    started = time.perf_counter()
    try:
        raw, peek_boxes, info = layout(models, picture)
    except Exception as exc:
        raise StageError("layout", exc)
    timing["layout_s"] = round(time.perf_counter() - started, 2)
    timing.update(info)

    # Boxes come as fractions of the picture; cut out exactly as RxnScribe does.
    molecules = []          # raw entries, in order of first appearance
    position = {}           # pixel box -> index in molecules
    crops = []
    text_boxes = {}         # pixel box -> crop
    reactions = []
    every_box = []          # every box RxnScribe returned, of every kind (pixels)
    context = []            # per reaction: its pixel boxes by role (for leftover ink)
    for reaction in raw:
        entry = {"reactants": [], "agents": [], "products": [], "texts": []}
        where = {"reactants": [], "conditions": [], "products": []}
        for role in ("reactants", "conditions", "products"):
            for box in reaction.get(role) or []:
                x1, y1, x2, y2 = (float(v) for v in box["bbox"])
                px = (max(int(x1 * width), 0), max(int(y1 * height), 0),
                      min(int(x2 * width), width), min(int(y2 * height), height))
                if px[2] <= px[0] or px[3] <= px[1]:
                    continue
                every_box.append(list(px))
                where[role].append(list(px))
                if box.get("category_id") == 1:
                    if px not in position:
                        position[px] = len(molecules)
                        peek = tuple(float(v) for v in box["bbox"]) in peek_boxes
                        molecules.append({"box": list(px), "source": "peek" if peek else "layout"})
                        crops.append(image[px[1]:px[3], px[0]:px[2]])
                    target = entry["agents" if role == "conditions" else role]
                    if position[px] not in target:
                        target.append(position[px])
                elif role == "conditions" or (role == "reactants" and box.get("category_id") == 2):
                    # Text at the arrow, and a reactant given as text. Labels
                    # ("1a") next to reactants and products are left out.
                    text_boxes.setdefault(px, image[px[1]:px[3], px[0]:px[2]])
                    entry["texts"].append({"box": list(px), "lines": [], "role": role})
        reactions.append(entry)
        context.append(where)

    if not every_box:
        # RxnScribe saw nothing at all. Leftover ink would find the same
        # drawing again, so it does not run here.
        return _whole_picture(models, picture, timing)

    started = time.perf_counter()
    if crops:
        try:
            preds = models.molscribe.predict_images(crops, return_confidence=True, batch_size=16)
        except Exception as exc:
            raise StageError("molecule", exc)
        for molecule, pred, crop in zip(molecules, preds, crops):
            molecule.update(_prediction(pred))
            molecule["seen"] = seen_box(crop)
    timing["molscribe_s"] = round(time.perf_counter() - started, 2)
    timing["molecules"] = len(crops)

    started = time.perf_counter()
    try:
        found = leftover_ink(models, image, every_box, context)
    except Exception as exc:   # never fails the picture
        _note("leftover ink", exc)
        found = []
    slots = {"reactant": "reactants", "product": "products", "agent": "agents"}
    for molecule, guess in found:
        index = len(molecules)
        molecules.append(molecule)
        slot = slots.get(guess.get("role"))
        reaction = guess.get("reaction")
        if slot and isinstance(reaction, int) and 0 <= reaction < len(reactions):
            reactions[reaction][slot].append(index)
    timing["leftover_s"] = round(time.perf_counter() - started, 2)
    timing["leftover"] = len(found)

    if not molecules:
        # Labels or text only, and no leftover structure either.
        return _whole_picture(models, picture, timing)

    started = time.perf_counter()
    read = {}
    for px, crop in text_boxes.items():
        read[px] = read_text(models.ocr, crop)
    for entry in reactions:
        for text in entry["texts"]:
            text["lines"] = read.get(tuple(text["box"]), [])
    timing["ocr_s"] = round(time.perf_counter() - started, 2)
    timing["texts"] = len(text_boxes)
    return {"width": int(width), "height": int(height), "molecules": molecules,
            "reactions": reactions, "timing": timing}


def _note(what, exc):
    """One line on stderr when a remedy was skipped: what, and the NAME of the
    exception (never its message, never anything of the picture)."""
    try:
        sys.stderr.write(f"recognition worker: {what} skipped ({type(exc).__name__})\n")
        sys.stderr.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------- layout: RxnScribe with "peek"

#: RxnScribe's tokens (rxnscribe/tokenizer.py).
RCT, CND, PRD, MOL = "[Rct]", "[Cnd]", "[Prd]", "[Mol]"
#: The side branch names at most this many boxes (5 tokens each).
PEEK_BOXES = 2
#: A side-branch box is refused when, with a molecule box already found, the
#: IoU or the share of either box that the other covers reaches this.
PEEK_MAX_OVERLAP = 0.5
#: The value RxnScribe gives a token its grammar forbids.
_FORBIDDEN = -10000.0


class PeekMismatch(Exception):
    """The re-implemented decoding is not the one of ``predict_images``."""


def decode_peek(advance, tok, max_len=256, peek=PEEK_BOXES):
    """RxnScribe's greedy decoding (``rxnscribe/pix2seq/transformer.py``, the
    inference branch, batch of one) step by step, plus the side branch.

    ``advance(state, token)`` -> ``(log_probs, state)`` is one decoder step:
    ``token`` None for the first one, ``log_probs`` a 1-D numpy array over the
    vocabulary before the grammar mask, ``state`` whatever the decoder keeps
    (never changed in place, so the side branch can start from it).
    ``tok`` is RxnScribe's reaction tokenizer.

    The side branch runs where the main decoding is inside a product part
    that holds at least one box but no molecule box, and is about to close it.
    From that state, with the closing token forbidden, it decodes greedily
    until it has named a molecule box or :data:`PEEK_BOXES` boxes, or the
    model wants to close a part or end.

    Returns ``{"seq", "scores", "eos", "peeks": [{"at", "boxes": [{"tokens",
    "min_score"}]}], "side_steps"}``; ``seq`` is the main sequence without the
    end token, ``eos`` whether the model ended it."""
    import numpy as np

    close = tok.stoi[PRD]
    eos_id = tok.EOS_ID
    stops = {close, eos_id, tok.stoi[RCT], tok.stoi[CND]}

    def masked(log_probs, mask):
        out = np.array(log_probs, dtype=np.float32, copy=True)
        out[np.asarray(mask, dtype=bool)] = _FORBIDDEN
        return out

    def side_branch(log_probs, state, states):
        boxes, current, current_scores, steps = [], [], [], 0
        log_probs = log_probs.copy()
        log_probs[close] = _FORBIDDEN
        while True:
            token = int(np.argmax(log_probs))
            if token in stops:
                break
            current.append(token)
            current_scores.append(float(log_probs[token]))
            steps += 1
            if len(current) == 5:
                boxes.append({"tokens": current, "min_score": round(min(current_scores), 3)})
                is_mol = tok.itos.get(current[4]) == MOL
                current, current_scores = [], []
                if is_mol or len(boxes) >= peek:
                    break
            raw, state = advance(state, int(np.argmax(log_probs[:len(log_probs) - 2])))
            states, masks = tok.update_states_and_masks(states, [token])
            log_probs = masked(raw, masks[0])
            if states[0][1] == "c" and len(boxes) < peek:
                log_probs[close] = _FORBIDDEN
        return boxes, steps

    state, token, states, previous = None, None, [None], [None]
    seq, scores, peeks = [], [], []
    side_steps, eos = 0, False
    section_has, section_boxes = set(), 0
    for _ in range(max_len):
        raw, state = advance(state, token)
        states, masks = tok.update_states_and_masks(states, previous)
        log_probs = masked(raw, masks[0])
        where = states[0]
        best = int(np.argmax(log_probs))
        if peek and where[0] == PRD and where[1] == "c" and section_boxes > 0 \
                and MOL not in section_has and best == close:
            boxes, steps = side_branch(log_probs, state, states)
            side_steps += steps
            peeks.append({"at": len(seq), "boxes": boxes})
        if best == eos_id:
            eos = True
            break
        seq.append(best)
        scores.append(float(log_probs[best]))
        name = tok.itos.get(best)
        if name in (RCT, CND, PRD) or (name is not None and where[1] == "e"):
            section_has, section_boxes = set(), 0      # a part was closed / a reaction starts
        elif where[1] == "y2":
            section_has.add(name)
            section_boxes += 1
        token = int(np.argmax(log_probs[:len(log_probs) - 2]))
        previous = [best]
    return {"seq": seq, "scores": scores, "eos": eos, "peeks": peeks, "side_steps": side_steps}


def main_sequence_holds(rows, seq, tok, eos=True):
    """True when ``seq`` is what greedy decoding gives: ``rows[i]`` are the
    model's log-probabilities after ``seq[:i]`` (one decoder pass over the
    whole sequence), and with RxnScribe's grammar mask the best token of each
    row is the next one of ``seq`` (and the end token after the last, when
    the decoding ``eos``).

    This guards the step-by-step arithmetic (incremental against one full
    pass) with the worker's own replay of the grammar. It cannot notice a
    change in RxnScribe's inference loop; :data:`RXNSCRIBE_COMMIT` and the
    live test do."""
    import numpy as np

    states, previous = [None], [None]
    wanted = list(seq) + ([tok.EOS_ID] if eos else [])
    if len(rows) < len(wanted):
        return False
    for i, token in enumerate(wanted):
        states, masks = tok.update_states_and_masks(states, previous)
        row = np.array(rows[i], dtype=np.float32, copy=True)
        row[np.asarray(masks[0], dtype=bool)] = _FORBIDDEN
        if int(np.argmax(row)) != token:
            return False
        previous = [token]
    return True


def _overlap(a, b):
    """(IoU, share of ``a`` covered, share of ``b`` covered)."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0, 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a, area_b = (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter), inter / area_a, inter / area_b


def splice(decoded, tok, scale, max_overlap=PEEK_MAX_OVERLAP):
    """The main sequence with the acceptable side-branch boxes inserted where
    their side branch started. Accepted: a molecule box that overlaps no
    molecule box already named (IoU below ``max_overlap``, and neither box
    covered for ``max_overlap`` or more by the other).
    Returns ``(seq, scores, accepted)``, ``accepted`` the boxes of
    ``tok.sequence_to_bbox``."""
    seq, scores = decoded["seq"], decoded["scores"]
    have = []
    i = 0
    while i < len(seq):
        if seq[i] not in tok.itos and i + 5 <= len(seq):
            box = tok.sequence_to_bbox(seq[i:i + 5], scale)
            if box is not None:
                if box["category"] == MOL:
                    have.append(box["bbox"])
                i += 5
                continue
        i += 1
    out_seq, out_scores, accepted, last = [], [], [], 0
    for peek in decoded["peeks"]:
        out_seq += seq[last:peek["at"]]
        out_scores += scores[last:peek["at"]]
        last = peek["at"]
        for found in peek["boxes"]:
            box = tok.sequence_to_bbox(found["tokens"], scale)
            if box is None or box["category"] != MOL:
                continue
            if any(max(_overlap(box["bbox"], other)) >= max_overlap for other in have):
                continue
            have.append(box["bbox"])
            accepted.append(box)
            out_seq += found["tokens"]
            out_scores += [found["min_score"]] * 5
    out_seq += seq[last:]
    out_scores += scores[last:]
    return out_seq, out_scores, accepted


def peek_layout(tok, advance, check, scale, parse):
    """Decoding with peek, the check of the main sequence against one full
    decoder pass, the insertion, the parse. ``check(seq)`` -> the
    log-probability rows of one decoder pass over ``seq``;
    ``parse(seq, scores, scale)`` -> the reactions (as ``predict_images``
    returns them). Raises :class:`PeekMismatch` when the step-by-step main
    sequence is not the greedy sequence of that pass.
    Returns ``(reactions, peek boxes {bbox tuple}, info)``."""
    decoded = decode_peek(advance, tok, max_len=tok.max_len)
    if not main_sequence_holds(check(decoded["seq"]), decoded["seq"], tok, eos=decoded["eos"]):
        raise PeekMismatch("main sequence")
    if not decoded["eos"]:
        # predict_images keeps nothing of a sequence that never ends.
        return parse([], [], scale), set(), {"peek_triggers": 0, "peek_added": 0}
    seq, scores, accepted = splice(decoded, tok, scale)
    info = {"peek_triggers": len(decoded["peeks"]), "peek_added": len(accepted)}
    return parse(seq, scores, scale), {tuple(float(v) for v in b["bbox"]) for b in accepted}, info


def _decoder(models, picture):
    """RxnScribe's model on ``picture``, ready to decode: ``(advance, check,
    scale, parse)`` for :func:`peek_layout`. The same steps as
    ``RxnScribe.predict_images`` and ``Pix2Seq.forward`` (CPU, batch of one)."""
    import torch
    import torch.nn.functional as F
    from rxnscribe.data import postprocess_reactions
    from rxnscribe.pix2seq.misc import nested_tensor_from_tensor_list

    rxn = models.rxn
    tok = rxn.tokenizer["reaction"]
    model = rxn.model
    tr = model.transformer
    image, ref = rxn.transform(picture)
    with torch.no_grad():
        features, pos = model.backbone(nested_tensor_from_tensor_list(torch.stack([image], dim=0)))
        src, mask = features[-1].decompose()
        mask = torch.zeros_like(mask).bool()
        src = model.input_proj(src)
        src = src.flatten(2).permute(2, 0, 1)
        mask = mask.flatten(1)
        pos_embed = pos[-1].flatten(2).permute(2, 0, 1)
        memory = tr.encoder(src, src_key_padding_mask=mask, pos=pos_embed)
        start = tr.det_embed.weight.unsqueeze(0).repeat(1, 1, 1).transpose(0, 1)

    def empty():
        return [torch.as_tensor([[], []], device=memory.device) for _ in range(tr.num_decoder_layers)]

    def advance(state, token):
        with torch.no_grad():
            if token is None:
                embed, state = start, empty()
            else:
                embed = tr.vocal_embed(torch.tensor([[token]], device=memory.device))
            hs, state = tr.decoder(embed, memory, memory_key_padding_mask=mask, pos=pos_embed,
                                   pre_kv_list=state)
            log_probs = F.log_softmax(tr.vocal_classifier(hs.transpose(0, 1)), dim=-1)
            return log_probs[0, -1].numpy(), state

    def check(seq):
        with torch.no_grad():
            tokens = torch.tensor([list(seq)], dtype=torch.long, device=memory.device)
            embed = torch.cat([start.transpose(0, 1), tr.vocal_embed(tokens)], dim=1).transpose(0, 1)
            size = embed.shape[0]
            causal = torch.triu(torch.ones((size, size), device=memory.device), diagonal=1).bool()
            hs, _ = tr.decoder(embed, memory, memory_key_padding_mask=mask, pos=pos_embed,
                               pre_kv_list=empty(), self_attn_mask=causal)
            return F.log_softmax(tr.vocal_classifier(hs.transpose(0, 1)), dim=-1)[0].numpy()

    def parse(seq, scores, scale):
        reactions = tok.sequence_to_data(list(seq), list(scores), scale=scale)
        return postprocess_reactions(reactions, image=picture, molscribe=None, ocr=None)

    return advance, check, ref["scale"], parse


def layout(models, picture):
    """RxnScribe's reactions of ``picture`` exactly as ``predict_images``
    gives them, plus the molecule boxes the peek side branch recovered.
    Returns ``(reactions, peek boxes {bbox tuple}, info)``. On any trouble
    with the peek decoding, and when the installed RxnScribe is not the one
    it was written for (``models.peek`` False, noted once at load), the
    picture is read with ``predict_images``."""
    if getattr(models, "peek", True):
        try:
            tok = models.rxn.tokenizer["reaction"]
            advance, check, scale, parse = _decoder(models, picture)
            return peek_layout(tok, advance, check, scale, parse)
        except Exception as exc:
            _note("peek decoding", exc)
    raw = models.rxn.predict_images([picture], molscribe=False, ocr=False)[0]
    return raw, set(), {"peek": "off"}


# --------------------------------------------------------------------------- leftover ink

#: The operating point of leftover ink (tools/recognition-e2e/missed/DECISION.md,
#: section 2). Lengths are in units of L, the bond length of the picture.
LEFTOVER = {
    "ink_diff": 50,         # a pixel is ink when a channel differs this much from the median colour
    "a": 0.2,               # strokes closer than 2 * a * L form one group
    "min_size": 0.8,        # groups whose longer side is shorter are dropped
    "min_short": 1.2,       # shape filter: shorter side ...
    "min_long": 1.8,        # ... longer side ...
    "max_aspect": 6.0,      # ... aspect ratio ...
    "min_bonds": 3,         # ... straight strokes of at least 0.6 L ...
    "min_angles": 2,        # ... in at least this many 30-degree directions
    "max_small_ink": 1.0,   # share of the ink in letter-sized pieces (1.0: filter off)
    "max_groups": 16,       # groups read by MolScribe, largest ink first
    "min_conf": 0.70,       # MolScribe confidence
    "min_heavy": 3,         # heavy atoms of the reading
    "max_new": 8,           # molecules added to one picture
    "max_gap": 8.0,         # an arrow farther than this gives no role
}
#: Leftover ink works on full-resolution arrays (several copies, a distance
#: transform, labels). Above this many pixels it works on a copy reduced to
#: this size and scales its boxes back. Every measured picture had at most
#: 2.2 million pixels (2820 x 772), so none of them is reduced; at the
#: protocol's 8000 x 8000 the full-size geometry took about 16 s and 2 GB.
LEFTOVER_MAX_PIXELS = 4_000_000
#: A reading with any other element is dropped (a misread usually shows as one).
LEFTOVER_ELEMENTS = frozenset({"C", "N", "O", "S", "P", "F", "Cl", "Br", "I", "B", "Si", "H", "Na",
                               "K", "Li", "Mg", "Cs", "Se", "Sn", "Zn", "Cu", "Pd", "Al"})


def ink_mask(image):
    """uint8 0/1. Background = the median colour; coloured atom labels count as ink."""
    import numpy as np

    background = np.median(image.reshape(-1, image.shape[2]), axis=0)
    diff = np.abs(image.astype(np.int16) - background.astype(np.int16)).max(axis=2)
    return (diff > LEFTOVER["ink_diff"]).astype(np.uint8)


def _stroke_width(ink):
    import cv2
    import numpy as np

    if not ink.any():
        return 1.0
    dist = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    return float(max(1.0, 2.0 * np.percentile(dist[ink > 0], 75)))


def _line_segments(image, stroke):
    """Straight strokes as rows ``[x1, y1, x2, y2, length]`` (OpenCV's line
    segment detector on the grey picture; a drawn line gives its two edges),
    longer than 4 stroke widths."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    found = cv2.createLineSegmentDetector(0).detect(gray)[0]
    if found is None:
        return np.zeros((0, 5), dtype=np.float32)
    seg = found.reshape(-1, 4).astype(np.float32)
    length = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    return np.column_stack([seg, length])[length > 4 * stroke]


def _components(mask):
    import cv2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return n, labels, stats


def find_arrows(labels, stats, L, stroke):
    """Long thin connected components with a head at ONE end:
    ``[{"tail", "head", "length", "box", "cc"}]``."""
    import numpy as np

    arrows = []
    for i in range(1, len(stats)):
        x, y, w, h, _area = (int(v) for v in stats[i])
        if max(w, h) < 1.0 * L:
            continue
        ys, xs = np.nonzero(labels[y:y + h, x:x + w] == i)
        pts = np.column_stack([xs + x, ys + y]).astype(np.float32)
        mean = pts.mean(axis=0)
        _values, vectors = np.linalg.eigh(np.cov((pts - mean).T))
        axis = vectors[:, 1]
        along = (pts - mean) @ axis
        across = (pts - mean) @ vectors[:, 0]
        major = float(along.max() - along.min())
        minor = float(across.max() - across.min())
        if major < 1.0 * L or minor > max(0.5 * L, 0.14 * major) or minor > 0.4 * major:
            continue
        if float((np.abs(across) <= max(1.5 * stroke, 2.0)).mean()) < 0.55:
            continue
        lo, hi = along.min(), along.max()
        end = max(0.18 * major, 3 * stroke)
        spread_lo = float(np.ptp(across[along <= lo + end])) if (along <= lo + end).any() else 0.0
        spread_hi = float(np.ptp(across[along >= hi - end])) if (along >= hi - end).any() else 0.0
        middle = (along > lo + end) & (along < hi - end)
        spread_mid = float(np.percentile(np.abs(across[middle]), 90) * 2) if middle.any() else stroke
        big, small = max(spread_lo, spread_hi), min(spread_lo, spread_hi)
        if big < max(2.0 * spread_mid, 2.0 * stroke) or small > 0.7 * big:
            continue        # no head, or a head at both ends
        p_lo, p_hi = mean + axis * lo, mean + axis * hi
        tail, head = (p_lo, p_hi) if spread_hi > spread_lo else (p_hi, p_lo)
        arrows.append({"tail": [float(tail[0]), float(tail[1])], "head": [float(head[0]), float(head[1])],
                       "length": major, "box": [x, y, x + w, y + h], "cc": i})
    return arrows


def prepare(image):
    """``image``: RGB uint8 array -> ink, stroke width, straight strokes, the
    bond length L (90th percentile of the stroke lengths; 3 % of the longer
    side when there are fewer than 6 strokes), components and arrows."""
    ink = ink_mask(image)
    stroke = _stroke_width(ink)
    segments = _line_segments(image, stroke)
    if len(segments) < 6:
        L = 0.03 * max(ink.shape[:2])
    else:
        import numpy as np

        L = float(np.percentile(segments[:, 4], 90))
    _n, labels, stats = _components(ink)
    arrows = find_arrows(labels, stats, L, stroke)
    return {"image": image, "ink": ink, "stroke": stroke, "segments": segments, "L": L,
            "labels": labels, "stats": stats, "arrows": arrows}


def candidates(prep, boxes):
    """Groups of the ink that no box covers (every box grown by
    max(2, 0.08 L) pixels; the arrows taken out), with the features of the
    shape filter."""
    import math

    import cv2
    import numpy as np

    L = prep["L"]
    left = prep["ink"].copy()
    height, width = left.shape
    margin = int(round(max(2, 0.08 * L)))
    for x0, y0, x1, y1 in boxes:
        left[max(int(y0) - margin, 0):min(int(y1) + margin, height),
             max(int(x0) - margin, 0):min(int(x1) + margin, width)] = 0
    for arrow in prep["arrows"]:
        left[prep["labels"] == arrow["cc"]] = 0
    radius = max(1, int(round(LEFTOVER["a"] * L)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    n, groups, gstats = _components(cv2.dilate(left, kernel))
    _rn, raw, rstats = _components(left)
    segments = prep["segments"]
    seg_group = np.zeros(len(segments), dtype=np.int32)
    if len(segments):
        mx = np.clip(((segments[:, 0] + segments[:, 2]) / 2).astype(int), 0, width - 1)
        my = np.clip(((segments[:, 1] + segments[:, 3]) / 2).astype(int), 0, height - 1)
        seg_group = np.where(left[my, mx] > 0, groups[my, mx], 0)
    out = []
    for g in range(1, n):
        gx, gy, gw, gh, _ = (int(v) for v in gstats[g])
        window = (groups[gy:gy + gh, gx:gx + gw] == g) & (left[gy:gy + gh, gx:gx + gw] > 0)
        ys, xs = np.nonzero(window)
        if len(xs) == 0:
            continue
        x0, x1, y0, y1 = gx + xs.min(), gx + xs.max() + 1, gy + ys.min(), gy + ys.max() + 1
        w, h = x1 - x0, y1 - y0
        if max(w, h) < LEFTOVER["min_size"] * L:
            continue
        ids = np.unique(raw[gy:gy + gh, gx:gx + gw][window])
        dims = np.maximum(rstats[ids, 2], rstats[ids, 3]).astype(float)
        areas = rstats[ids, 4].astype(float)
        mine = segments[seg_group == g]
        bonds = mine[mine[:, 4] >= 0.6 * L]
        angles = set()
        for sx0, sy0, sx1, sy1, _ in bonds:
            angles.add(int(((math.degrees(math.atan2(sy1 - sy0, sx1 - sx0)) + 180.0 + 15.0) % 180.0) // 30))
        ink_px = int(len(xs))
        # Rounded as in the measurement, so that the filter gives the same answers.
        out.append({"box": [int(x0), int(y0), int(x1), int(y1)], "group": g, "_groups": groups, "_left": left,
                    "w_L": round(w / L, 3), "h_L": round(h / L, 3), "ink": ink_px,
                    "n_bonds": int(len(bonds)), "n_angles": len(angles),
                    "small_ink_frac": round(float(areas[dims < 0.6 * L].sum()) / max(ink_px, 1), 3)})
    out.sort(key=lambda c: (c["box"][0], c["box"][1]))
    return out


def shape_ok(cand, params=LEFTOVER):
    """Big enough, not a line of text, has bonds in two directions."""
    short, long_ = sorted((cand["w_L"], cand["h_L"]))
    return (short >= params["min_short"] and long_ >= params["min_long"]
            and long_ / max(short, 1e-6) <= params["max_aspect"]
            and cand["n_bonds"] >= params["min_bonds"] and cand["n_angles"] >= params["min_angles"]
            and cand["small_ink_frac"] <= params["max_small_ink"])


def crop_of(prep, cand):
    """A white picture holding only this group's ink (dilated 5 x 5), padded
    by max(4, 0.3 L) pixels. Returns ``(crop, [x0, y0, x1, y1])``, the
    rectangle of the picture the crop covers."""
    import cv2
    import numpy as np

    image = prep["image"]
    height, width = image.shape[:2]
    pad = int(round(max(4, 0.3 * prep["L"])))
    x0, y0, x1, y1 = cand["box"]
    X0, Y0, X1, Y1 = max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, width), min(y1 + pad, height)
    own = ((cand["_groups"][Y0:Y1, X0:X1] == cand["group"]) & (cand["_left"][Y0:Y1, X0:X1] > 0)).astype(np.uint8)
    own = cv2.dilate(own, np.ones((5, 5), np.uint8)) > 0
    crop = np.full((Y1 - Y0, X1 - X0, 3), 255, dtype=np.uint8)
    crop[own] = image[Y0:Y1, X0:X1][own]
    return crop, [int(X0), int(Y0), int(X1), int(Y1)]


def chem_ok(smiles, params=LEFTOVER):
    """RDKit reads it, it has enough heavy atoms and only plain elements."""
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")
    if not smiles:
        return False
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return False
    if sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1) < params["min_heavy"]:
        return False
    return not any(atom.GetAtomicNum() > 0 and atom.GetSymbol() not in LEFTOVER_ELEMENTS
                   for atom in mol.GetAtoms())


def _point_box_distance(point, box):
    import math

    dx = max(box[0] - point[0], 0.0, point[0] - box[2])
    dy = max(box[1] - point[1], 0.0, point[1] - box[3])
    return math.hypot(dx, dy)


def _relation(arrow, box, L):
    """(role, distance) of a box relative to one arrow: "agent" when at least
    half of the box lies along the arrow and it is at most half its size plus
    2.5 L off it; else "product" past the middle of the arrow, "reactant"
    before it."""
    import numpy as np

    tail, head = np.array(arrow["tail"], dtype=float), np.array(arrow["head"], dtype=float)
    length = max(float(np.linalg.norm(head - tail)), 1e-6)
    u = (head - tail) / length
    corners = np.array([[box[0], box[1]], [box[2], box[1]], [box[0], box[3]], [box[2], box[3]]], dtype=float)
    t = ((corners - tail) @ u) / length
    t0, t1 = float(t.min()), float(t.max())
    centre = np.array([(box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0])
    off = abs(float((centre - tail) @ np.array([-u[1], u[0]])))
    half = (abs(u[1]) * (box[2] - box[0]) + abs(u[0]) * (box[3] - box[1])) / 2.0
    inside = max(0.0, min(t1, 1.0) - max(t0, 0.0)) / max(t1 - t0, 1e-6)
    if inside >= 0.5 and off <= half + 2.5 * L:
        return "agent", max(off - half, 0.0)
    d_head = _point_box_distance(arrow["head"], box)
    d_tail = _point_box_distance(arrow["tail"], box)
    if float((centre - tail) @ u) / length >= 0.5:
        return "product", d_head + 0.5 * max(off - half, 0.0)
    return "reactant", d_tail + 0.5 * max(off - half, 0.0)


def arrow_reaction(L, arrow, context):
    """The reaction (index in ``context``) whose boxes sit on the right sides
    of this arrow (reactants at the tail, products at the head, condition
    boxes along it), or None."""
    best, best_score = None, 0.0
    for index, reaction in enumerate(context):
        agree = total = 0
        distance = []
        for role, key in (("reactant", "reactants"), ("agent", "conditions"), ("product", "products")):
            for box in reaction.get(key, []):
                got, d = _relation(arrow, box, L)
                total += 1
                agree += got == role
                distance.append(d)
        if not total:
            continue
        score = agree / total - 0.02 * (min(distance) / L)
        if agree and score > best_score:
            best, best_score = index, score
    return best


def guess_role(L, arrows, box, context, max_gap=LEFTOVER["max_gap"]):
    """Role of a recovered box by where it lies relative to the nearest arrow
    within ``max_gap`` L: ``{"role": "reactant" | "agent" | "product" | None,
    "reaction": index in context | None}``."""
    best = None
    for index, arrow in enumerate(arrows):
        role, distance = _relation(arrow, box, L)
        if best is None or distance < best[2]:
            best = (index, role, distance)
    if best is None or best[2] > max_gap * L:
        return {"role": None, "reaction": None}
    return {"role": best[1], "reaction": arrow_reaction(L, arrows[best[0]], context)}


def _reduced(image, boxes, context, max_pixels):
    """``(image, boxes, context, fx, fy)``: unchanged with factors 1 when the
    picture has at most ``max_pixels`` pixels, else a copy reduced to about
    that many (OpenCV's area resampling) with the boxes in its pixels;
    ``fx``, ``fy`` turn its pixels back into the picture's."""
    height, width = image.shape[:2]
    if height * width <= max_pixels:
        return image, boxes, context, 1.0, 1.0
    import cv2

    shrink = (max_pixels / float(height * width)) ** 0.5
    small_w, small_h = max(1, int(width * shrink)), max(1, int(height * shrink))
    small = cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA)
    fx, fy = width / float(small_w), height / float(small_h)

    def scaled(box):
        return [box[0] / fx, box[1] / fy, box[2] / fx, box[3] / fy]

    boxes = [scaled(box) for box in boxes]
    context = [{key: [scaled(box) for box in value] for key, value in reaction.items()} for reaction in context]
    return small, boxes, context, fx, fy


def leftover_ink(models, image, boxes, context, params=LEFTOVER, max_pixels=LEFTOVER_MAX_PIXELS):
    """Molecules in the ink that no box of RxnScribe covers.

    ``boxes``: every pixel box RxnScribe returned (all kinds); ``context``:
    per reaction ``{"reactants", "conditions", "products"}`` pixel boxes.
    Returns ``[(molecule, {"role", "reaction"})]``; a molecule is like the
    worker's (``box`` = the rectangle MolScribe was given, ``seen`` inside
    it) with ``"source": "leftover"``. A picture above ``max_pixels`` is
    searched and read on a reduced copy (:func:`_reduced`); ``box`` and
    ``seen`` are then given back in the picture's pixels."""
    full_h, full_w = image.shape[:2]
    image, boxes, context, fx, fy = _reduced(image, boxes, context, max_pixels)
    prep = prepare(image)
    groups = [c for c in candidates(prep, boxes) if shape_ok(c, params)]
    groups.sort(key=lambda c: -c["ink"])
    groups = groups[:params["max_groups"]]
    if not groups:
        return []
    crops = [crop_of(prep, c) for c in groups]
    preds = models.molscribe.predict_images([crop for crop, _ in crops], return_confidence=True,
                                            batch_size=16)
    out = []
    for group, (crop, rect), pred in zip(groups, crops, preds):
        reading = _prediction(pred)
        if (reading["confidence"] or 0.0) < params["min_conf"] or not chem_ok(reading["smiles"], params):
            continue
        seen = seen_box(crop)
        if (fx, fy) != (1.0, 1.0):
            rect = [min(int(round(rect[0] * fx)), full_w), min(int(round(rect[1] * fy)), full_h),
                    min(int(round(rect[2] * fx)), full_w), min(int(round(rect[3] * fy)), full_h)]
            if seen is not None:
                seen = [int(round(seen[0] * fx)), int(round(seen[1] * fy)),
                        int(round(seen[2] * fx)), int(round(seen[3] * fy))]
        molecule = {"box": rect, "source": "leftover"}
        molecule.update(reading)
        molecule["seen"] = seen
        out.append((molecule, guess_role(prep["L"], prep["arrows"], group["box"], context)))
        if len(out) >= params["max_new"]:
            break
    return out


def handle(models, request):
    if request.get("op") != "recognize":
        raise StageError("request", ValueError("unknown op"))
    mode = request.get("mode")
    if mode not in ("reaction", "molecule"):
        raise StageError("request", ValueError("unknown mode"))
    try:
        picture = _open_image(request.get("image") or "")
    except Exception as exc:
        raise StageError("image", exc)
    if mode == "molecule":
        return recognize_molecule(models, picture)
    return recognize_reaction(models, picture)


# --------------------------------------------------------------------------- main

def main(argv=None):
    import json
    import queue

    argv = list(sys.argv[1:] if argv is None else argv)
    models_dir = _models_dir(argv)
    requests_in, lines_out = _protect_streams()
    channel = _Channel(lines_out)
    if models_dir is None:
        channel.send({"event": "failed", "stage": "arguments", "error": "NoModelsFolder"})
        return 2
    _set_environment(models_dir)
    requests = queue.Queue()
    _start_reader(requests_in, requests)
    channel.send({"event": "hello", "worker": WORKER_VERSION})

    try:
        models = load_models(models_dir)
    except BaseException as exc:  # noqa: BLE001 - reported to the host, then the worker ends
        import traceback

        traceback.print_exc()          # stderr: nothing of a picture exists yet
        channel.send({"event": "failed", "stage": "load", "error": type(exc).__name__})
        return 3
    channel.send({"event": "ready", "load_seconds": round(time.perf_counter() - _T_START, 2)})

    while True:
        line = requests.get()
        if len(line) > MAX_LINE_BYTES:
            channel.send({"id": None, "ok": False, "stage": "request", "error": "LineTooLong"})
            return 4       # the rest of that line is still in the pipe
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError:
            channel.send({"id": None, "ok": False, "stage": "request", "error": "BadJson"})
            continue
        if request.get("op") == "shutdown":
            return 0
        request_id = request.get("id")
        started = time.perf_counter()
        try:
            result = handle(models, request)
            result["timing"]["total_s"] = round(time.perf_counter() - started, 2)
            channel.send({"id": request_id, "ok": True, "result": result})
        except StageError as exc:
            channel.send({"id": request_id, "ok": False, "stage": exc.stage,
                          "error": type(exc.cause).__name__})
        except Exception as exc:  # noqa: BLE001
            channel.send({"id": request_id, "ok": False, "stage": "unknown",
                          "error": type(exc).__name__})


if __name__ == "__main__":
    code = main()
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code if isinstance(code, int) else 1)
