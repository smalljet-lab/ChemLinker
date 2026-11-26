"""Leftover ink of the real worker on a drawn picture. Runs under the Python of
the recognition environment (it needs OpenCV), started by
tests/test_recognition_remedy.py:

    <recognition env>/python -I leftover_ink_picture.py <recognition_worker.py>

The picture (invented): a reactant ring at the left, an arrow with text above
it, the digit "1" after the arrow head and a large product drawing at the
right. The layout "found" only the reactant, the text and the digit (like
RxnScribe on the user's scheme), so the product is leftover ink. MolScribe is
a fake that reads every crop as naphthalene with a given confidence.
Prints one JSON object.
"""
import importlib.util
import json
import math
import sys

import cv2
import numpy as np

spec = importlib.util.spec_from_file_location("worker_under_test", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

BLACK = (0, 0, 0)


def ring(image, cx, cy, r, start=90.0):
    points = [(cx + r * math.cos(math.radians(start + 60 * k)), cy - r * math.sin(math.radians(start + 60 * k)))
              for k in range(6)]
    for k in range(6):
        a, b = points[k], points[(k + 1) % 6]
        cv2.line(image, (round(a[0]), round(a[1])), (round(b[0]), round(b[1])), BLACK, 2, cv2.LINE_AA)
    return points


def bond(image, a, angle, length):
    b = (a[0] + length * math.cos(math.radians(angle)), a[1] - length * math.sin(math.radians(angle)))
    cv2.line(image, (round(a[0]), round(a[1])), (round(b[0]), round(b[1])), BLACK, 2, cv2.LINE_AA)
    return b


def picture():
    image = np.full((300, 900, 3), 255, dtype=np.uint8)
    # reactant: a ring with two substituents
    p = ring(image, 110, 150, 40)
    bond(image, p[0], 90, 40)
    bond(image, p[3], 270, 40)
    # arrow with a filled head, text above it
    cv2.line(image, (230, 150), (400, 150), BLACK, 2, cv2.LINE_AA)
    cv2.fillPoly(image, [np.array([[400, 141], [420, 150], [400, 159]], dtype=np.int32)], BLACK, cv2.LINE_AA)
    cv2.putText(image, "Na2SO4/HCOOH", (240, 135), cv2.FONT_HERSHEY_SIMPLEX, 0.5, BLACK, 1, cv2.LINE_AA)
    # the coefficient "1"
    cv2.putText(image, "1", (440, 158), cv2.FONT_HERSHEY_SIMPLEX, 0.6, BLACK, 1, cv2.LINE_AA)
    # product: two fused rings with a chain and branches
    a = ring(image, 560, 150, 40)
    b = ring(image, 560 + 2 * 40 * math.cos(math.radians(30)), 150, 40)
    c = bond(image, b[5], -30, 40)
    d = bond(image, c, 30, 40)
    bond(image, d, -30, 40)
    bond(image, d, 90, 40)
    bond(image, a[1], 150, 40)
    bond(image, a[2], 210, 40)
    return image


class FakeMolScribe:
    def __init__(self, confidence):
        self.confidence = confidence
        self.calls = []

    def predict_images(self, images, **kwargs):
        self.calls.append([image.shape for image in images])
        return [{"smiles": "c1ccc2ccccc2c1", "molfile": "M  END", "confidence": self.confidence} for _ in images]


class Models:
    def __init__(self, confidence):
        self.molscribe = FakeMolScribe(confidence)


image = picture()
reactant = [60, 60, 160, 240]
text = [236, 120, 352, 140]
digit = [436, 140, 452, 160]
boxes = [reactant, text, digit]
context = [{"reactants": [reactant], "conditions": [text], "products": [digit]}]

prep = worker.prepare(image)
groups = worker.candidates(prep, boxes)
passed = [g for g in groups if worker.shape_ok(g)]
out = {
    "L": prep["L"],
    "arrows": [{"tail": a["tail"], "head": a["head"]} for a in prep["arrows"]],
    "groups": [{"box": g["box"], "shape_ok": worker.shape_ok(g)} for g in groups],
}
models = Models(0.9)
found = worker.leftover_ink(models, image, boxes, context)
out["found"] = [{"box": m["box"], "seen": m["seen"], "source": m["source"], "smiles": m["smiles"], "guess": g}
                for m, g in found]
out["molscribe_calls"] = len(models.molscribe.calls)
unsure = Models(0.69)
out["found_low_confidence"] = len(worker.leftover_ink(unsure, image, boxes, context))
# Every box of the layout covers the product as well: nothing is left over.
out["found_all_covered"] = len(worker.leftover_ink(Models(0.9), image, boxes + [[470, 40, 800, 260]], context))
# The digit NOT covered by a box: it must not pass the shape filter on its own.
out["found_digit_uncovered"] = [m["box"] for m, _ in worker.leftover_ink(Models(0.9), image, [reactant, text,
                                                                                         [470, 40, 800, 260]],
                                                                         context)]
# The text NOT covered by a box: a line of letters is no structure either.
out["found_text_uncovered"] = [m["box"] for m, _ in worker.leftover_ink(Models(0.9), image, [reactant, digit,
                                                                                        [470, 40, 800, 260]],
                                                                        context)]
# No arrow in the picture (arrow painted out): found, but without a role.
plain = image.copy()
plain[138:162, 225:425] = 255
plain[110:145, 230:360] = 255
found = worker.leftover_ink(Models(0.9), plain, [reactant, digit], [{"reactants": [reactant], "conditions": [],
                                                                     "products": [digit]}])
out["no_arrow"] = [g for _, g in found]
# A picture above the pixel budget is searched on a reduced copy: the same
# product, its box and "seen" back in the picture's pixels, MolScribe given
# the reduced crop.
reduced = Models(0.9)
found = worker.leftover_ink(reduced, image, boxes, context, max_pixels=image.shape[0] * image.shape[1] // 4)
out["reduced"] = [{"box": m["box"], "seen": m["seen"], "guess": g} for m, g in found]
out["reduced_crop_shapes"] = reduced.molscribe.calls
out["full_crop_shapes"] = models.molscribe.calls
# The installed RxnScribe is the commit the peek decoding was written for.
out["rxnscribe_pinned"] = worker.rxnscribe_is_pinned()
print(json.dumps(out))
