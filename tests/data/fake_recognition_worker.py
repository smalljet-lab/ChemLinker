"""A stand-in for recognition_worker.py in the tests: speaks the same JSON
lines, needs no torch, and misbehaves on request.

    python fake_recognition_worker.py BEHAVIOUR [STATE_FOLDER]

BEHAVIOUR: ok | garbage | fail_load | die_loading | slow_load | never_ready |
crash | crash_once | hang | model_error | image_error | bad_shape.
STATE_FOLDER (optional): ``starts`` counts the starts there, and ``ended-<n>``
is written when start number n ends because its stdin closed.
"""
import json
import os
import sys
import time

behaviour = sys.argv[1] if len(sys.argv) > 1 else "ok"
folder = sys.argv[2] if len(sys.argv) > 2 else None
out = sys.stdout.buffer

ETHANOL_MOLFILE = """
     RDKit          2D

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.2990    0.7500    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5981   -0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  1  0
M  END
"""


def send(message):
    out.write((json.dumps(message) + "\n").encode("ascii"))
    out.flush()


starts = 1
if folder:
    path = os.path.join(folder, "starts")
    try:
        with open(path) as handle:
            starts = int(handle.read()) + 1
    except (OSError, ValueError):
        starts = 1
    with open(path, "w") as handle:
        handle.write(str(starts))

send({"event": "hello", "worker": 1})
sys.stderr.write("a library says something on stderr\n")
sys.stderr.flush()
if behaviour == "fail_load":
    send({"event": "failed", "stage": "load", "error": "ImportError"})
    sys.exit(3)
if behaviour == "die_loading":
    sys.exit(1)
if behaviour == "slow_load":
    time.sleep(1.0)
if behaviour == "never_ready":
    time.sleep(600)
if behaviour == "garbage":
    out.write(b"this is not JSON\n[1, 2, 3]\n\xff\xfe\x00\n\n")
    out.flush()
send({"event": "ready", "load_seconds": 0.25})


def result(request):
    if request.get("mode") == "molecule":
        return {"width": 400, "height": 200, "reactions": [], "timing": {},
                "molecules": [{"box": [0, 0, 400, 200], "smiles": "CCO",
                               "molfile": ETHANOL_MOLFILE, "confidence": 0.93}]}
    return {"width": 400, "height": 200, "timing": {"image_chars": len(request.get("image", ""))},
            "molecules": [
                {"box": [10, 20, 110, 120], "smiles": "CC=O", "molfile": None, "confidence": 0.91},
                {"box": [290, 20, 390, 120], "smiles": "CCO", "molfile": ETHANOL_MOLFILE,
                 "confidence": 0.62}],
            "reactions": [{"reactants": [0], "agents": [], "products": [1],
                           "texts": [{"box": [130, 30, 270, 60], "role": "conditions",
                                      "lines": ["NaBH4", "MeOH, 0 C"]}]}]}


for line in sys.stdin.buffer:
    request = json.loads(line)
    if request.get("op") == "shutdown":
        sys.exit(0)
    if behaviour == "crash" or (behaviour == "crash_once" and starts == 1):
        os._exit(7)
    if behaviour == "hang":
        time.sleep(600)
    if behaviour == "model_error":
        send({"id": request["id"], "ok": False, "stage": "layout", "error": "RuntimeError"})
        continue
    if behaviour == "image_error":
        send({"id": request["id"], "ok": False, "stage": "image", "error": "UnidentifiedImageError"})
        continue
    if behaviour == "bad_shape":
        send({"id": request["id"], "ok": True, "result": {"molecules": "none"}})
        continue
    if behaviour == "garbage":
        out.write(b"noise between the answers\n")
        send({"id": 999999, "ok": True, "result": "an answer nobody asked for"})
    send({"id": request["id"], "ok": True, "result": result(request)})

# stdin closed: the host is gone.
if folder:
    with open(os.path.join(folder, "ended-%d" % starts), "w") as handle:
        handle.write("stdin closed")
