#!/usr/bin/env python3
"""Set the version of the whole project, or check that it is the same everywhere.

    python scripts/set-version.py 1.2.3     write 1.2.3 into every file below
    python scripts/set-version.py --check   exit 1 when the files disagree

The program and the browser extension carry one version number. It is written
in four files:

* ``src/compound_registry/__init__.py``   ``__version__`` (pyproject.toml reads it)
* ``extension/package.json``              ``version``
* ``extension/package-lock.json``         ``version`` and ``packages[""].version``
* ``extension/static/manifest.json``      ``version``

Only the version text is replaced; everything else in the files stays as it
is. Needs nothing but the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
INIT_FILE = "src/compound_registry/__init__.py"
PACKAGE_FILE = "extension/package.json"
LOCK_FILE = "extension/package-lock.json"
MANIFEST_FILE = "extension/static/manifest.json"

_INIT_RE = re.compile(r"""^(__version__\s*=\s*)(["'])([^"']*)\2""", re.MULTILINE)
_JSON_RE = re.compile(r'("version"\s*:\s*")([^"]*)(")')


class VersionError(Exception):
    """A file is missing or does not hold a version where one is expected."""


def _read(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError as exc:
        raise VersionError(f"cannot read {path}: {exc}") from None


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _json(path: Path, text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise VersionError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise VersionError(f"{path} does not hold a JSON object")
    return data


def read_versions(root: Path) -> dict[str, str]:
    """Every place that holds the version, as ``{"file: key": version}``."""
    root = Path(root)
    found: dict[str, str] = {}
    init = _INIT_RE.search(_read(root / INIT_FILE))
    if init is None:
        raise VersionError(f"no __version__ = \"...\" line in {root / INIT_FILE}")
    found[f"{INIT_FILE}: __version__"] = init.group(3)
    for name in (PACKAGE_FILE, MANIFEST_FILE, LOCK_FILE):
        data = _json(root / name, _read(root / name))
        found[f"{name}: version"] = str(data.get("version"))
        if name == LOCK_FILE:
            own = data.get("packages", {}).get("", {})
            found[f'{name}: packages[""].version'] = str(own.get("version"))
    return found


def _replace_json(path: Path, text: str, version: str, count: int) -> str:
    """Replace the first ``count`` ``"version": "..."`` of a JSON text (the
    project's own version comes before those of its dependencies)."""
    new, done = _JSON_RE.subn(lambda m: m.group(1) + version + m.group(3), text, count=count)
    if done != count:
        raise VersionError(f"{path}: expected {count} \"version\" value(s) at the top, found {done}")
    return new


def set_version(root: Path, version: str) -> list[str]:
    """Write ``version`` into every file; returns the files that changed.
    Nothing is written unless every file can be updated."""
    root = Path(root)
    if not VERSION_RE.match(version):
        raise VersionError(f"{version!r} is not a version of the form X.Y.Z (numbers only)")
    texts: dict[str, str] = {}
    init = _read(root / INIT_FILE)
    new_init, done = _INIT_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{version}{m.group(2)}",
                                   init, count=1)
    if done != 1:
        raise VersionError(f"no __version__ = \"...\" line in {root / INIT_FILE}")
    texts[INIT_FILE] = new_init
    for name, count in ((PACKAGE_FILE, 1), (MANIFEST_FILE, 1), (LOCK_FILE, 2)):
        new = _replace_json(root / name, _read(root / name), version, count)
        data = _json(root / name, new)
        ok = data.get("version") == version
        if name == LOCK_FILE:
            ok = ok and data.get("packages", {}).get("", {}).get("version") == version
        if not ok:
            raise VersionError(f"{root / name}: the version is not where it is expected")
        texts[name] = new
    changed = [name for name, text in texts.items() if text != _read(root / name)]
    for name in changed:
        _write(root / name, texts[name])
    return changed


def check(root: Path) -> tuple[bool, dict[str, str]]:
    """``(all the same and well-formed, read_versions(root))``."""
    found = read_versions(root)
    values = set(found.values())
    return len(values) == 1 and bool(VERSION_RE.match(next(iter(values)))), found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="set-version.py",
        description="Set the version of the program and the browser extension, or check it.")
    parser.add_argument("version", nargs="?", help="the new version, X.Y.Z")
    parser.add_argument("--check", action="store_true",
                        help="change nothing; exit 1 when the files disagree")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="the project folder (default: the one this script is in)")
    args = parser.parse_args(argv)
    if args.check == (args.version is not None):
        parser.error("give a version X.Y.Z, or --check")
    try:
        if args.check:
            same, found = check(args.root)
            for place, value in found.items():
                print(f"{value:<10} {place}")
            if not same:
                print("error: the version is not the same everywhere; run "
                      "'python scripts/set-version.py X.Y.Z'", file=sys.stderr)
                return 1
            return 0
        changed = set_version(args.root, args.version)
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if changed:
        print(f"Version {args.version} written to:")
        for name in changed:
            print(f"  {name}")
    else:
        print(f"Every file already has version {args.version}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
