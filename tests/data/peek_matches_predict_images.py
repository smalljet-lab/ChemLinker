"""The peek decoding of the real worker against RxnScribe's own predict_images,
with the real models. Runs under the Python of the recognition environment,
started by tests/test_recognition_live.py:

    <recognition env>/python -I peek_matches_predict_images.py <recognition_worker.py> <models> PICTURE...

For every picture: the reactions parsed from the MAIN sequence of the peek
decoding must be exactly those of predict_images, and the one-pass check must
agree. Prints one JSON object (no structure, no text of the pictures).
"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("worker_under_test", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
models_dir = sys.argv[2]
worker._set_environment(models_dir)
models = worker.load_models(models_dir)

from PIL import Image  # noqa: E402


def boxes(reactions):
    return [[(role, b.get("category_id"), [round(float(v), 6) for v in b["bbox"]])
             for role in ("reactants", "conditions", "products") for b in r.get(role) or []] for r in reactions]


out = []
for path in sys.argv[3:]:
    picture = Image.open(path).convert("RGB")
    tok = models.rxn.tokenizer["reaction"]
    advance, check, scale, parse = worker._decoder(models, picture)
    decoded = worker.decode_peek(advance, tok, max_len=tok.max_len)
    main = parse(decoded["seq"], decoded["scores"], scale) if decoded["eos"] else []
    base = models.rxn.predict_images([picture], molscribe=False, ocr=False)[0]
    reactions, peek_boxes, info = worker.layout(models, picture)
    out.append({"same": boxes(main) == boxes(base), "holds": worker.main_sequence_holds(
        check(decoded["seq"]), decoded["seq"], tok, eos=decoded["eos"]),
        "eos": decoded["eos"], "triggers": len(decoded["peeks"]), "added": len(peek_boxes), "info": info,
        "reactions": len(base)})
print(json.dumps(out))
