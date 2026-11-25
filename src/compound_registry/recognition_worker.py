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
    def __init__(self, molscribe, rxn, ocr):
        self.molscribe = molscribe
        self.rxn = rxn
        self.ocr = ocr


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
    return Models(molscribe, rxn, ocr)


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


def recognize_reaction(models, picture):
    import numpy as np

    image = np.asarray(picture)
    height, width = image.shape[:2]
    timing = {}

    started = time.perf_counter()
    try:
        raw = models.rxn.predict_images([picture], molscribe=False, ocr=False)[0]
    except Exception as exc:
        raise StageError("layout", exc)
    timing["layout_s"] = round(time.perf_counter() - started, 2)

    # Boxes come as fractions of the picture; cut out exactly as RxnScribe does.
    molecules = []          # raw entries, in order of first appearance
    position = {}           # pixel box -> index in molecules
    crops = []
    text_boxes = {}         # pixel box -> crop
    reactions = []
    for reaction in raw:
        entry = {"reactants": [], "agents": [], "products": [], "texts": []}
        for role in ("reactants", "conditions", "products"):
            for box in reaction.get(role) or []:
                x1, y1, x2, y2 = (float(v) for v in box["bbox"])
                px = (max(int(x1 * width), 0), max(int(y1 * height), 0),
                      min(int(x2 * width), width), min(int(y2 * height), height))
                if px[2] <= px[0] or px[3] <= px[1]:
                    continue
                if box.get("category_id") == 1:
                    if px not in position:
                        position[px] = len(molecules)
                        molecules.append({"box": list(px)})
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

    if not molecules:
        # No reaction in the picture (a selection around one structure, with no
        # arrow): read the whole picture as one molecule instead of answering
        # "nothing found". A blank or unreadable picture stays empty.
        single = recognize_molecule(models, picture)
        found = single["molecules"][0]
        # An empty picture comes back as a nonsense structure with a score of 0.
        if found.get("smiles") and (found.get("confidence") or 0.0) >= 0.2:
            single["timing"]["layout_s"] = timing["layout_s"]
            return single
        return {"width": int(width), "height": int(height), "molecules": [],
                "reactions": [], "timing": timing}

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
    read = {}
    for px, crop in text_boxes.items():
        try:
            found = models.ocr.readtext(crop, detail=1)
            read[px] = [str(item[1]) for item in found]
        except Exception:
            read[px] = []      # unreadable text never fails the picture
    for entry in reactions:
        for text in entry["texts"]:
            text["lines"] = read.get(tuple(text["box"]), [])
    timing["ocr_s"] = round(time.perf_counter() - started, 2)
    timing["texts"] = len(text_boxes)
    return {"width": int(width), "height": int(height), "molecules": molecules,
            "reactions": reactions, "timing": timing}


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
