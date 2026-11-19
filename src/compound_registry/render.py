"""Structure thumbnails as standalone SVG.

The thumbnail is drawn from the stored molfile itself, so the orientation and
the wedges the chemist drew are kept (RDKit only computes coordinates when the
file has none, or has 3D coordinates). Stereo annotation is switched on: CIP
labels (R/S, E/Z) and enhanced-stereo group labels (abs / and1 / or1).

:func:`svg_thumbnail` never raises; anything it cannot draw becomes a small
"no preview" placeholder, so a bad thumbnail can never break a lookup or a
registration.
"""

from __future__ import annotations

import re

from rdkit import Chem, rdBase
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

__all__ = ["svg_thumbnail", "placeholder_svg"]

_PROLOG_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*")
_COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def placeholder_svg(width: int = 240, height: int = 160) -> str:
    """A plain grey box with the text "no preview"."""
    w, h = _safe_size(width, 240), _safe_size(height, 160)
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}px' height='{h}px' "
        f"viewBox='0 0 {w} {h}'>"
        f"<rect x='0' y='0' width='{w}' height='{h}' style='fill:#F4F4F4;stroke:#CCCCCC'/>"
        f"<text x='{w / 2:.1f}' y='{h / 2:.1f}' text-anchor='middle' dominant-baseline='middle' "
        "style='font-family:sans-serif;font-size:14px;fill:#888888'>no preview</text>"
        "</svg>"
    )


def svg_thumbnail(molblock: str, width: int = 240, height: int = 160) -> str:
    """SVG drawing of ``molblock`` (V2000 or V3000), ``<svg ...>...</svg>``.

    No XML prolog, no scripts, no external references. Returns
    :func:`placeholder_svg` on any failure.
    """
    try:
        w, h = _safe_size(width, 240), _safe_size(height, 160)
        if not isinstance(molblock, str) or not molblock.strip():
            return placeholder_svg(w, h)
        with rdBase.BlockLogs():
            mol = _parse(molblock)
            if mol is None:
                return placeholder_svg(w, h)
            drawer = rdMolDraw2D.MolDraw2DSVG(w, h)
            opts = drawer.drawOptions()
            opts.addStereoAnnotation = True      # R/S, E/Z and AND/OR group labels
            opts.addStereoGroupAnnotation = True
            prepared = rdMolDraw2D.PrepareMolForDrawing(
                mol, kekulize=True, addChiralHs=True, wedgeBonds=False
            )
            drawer.DrawMolecule(prepared)
            drawer.FinishDrawing()
            text = drawer.GetDrawingText()
        return _clean(text) or placeholder_svg(w, h)
    except Exception:  # noqa: BLE001 - a thumbnail must never break the caller
        return placeholder_svg(width, height)


# --------------------------------------------------------------------------- helpers

def _safe_size(value: object, default: int) -> int:
    """``value`` if it is a sensible pixel size (16..4000), else ``default``."""
    if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 4000:
        return default
    return value


def _parse(molblock: str) -> Chem.Mol | None:
    """Molecule with the drawn coordinates and wedges, or None."""
    text = molblock.replace("\r\n", "\n").replace("\r", "\n")
    mol = None
    for sanitize in (True, False):
        try:
            mol = Chem.MolFromMolBlock(text, sanitize=sanitize, removeHs=False, strictParsing=False)
        except Exception:  # noqa: BLE001
            mol = None
        if mol is not None:
            break
    if mol is None or mol.GetNumAtoms() == 0:
        return None
    if not sanitize:
        mol.UpdatePropertyCache(strict=False)
        Chem.FastFindRings(mol)
    if mol.GetNumConformers() == 0 or _needs_2d(mol):
        # No usable drawing: lay it out and let RDKit place the wedges.
        rdDepictor.Compute2DCoords(mol)
        Chem.WedgeMolBonds(mol, mol.GetConformer())
    else:
        # Keep the wedges exactly as drawn rather than letting RDKit choose.
        Chem.ReapplyMolBlockWedging(mol)
    return mol


def _needs_2d(mol: Chem.Mol) -> bool:
    """True for 3D coordinates or a file whose atoms all sit on one point."""
    conf = mol.GetConformer()
    if conf.Is3D():
        return True
    points = set()
    for i in range(mol.GetNumAtoms()):
        p = conf.GetAtomPosition(i)
        points.add((round(p.x, 3), round(p.y, 3)))
    return mol.GetNumAtoms() > 1 and len(points) == 1


def _clean(svg: str) -> str:
    """Drop the XML prolog and comments; refuse anything that is not plain SVG."""
    svg = _PROLOG_RE.sub("", svg)
    svg = _COMMENT_RE.sub("", svg).strip()
    lowered = svg.lower()
    if not lowered.startswith("<svg") or not lowered.endswith("</svg>"):
        return ""
    if "<script" in lowered or "href" in lowered or "<foreignobject" in lowered:
        return ""
    return svg
