"""Command-line interface: ``compound-registry <command> ...``.

Every command goes through :class:`~compound_registry.service.RegistryService`.
Commands are registered in :data:`COMMANDS`; adding one (for example
``install-host``) means writing an ``add_<name>_parser`` function and a handler
and listing them there.

Output rules
------------
* Human output goes to stdout, diagnostics and errors to stderr; everything is
  plain ASCII (non-ASCII characters are shown as ``\\xNN`` escapes) and has no
  colour codes, so it works on any Windows console.
* ``--json``: exactly one JSON document on stdout, also for errors
  (``{"error": {"code": ..., "message": ...}}``). JSON mode never prompts.
* Exit codes: 0 success, 1 a registry error (or a refused / cancelled
  confirmation, a failed check), 2 a usage error.

Safety rule for ``register``
----------------------------
An issued ID is permanent. When a registration would create a NEW stereo
variant (status ``new`` or ``known_core``) and neither ``--mode`` nor ``--yes``
was given, the user must confirm the stereo mode interactively; when nobody
can be asked (not a terminal, ``--stdin`` or ``--json``) the command refuses.
Drawn stereo that is not part of the keys (``ignored_stereo``) needs its own
explicit ``yes`` (or ``--accept-ignored-stereo``); ``--yes`` does not cover it.

Creating a registry
-------------------
Only ``init`` and the installer (``setup``, ``install-host
--create-registry``) create a registry.
Every other command refuses with ``registry_missing`` when the home folder,
its ``config.toml`` or the database does not exist, because a new, empty
registry in the place of a moved or unreachable one would issue IDs that
already exist.

``recover`` re-creates IDs that the issued-ID journal knows but the database
lost (after restoring an old backup); ``rekey-check --accept`` records a new
RDKit/InChI version after checking that every stored structure keeps its keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import install_host
from .errors import RegistryError, SlotConflictError
from .models import MODE_OVERRIDES
from .protocol import MATCH_MODES, MAX_PRODUCT_INDEX
from .service import EXPORT_FORMATS, RegistryService, create_registry

PROG = "compound-registry"
NEW_VARIANT_STATUSES = ("new", "known_core")


# --------------------------------------------------------------------------- output

def _ascii(text: str) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def out(text: str = "") -> None:
    """One line of human output on stdout (ASCII only)."""
    sys.stdout.write(_ascii(text) + "\n")


def err(text: str) -> None:
    """One line of diagnostics on stderr (ASCII only)."""
    sys.stderr.write(_ascii(text) + "\n")


def emit_json(document: Any) -> None:
    """The single JSON document of ``--json`` mode (ASCII, ``\\uXXXX`` escapes)."""
    sys.stdout.write(json.dumps(document, indent=2, ensure_ascii=True) + "\n")


def _is_interactive() -> bool:
    """True when a person can answer a prompt (stdin and stdout are terminals)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _stdin_is_terminal() -> bool:
    """True when a person can type answers (stdin is a terminal)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _ask(prompt: str) -> str:
    """Read one answer; EOF counts as an empty answer."""
    try:
        return input(_ascii(prompt)).strip()
    except EOFError:
        return ""


@dataclass
class Context:
    """What every handler gets besides its parsed arguments."""

    args: argparse.Namespace
    json: bool
    home: Path | None

    def service(self) -> RegistryService:
        """Open the EXISTING registry. No command but ``init`` and the
        installer (``setup``, ``install-host --create-registry``) creates one: a new, empty
        registry in the place of a moved or unreachable one would issue IDs
        that already exist."""
        location = install_host.locate_registry(self.home)
        if not location.exists:
            raise install_host.registry_missing_error(location, cli=True)
        return RegistryService(location.config)

    @property
    def can_prompt(self) -> bool:
        return not self.json and _is_interactive()


class CliRefusal(RegistryError):
    """A confirmation was required but not given (or the user said no)."""

    code = "confirmation_required"


# --------------------------------------------------------------------------- input

def _read_text_file(path: str) -> str:
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise RegistryError(f"Cannot read {path}: {exc.strerror or exc}", code="file_error") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # old ChemDraw/ISIS files; only header text can differ


def _structure_from_args(args: argparse.Namespace) -> dict[str, str] | None:
    """``{"molfile": ...}`` or ``{"smiles": ...}`` from --molfile/--smiles/--stdin, or None."""
    if getattr(args, "molfile", None) is not None:
        return {"molfile": _read_text_file(args.molfile)}
    if getattr(args, "smiles", None) is not None:
        return {"smiles": args.smiles}
    if getattr(args, "stdin", False):
        text = sys.stdin.read()
        stripped = text.strip()
        if "\n" in stripped or "M  END" in text:
            return {"molfile": text}
        return {"smiles": stripped}
    return None


# --------------------------------------------------------------------------- formatting

def _experiment_text(b: dict[str, Any]) -> str:
    exp = b["experiment_ref"]
    if b.get("experiment_code"):
        exp = f"{b['experiment_code']} (ref {b['experiment_ref']})"
    return f"{exp}, product {b['product_index']}, registered {b['created_at']}"


def _batch_line(b: dict[str, Any]) -> str:
    line = f"{b['full_id']}  experiment {_experiment_text(b)}"
    if b.get("retired"):
        line += f"  [RETIRED {b['retired_at']}: {b['retired_reason']}]"
    return line


def _print_structure(s: dict[str, Any]) -> None:
    mode = s["stereo_mode"]
    if s.get("mode_overridden") and s.get("detected_mode") != mode:
        mode += f" (detected: {s['detected_mode']})"
    elif s.get("mode_overridden"):
        mode += " (chosen)"
    out(f"  SMILES       {s['smiles']}")
    out(f"  Formula      {s['formula']}")
    out(f"  InChIKey     {s['inchikey']}")
    out(f"  Stereo mode  {mode}")


def _print_warnings(warnings: list[str]) -> None:
    if warnings:
        out("Warnings:")
        for w in warnings:
            out(f"  - {w}")


_STATUS_TEXT = {
    "registered_here": "already registered in this experiment",
    "known_compound": "known compound (this stereo variant is registered)",
    "known_core": "known core, NEW stereo variant",
    "new": "NEW compound",
}


def _print_analysis(a: dict[str, Any]) -> None:
    out(f"Status         {_STATUS_TEXT.get(a['status'], a['status'])}")
    if a["id"]:
        out(f"ID here        {a['id']}")
    if a["core_stereo_id"]:
        out(f"CORE_STEREO    {a['core_stereo_id']}")
    elif a["core_no"] is not None:
        out(f"CORE           {a['core_no']} (this stereo variant is not registered)")
    out("Structure")
    _print_structure(a["structure"])
    if a["batches"]:
        out(f"Batches of this compound ({len(a['batches'])}):")
        for b in a["batches"]:
            out(f"  {_batch_line(b)}")
    if a["other_stereos"]:
        out(f"Other stereo variants of the same core ({len(a['other_stereos'])}):")
        for s in a["other_stereos"]:
            out(f"  {s['core_stereo_id']}  mode {s['stereo_mode']}, {s['batch_count']} batch(es), "
                f"{s['smiles']}")
    if a.get("slot_conflict"):
        c = a["slot_conflict"]
        out(f"Slot taken     product {c['product_index']} of this experiment is already "
            f"{c['full_id']} (a different compound)")
    _print_warnings(list(a["structure"]["warnings"]) + list(a["warnings"]))


# --------------------------------------------------------------------------- register

def _confirm_new_variant(ctx: Context, svc: RegistryService, structure: dict[str, str],
                         analysis: dict[str, Any], analyze_kwargs: dict[str, Any]) -> str | None:
    """Ask the user to confirm the stereo mode of a new stereo variant.

    Returns the mode to register with (None = as detected / configured
    default). Raises :class:`CliRefusal` when nobody can be asked or the user
    cancels.
    """
    detected = analysis["structure"]["stereo_mode"]
    if not ctx.can_prompt:
        if detected == "none":
            hint = ("The structure has no stereo elements; pass --yes to confirm the "
                    "registration.")
        else:
            hint = (f"Pass --mode {'|'.join(MODE_OVERRIDES)} to state the stereo mode, or "
                    f"--yes to accept '{detected}'.")
        raise CliRefusal(
            f"This registration would create a NEW stereo variant (stereo mode "
            f"'{detected}'), and an issued ID is permanent. {hint}"
        )
    mode: str | None = None
    current = analysis
    while True:
        out()
        _print_analysis(current)
        out()
        shown = current["structure"]["stereo_mode"]
        answer = _ask(
            f"Register as a new stereo variant with stereo mode '{shown}'?\n"
            f"  y = yes, register | {' / '.join(MODE_OVERRIDES)} = use another mode | "
            f"n = cancel\n> "
        ).lower()
        if answer in ("y", "yes"):
            return mode
        if answer in MODE_OVERRIDES:
            if answer != shown:
                mode = answer
            current = svc.analyze(**structure, **analyze_kwargs, mode=mode, with_svg=False)
            continue
        if answer in ("", "n", "no", "q", "quit"):
            raise CliRefusal("Registration cancelled; nothing was registered.", code="cancelled")
        out(f"Please answer y, n or one of {', '.join(MODE_OVERRIDES)}.")


def _confirm_ignored_stereo(ctx: Context, analysis: dict[str, Any]) -> bool:
    """Ask for an explicit ``yes`` to register a structure whose drawn stereo
    is partly NOT in the keys. Returns True when accepted. When nobody can be
    asked, returns False (the service then refuses with ``stereo_not_in_key``);
    raises :class:`CliRefusal` when the user declines."""
    items = analysis["structure"]["ignored_stereo"]
    if not ctx.can_prompt:
        return False
    out()
    out("WARNING: this drawing contains stereo that is NOT part of the registry keys:")
    for item in items:
        out(f"  - {item}")
    out("Stereoisomers that differ only in these features would get the SAME ID.")
    answer = _ask("Type 'yes' to register anyway (anything else cancels): ")
    if answer.lower() != "yes":
        raise CliRefusal("Registration cancelled; nothing was registered.", code="cancelled")
    return True


def cmd_register(ctx: Context) -> int:
    args = ctx.args
    structure = _structure_from_args(args)
    assert structure is not None  # argparse requires one source
    with ctx.service() as svc:
        # Stale database / changed chemistry: stop before asking anything.
        svc.check_writable(registering=True)
        analyze_kwargs = {"experiment_ref": args.experiment_ref, "product_index": args.product_index}
        mode = args.mode
        analysis = svc.analyze(**structure, **analyze_kwargs, mode=mode, with_svg=False)
        if analysis["id"] is None and analysis["slot_conflict"] is not None:
            c = analysis["slot_conflict"]
            raise SlotConflictError(
                f"Product {args.product_index} of experiment {args.experiment_ref} is already "
                f"registered as {c['full_id']}, a different compound. Retire {c['full_id']} "
                f"first if it was wrong. Nothing was registered."
            )
        if analysis["status"] in NEW_VARIANT_STATUSES and mode is None and not args.yes:
            mode = _confirm_new_variant(ctx, svc, structure, analysis, analyze_kwargs)
            analysis = svc.analyze(**structure, **analyze_kwargs, mode=mode, with_svg=False)
        accept_ignored = bool(args.accept_ignored_stereo)
        if (analysis["structure"]["ignored_stereo"] and analysis["status"] != "registered_here"
                and not accept_ignored):
            accept_ignored = _confirm_ignored_stereo(ctx, analysis)
        result = svc.register(
            **structure,
            mode=mode,
            experiment_ref=args.experiment_ref,
            experiment_code=args.experiment_code,
            experiment_url=args.experiment_url,
            product_index=args.product_index,
            source="cli",
            accept_ignored_stereo=accept_ignored,
        )
    if ctx.json:
        emit_json(result)
        return 0
    banner = "=" * 48
    state = "NEW - registered now" if result["created"] else "already registered - existing ID"
    out(banner)
    out(f"  ID  {result['id']}   ({state})")
    out(banner)
    b = result["batch"]
    out(f"Experiment   {_experiment_text(b)}")
    if result["created"]:
        made = []
        if result["core_created"]:
            made.append("new CORE")
        if result["stereo_created"]:
            made.append("new stereo variant")
        made.append(f"batch {b['batch_no']}")
        out(f"Created      {', '.join(made)}")
    _print_structure(result["structure"])
    if result["accepted_ignored_stereo"]:
        out("Accepted     stereo not in the key: "
            + "; ".join(result["accepted_ignored_stereo"]))
    bk = result["backup"]
    if bk["performed"]:
        out(f"Backup       {bk['path']}")
    _print_warnings(result["warnings"])
    return 0


# --------------------------------------------------------------------------- lookup

def cmd_lookup(ctx: Context) -> int:
    args = ctx.args
    structure = _structure_from_args(args)
    with ctx.service() as svc:
        if args.id is not None:
            result: Any = svc.get(args.id)
            kind = "id"
        elif structure is not None:
            result = svc.analyze(**structure, mode=args.mode, experiment_ref=args.experiment_ref,
                                 with_svg=False)
            kind = "structure"
        else:
            result = {"experiment_ref": args.experiment_ref.strip(),
                      "batches": svc.experiment(args.experiment_ref)}
            kind = "experiment"
    if ctx.json:
        emit_json(result)
        return 0
    if kind == "id":
        out(f"ID           {result['full_id']}{'  [RETIRED]' if result['retired'] else ''}")
        out(f"CORE_STEREO  {result['core_stereo_id']}")
        out(f"Experiment   {_experiment_text(result)}")
        if result["experiment_url"]:
            out(f"URL          {result['experiment_url']}")
        out(f"SMILES       {result['smiles']}")
        out(f"InChIKey     {result['inchikey']}")
        out(f"Stereo mode  {result['stereo_mode']}")
        out(f"Source       {result['source']} (RDKit {result['rdkit_version']}, "
            f"InChI {result['inchi_version']})")
        if result["retired"]:
            out(f"Retired      {result['retired_at']}: {result['retired_reason']}")
    elif kind == "structure":
        _print_analysis(result)
    else:
        batches = result["batches"]
        out(f"Experiment {result['experiment_ref']}: {len(batches)} batch(es)")
        for b in batches:
            out(f"  {_batch_line(b)}  {b['smiles']}")
    return 0


# --------------------------------------------------------------------------- experiments

def cmd_experiments(ctx: Context) -> int:
    """All experiments in which the compound named by an ID was made."""
    with ctx.service() as svc:
        result = svc.find_by_id(ctx.args.id, with_svg=False)
    if ctx.json:
        emit_json(result)
        return 0
    what = {"batch": "the compound of batch", "stereo": "compound", "core": "CORE"}[result["level"]]
    out(f"Experiments for {what} {result['query']}")
    for v in result["variants"]:
        out(f"  {v['core_stereo_id']}  mode {v['stereo_mode']}, {v['batch_count']} batch(es), "
            f"{v['smiles']}")
    out(f"{len(result['experiments'])} experiment(s):")
    for e in result["experiments"]:
        name = e["experiment_code"] or "(no code)"
        ids = ", ".join(
            b["full_id"] + (" [RETIRED]" if b["retired"] else "") for b in e["batches"])
        out(f"  {name}  (key {e['experiment_ref']})  {ids}")
        if e["experiment_url"]:
            out(f"      {e['experiment_url']}")
    if result["other_variants"]:
        out("Other stereo variants of the same CORE (not listed above):")
        for v in result["other_variants"]:
            out(f"  {v['core_stereo_id']}  mode {v['stereo_mode']}, {v['batch_count']} batch(es)")
    _print_warnings(result["warnings"])
    return 0


# --------------------------------------------------------------------------- compounds / names

def cmd_compounds(ctx: Context) -> int:
    """Every registered CORE (optionally filtered), with its stereo variants."""
    args = ctx.args
    structure = _structure_from_args(args) or {}
    with ctx.service() as svc:
        result = svc.compounds(text=args.text, molfile=structure.get("molfile"),
                               smiles=structure.get("smiles"), match=args.match,
                               limit=None, with_svg=False)
    if ctx.json:
        emit_json(result)
        return 0
    searching = args.text is not None or bool(structure)
    if searching:
        out(f"{result['total']} of {result['registry_total']} compound(s) match")
    else:
        out(f"{result['registry_total']} compound(s)")
    for note in result["notes"]:
        out(f"  ({note})")
    for item in result["items"]:
        props = item["properties"]
        what = (f"{props['sum_formula']}  {props['mol_weight']:.3f} g/mol" if props
                else item["formula"])
        line = (f"{item['core_id']}  {what}  {item['batch_count']} batch(es) in "
                f"{item['experiment_count']} experiment(s)")
        if item["matched_by"]:
            line += f"  [matched: {', '.join(item['matched_by'])}]"
        out(line)
        for v in item["variants"]:
            mark = "*" if searching and v["matched"] else " "
            out(f" {mark}{v['core_stereo_id']}  mode {v['stereo_mode']}, {v['batch_count']} "
                f"batch(es)  {v['smiles']}")
            if v["name"]:
                out(f"      name: {v['name']['text']}")
    if searching and any(v["matched"] for i in result["items"] for v in i["variants"]):
        out("(* = this stereo variant itself matched)")
    _print_warnings(result["warnings"])
    return 0


def _name_source_text(name: dict[str, Any]) -> str:
    return " by ChemDraw" if name.get("source") == "chemdraw" else ""


def cmd_name(ctx: Context) -> int:
    """Show, store, generate or remove the name of one stereo variant."""
    args = ctx.args
    with ctx.service() as svc:
        if args.remove:
            result = svc.set_name(args.core_stereo, None)
        elif args.generate:
            result = svc.generate_name(args.core_stereo, replace=args.replace)
        elif args.name is not None:
            result = svc.set_name(args.core_stereo, args.name)
        else:
            result = svc.get_name(args.core_stereo)
    if ctx.json:
        emit_json(result)
        return 0
    cs, name = result["core_stereo_id"], result["name"]
    if "generated" in result:
        if result["generated"]:
            state = "generated" if result["changed"] else "unchanged (generated again)"
            out(f"Name of {cs} {state} by ChemDraw: {name['text']}")
        else:
            out(f"No name was generated for {cs}: {result['message']}")
            if name is not None:
                out(f"Stored name{_name_source_text(name)}: {name['text']}")
        _print_warnings(result["warnings"])
        return 0
    if "changed" not in result:
        if name is None:
            out(f"{cs}  (no name stored)")
        else:
            out(f"{cs}  {name['text']}")
            out(f"{' ' * len(cs)}  (stored{_name_source_text(name)} {name['updated_at']})")
        return 0
    if name is None:
        out(f"Name of {cs} removed." if result["changed"]
            else f"{cs} has no stored name; nothing was changed.")
    else:
        out(f"Name of {cs} {'stored' if result['changed'] else 'unchanged'}: {name['text']}")
    _print_warnings(result["warnings"])
    return 0


def cmd_spectra(ctx: Context) -> int:
    """The spectra the MestReNova add-on recorded (multiplet reports, files)."""
    with ctx.service() as svc:
        result = svc.spectra(ctx.args.id)
        now = {} if ctx.json else {
            (item["full_id"], item["spectrum_key"], field):
                svc.recorded_file(item["full_id"], item["data"].get(field))
            for item in result["spectra"] for field in ("mnova_file", "pdf_file")}
    if ctx.json:
        emit_json(result)
        return 0
    if not result["spectra"]:
        out("No spectrum is recorded" + (f" for {result['id']}." if result["id"] else "."))
        return 0
    for item in result["spectra"]:
        data = item["data"]
        out(f"{item['full_id']}  {item['spectrum_key']}  (recorded {item['updated_at']})")
        out(f"  {data.get('report') or '(no multiplet report)'}")
        for label, field in (("mnova", "mnova_file"), ("pdf", "pdf_file")):
            if data.get(field):
                where = now.get((item["full_id"], item["spectrum_key"], field))
                out(f"  {label}: {where or str(data[field]) + ' (not found)'}")
    return 0


def cmd_generate_names(ctx: Context) -> int:
    """Ask ChemDraw for the name of every stereo variant without a stored name."""
    args = ctx.args

    def progress(entry: dict[str, Any]) -> None:
        if ctx.json:
            return
        if entry["generated"]:
            out(f"{entry['core_stereo_id']}  {entry['name']['text']}")
        else:
            out(f"{entry['core_stereo_id']}  (no name) {entry['message']}")

    with ctx.service() as svc:
        result = svc.generate_names(limit=args.limit, progress=progress)
    if ctx.json:
        emit_json(result)
        return 0
    if result["tried"] == 0:
        out("Every stereo variant already has a name; nothing to do.")
    else:
        out(f"{result['generated']} of {result['tried']} stereo variant(s) named.")
    if result["stopped"]:
        out(f"Stopped early: {result['stopped']}")
    if result["remaining"]:
        out(f"{result['remaining']} variant(s) without a name were not tried.")
    _print_warnings(result["warnings"])
    return 0


# --------------------------------------------------------------------------- yields

def _percent_text(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def cmd_yields(ctx: Context) -> int:
    """One row per batch: experiment, product, ID, yields and name (read-only)."""
    args = ctx.args
    with ctx.service() as svc:
        result = svc.yields(text=args.text, only_recorded=args.only_recorded, limit=None)
    if ctx.json:
        emit_json(result)
        return 0
    if args.text is not None or args.only_recorded:
        out(f"{result['total']} of {result['registry_total']} batch(es) listed")
    else:
        out(f"{result['registry_total']} batch(es)")
    header = ("ELN number", "Product", "Full ID", "Isolated %", "Analytical %", "Name")
    rows = []
    for item in result["items"]:
        y = item["yield"] or {}
        rows.append((
            item["experiment_code"] or f"(key {item['experiment_ref']})",
            str(item["product_index"]),
            item["full_id"] + (" [RETIRED]" if item["retired"] else ""),
            _percent_text(y.get("isolated")),
            _percent_text(y.get("analytical")),
            item["name"] or "",
        ))
    if rows:
        widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
        for r in [header, *rows]:
            out(("  ".join(cell.ljust(w) for cell, w in zip(r, widths)) + "  " + r[-1]).rstrip())
    _print_warnings(result["warnings"])
    return 0


# --------------------------------------------------------------------------- reactions / routes

def _number_text(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _amounts_text(amounts: dict[str, Any] | None) -> str:
    """``eq 1, 8.232 mmol, 1500 mg`` (only the values that are known)."""
    if not amounts:
        return ""
    parts = []
    if amounts.get("eq") is not None:
        parts.append(f"eq {_number_text(amounts['eq'])}")
    for value, unit in (("n", "n_unit"), ("m", "m_unit"), ("v", "v_unit")):
        if amounts.get(value) is not None:
            parts.append(f"{_number_text(amounts[value])} {amounts.get(unit) or ''}".rstrip())
    return ", ".join(parts)


def _component_lines(component: dict[str, Any], shown: str | None) -> list[str]:
    name = component.get("name") or "(no name)"
    extra = [t for t in (component.get("formula"), component.get("cas")) if t]
    line = f"  {component['row']}. {name}" + (f" ({', '.join(extra)})" if extra else "")
    if component.get("source_experiment_ref"):
        source = component.get("source_experiment_code") or "an experiment"
        line += f"  [from {source}, key {component['source_experiment_ref']}]"
    if not component.get("molfile"):
        line += "  [no structure]"
    lines = [line]
    first = "planned" if shown == "planned" else "actual"
    for which in (first, "planned" if first == "actual" else "actual"):
        text = _amounts_text(component.get(which))
        if text:
            lines.append(f"       {which}: {text}")
    return lines


def cmd_reaction(ctx: Context) -> int:
    """Show the reaction stored for an experiment (read-only)."""
    with ctx.service() as svc:
        result = svc.reaction(ctx.args.experiment)
    if ctx.json:
        emit_json(result)
        return 0
    r = result["reaction"]
    title = result["experiment_code"] or "(no ELN number)"
    out(f"{title}  (internal key {result['experiment_ref']}, stored {result['updated_at']})")
    if result["experiment_url"]:
        out(f"  {result['experiment_url']}")
    shown = r.get("amounts_shown")
    for label, key in (("Reactants", "reactants"), ("Reagents", "reagents")):
        out(f"{label}:" if r.get(key) else f"{label}: none")
        for component in r.get(key) or []:
            for line in _component_lines(component, shown):
                out(line)
    solvents = [f"{s['name']}" + (f" {_number_text(s['volume'])} {s.get('unit') or ''}".rstrip()
                                  if s.get("volume") is not None else "")
                for s in r.get("solvents") or []]
    out(f"Solvents: {'; '.join(solvents) if solvents else 'none'}")
    out(f"Temperature: {r.get('temperature') or '-'}")
    ref = r.get("reference_amount")
    out(f"Reference amount: "
        f"{(_number_text(ref['value']) + ' ' + (ref.get('unit') or '')).rstrip() if ref else '-'}")
    if shown:
        out(f"Amounts shown on the page: {shown}")
    return 0


def cmd_route(ctx: Context) -> int:
    """Draw the synthesis route of a stereo variant as a ChemDraw file."""
    with ctx.service() as svc:
        result = svc.route(ctx.args.core_stereo, open=not ctx.args.no_open)
    failed = result["steps"] > 0 and result["file"] is None
    if ctx.json:
        emit_json(result)
        return 1 if failed else 0
    cs = result["core_stereo_id"]
    if result["steps"] == 0:
        out(f"No reaction that made {cs} is known (with a yield above zero); nothing was drawn.")
    else:
        out(f"{result['steps']} step(s) drawn for {cs}: {', '.join(result['experiments'])}")
        if result["file"] is not None:
            out(f"File: {result['file']['path']}")
            out("Opened with Windows." if result["opened"] else "Not opened.")
    if result["open_message"]:
        out(result["open_message"])
    if result["missing"]:
        out("Reactions not stored yet (show each experiment once in the ELN with the extension):")
        for m in result["missing"]:
            out(f"  - {m['experiment_code'] or m['experiment_ref']} (makes {m['compound']})")
    if result["notes"]:
        out("Notes:")
        for note in result["notes"]:
            out(f"  - {note}")
    _print_warnings(result["warnings"])
    return 1 if failed else 0


def cmd_limiting(ctx: Context) -> int:
    """Choose the starting material ("SM") of an experiment's run line."""
    with ctx.service() as svc:
        ref = svc.reaction(ctx.args.experiment)["experiment_ref"]
        result = svc.set_limiting(ref, ctx.args.row)
    if ctx.json:
        emit_json(result)
        return 0
    where = ctx.args.experiment.strip()
    named = where if where == result["experiment_ref"] else \
        f"{where} (internal key {result['experiment_ref']})"
    if result["row"] is None:
        chosen = "chosen automatically"
    else:
        chosen = f"reactant row {result['row']}" + (f" ({result['name']})" if result["name"]
                                                     else "")
    out(f"Starting material of {named}: {chosen}"
        + ("." if result["changed"] else " (already so; nothing changed)."))
    _print_warnings(result["warnings"])
    return 0


def cmd_folders(ctx: Context) -> int:
    """Make the missing compound folders (one per stereo variant)."""
    with ctx.service() as svc:
        result = svc.make_folders(ctx.args.core_stereo)
    if ctx.json:
        emit_json(result)
        return 0 if not result["failed"] else 1
    entries = result["folders"]
    if ctx.args.core_stereo is not None:
        entry = entries[0]
        if entry["path"] is not None:
            out(entry["path"])
        if entry["warning"]:
            err(f"warning: {entry['warning']}")
        return 0 if entry["path"] is not None else 1
    out(f"Compound folders in {result['files_folder']}")
    if not entries:
        out("No compound is registered yet.")
    for entry in entries:
        if entry["path"] is None:
            state = "NOT MADE"
        else:
            state = "created" if entry["created"] else "already there"
        out(f"{entry['core_stereo_id']}  {state}  {entry['path'] or '-'}")
        if entry["warning"]:
            out(f"  warning: {entry['warning']}")
    out(f"{result['created']} folder(s) created, {result['failed']} could not be made.")
    return 0 if not result["failed"] else 1


# --------------------------------------------------------------------------- export / backup

def cmd_export(ctx: Context) -> int:
    args = ctx.args
    with ctx.service() as svc:
        result = svc.export(args.format, args.output, include_retired=not args.active_only)
    if ctx.json:
        emit_json(result)
    else:
        what = "active batches" if args.active_only else "batches"
        out(f"Exported {result['count']} {what} to {result['path']}")
    return 0


def cmd_backup(ctx: Context) -> int:
    args = ctx.args
    with ctx.service() as svc:
        if args.list:
            backups = svc.list_backups()
            if ctx.json:
                emit_json({"backup_dir": str(svc.config.backup_dir), "backups": backups})
            else:
                out(f"Backups in {svc.config.backup_dir}: {len(backups)}")
                for b in backups:
                    out(f"  {b['name']}  {b['bytes']} bytes")
            return 0
        if args.verify is not None:
            result = svc.verify_backup(args.verify)
            if not result["ok"]:
                raise RegistryError(f"{result['path']} is not a usable backup: {result['result']}",
                                    code="backup_invalid")
            if ctx.json:
                emit_json(result)
            else:
                out(f"OK: {result['path']} is an intact registry backup.")
            return 0
        result = svc.backup(force=True)
    if not result["performed"]:
        raise RegistryError(result["warning"] or "The backup failed.", code="backup_failed")
    if ctx.json:
        emit_json(result)
    else:
        out(f"Backup written: {result['path']}")
        if result["removed"]:
            out(f"Old backups removed: {len(result['removed'])}")
        if result["warning"]:
            err(f"warning: {result['warning']}")
    return 0


# --------------------------------------------------------------------------- retire

def cmd_retire(ctx: Context) -> int:
    args = ctx.args
    reason = args.reason.strip()
    if not reason:
        raise RegistryError("A reason is required to retire a batch.", code="invalid_argument")
    with ctx.service() as svc:
        svc.check_writable(registering=False)
        batch = svc.get(args.full_id)
        if batch["retired"]:
            raise RegistryError(
                f"{batch['full_id']} was already retired at {batch['retired_at']}: "
                f"{batch['retired_reason']}", code="already_retired")
        if not args.yes:
            if not ctx.can_prompt:
                raise CliRefusal(
                    f"Retiring {batch['full_id']} is permanent (its number is never reused). "
                    "Pass --yes to confirm."
                )
            out(f"About to retire  {_batch_line(batch)}")
            out(f"                 {batch['smiles']}")
            out(f"Reason           {reason}")
            answer = _ask("Retire this batch? Its number will never be reused. [y/N] ").lower()
            if answer not in ("y", "yes"):
                raise CliRefusal("Cancelled; nothing was retired.", code="cancelled")
        result = svc.retire(batch["full_id"], reason)
    if ctx.json:
        emit_json(result)
        return 0
    out(f"Retired {result['id']}: {result['batch']['retired_reason']}")
    if result["backup"]["performed"]:
        out(f"Backup  {result['backup']['path']}")
    _print_warnings(result["warnings"])
    return 0


# --------------------------------------------------------------------------- maintenance

def cmd_rekey_check(ctx: Context) -> int:
    accept = bool(getattr(ctx.args, "accept", False))
    with ctx.service() as svc:
        result = svc.rekey_check(accept=accept)
    if ctx.json:
        emit_json(result)
    else:
        fp = result["chemistry_fingerprint"]
        out(f"RDKit {result['rdkit_version']}, InChI {result['inchi_version']}")
        out(f"Chemistry       stored {fp['stored'] or '(none)'}")
        out(f"                current {fp['current']}")
        out(f"Checked {result['checked']} batch(es): {result['ok']} OK, "
            f"{len(result['mismatches'])} mismatch(es)")
        for m in result["mismatches"]:
            out(f"  {m['id']}: stored {m['stored']}, recomputed {m['recomputed']} - {m['reason']}")
        if result["accepted"]:
            out("Accepted: the current chemistry version is now recorded; registering is allowed.")
    if result["mismatches"]:
        if not ctx.json:
            if accept:
                err("error [rekey_mismatch]: some stored structures no longer give the same keys; "
                    "the new chemistry version was NOT accepted and registering stays blocked.")
            else:
                err("error [rekey_mismatch]: some stored structures no longer give the same keys.")
        return 1
    return 0


def _print_recover_plan(plan: dict[str, Any]) -> None:
    out(f"Journal          {plan['journal']}")
    out(f"Already present  {plan['already_present']} journal entr(y/ies)")
    out(f"To re-create     {len(plan['to_register'])} ID(s): {', '.join(plan['to_register']) or '-'}")
    out(f"To re-retire     {len(plan['to_retire'])} ID(s): {', '.join(plan['to_retire']) or '-'}")


def cmd_recover(ctx: Context) -> int:
    args = ctx.args
    with ctx.service() as svc:
        plan = svc.recover_plan()
        nothing = not plan["to_register"] and not plan["to_retire"] and not plan["conflicts"]
        if not nothing and not args.yes:
            if not ctx.can_prompt:
                raise CliRefusal(
                    f"recover would re-create {len(plan['to_register'])} ID(s) and "
                    f"{len(plan['to_retire'])} retirement(s) from the issued-ID journal. "
                    "Pass --yes to confirm."
                )
            _print_recover_plan(plan)
            answer = _ask("Re-create these entries in the database exactly as journalled? [y/N] ")
            if answer.lower() not in ("y", "yes"):
                raise CliRefusal("Cancelled; nothing was changed.", code="cancelled")
        result = svc.recover()
    if ctx.json:
        emit_json(result)
        return 0
    out(f"Recovered {len(result['recovered'])} ID(s): {', '.join(result['recovered']) or '-'}")
    out(f"Re-retired {len(result['retired'])} ID(s): {', '.join(result['retired']) or '-'}")
    out(f"Already present: {result['already_present']}")
    if result["backup"]["performed"]:
        out(f"Backup  {result['backup']['path']}")
    _print_warnings(result["warnings"])
    return 0


def _native_host_status(browser: str = "firefox") -> dict[str, Any]:
    """``install_host.status()`` of ``browser``; a failure becomes a problem,
    never an error."""
    try:
        return install_host.status(browser=browser)
    except Exception as exc:  # noqa: BLE001 - doctor must report, not fail
        text = exc.message if isinstance(exc, RegistryError) else f"{type(exc).__name__}: {exc}"
        allowed = "allowed_extensions" if browser == "firefox" else "allowed_origins"
        return {"browser": browser, "registered": False, "check_failed": True,
                "manifest_path": None, "manifest_exists": False,
                "manifest_valid": False, "launcher": None, "launcher_exists": False,
                allowed: None, "home_override": None,
                "problems": [f"The {_BROWSER_NAMES[browser]} host registration could not be "
                             f"checked: {text}"]}


def _print_native_host(host: dict[str, Any], browser: str, doctor_home: str | None) -> None:
    """One browser's registration. ``doctor_home``: the registry of the
    running command (normalised), to say when the extension uses another."""
    name = _BROWSER_NAMES[browser]
    label = f"{name} host"
    if host.get("registered"):
        out(f"{label:<16}registered, manifest {host.get('manifest_path')}")
    else:
        out(f"{label:<16}NOT registered")
    if host.get("launcher"):
        out(f"Host program    {host['launcher']}"
            f"{'' if host.get('launcher_exists') else ' (missing)'}")
    # Always say which registry the extension uses.
    used = host.get("extension_home") or host.get("home_override")
    if host.get("home_override"):
        out(f"Extension home  {used} (host configuration)")
    elif used:
        out(f"Extension home  {used} (default registry as {name} sees it, if {name} has the "
            "same environment)")
    else:
        out("Extension home  unknown (see the warnings)")
    if (doctor_home is not None and used
            and os.path.normcase(os.path.abspath(used)) != doctor_home):
        out("                (the extension uses a DIFFERENT registry than this command)")
    for problem in host.get("problems") or []:
        out(f"  warning: {problem}")


def _print_native_hosts(hosts: dict[str, dict[str, Any]], doctor_home: str | None) -> None:
    """The registration of every browser in ``hosts`` that is registered (or
    could not be checked); one line when none is."""
    shown = [b for b, host in hosts.items() if host.get("registered") or host.get("check_failed")]
    for browser in shown:
        _print_native_host(hosts[browser], browser, doctor_home)
    if not any(hosts[b].get("registered") for b in hosts):
        names = " or ".join(_BROWSER_NAMES[b] for b in hosts)
        out(f"Browser host    NOT registered with {names} (run the installer, install.ps1 / "
            "install.sh,")
        out("                or 'compound-registry install-host --browser firefox|chrome|both')")


def cmd_host_status(ctx: Context) -> int:
    """How the host is registered with the browsers; opens no registry."""
    browsers = _BROWSER_CHOICES[getattr(ctx.args, "browser", None) or "both"]
    hosts = {browser: _native_host_status(browser) for browser in browsers}
    registered = [b for b in browsers if hosts[b].get("registered")]
    healthy = bool(registered) and not any(hosts[b].get("problems") for b in registered) \
        and not any(hosts[b].get("check_failed") for b in browsers)
    if ctx.json:
        emit_json(hosts)
        return 0 if healthy else 1
    for browser in browsers:
        _print_native_host(hosts[browser], browser, None)
    return 0 if healthy else 1


def cmd_init(ctx: Context) -> int:
    """Create the registry (config.toml, database) if it does not exist."""
    location = install_host.locate_registry(ctx.home)
    created = not location.exists
    warnings: list[str] = []
    wanted_format = getattr(ctx.args, "id_format", None)
    if created or wanted_format is not None:
        _config, warnings = create_registry(location.home, wanted_format)
        location = install_host.locate_registry(ctx.home)
        if not location.exists:
            raise install_host.registry_missing_error(location, cli=True)
    else:
        from .config import sync_folder_warning

        sync_warning = sync_folder_warning(Path(location.config.db_path))
        if sync_warning:
            warnings.append(sync_warning)
    with RegistryService(location.config) as svc:
        doctor = svc.doctor()
    stats = doctor.get("stats") or {}
    result = {
        "home": location.home,
        "config_path": str(location.config.config_path),
        "db_path": str(location.config.db_path),
        "created": created,
        "batches": stats.get("batches"),
        "retired": stats.get("retired"),
        "integrity": doctor.get("integrity"),
        "id_format": doctor.get("id_format"),
        "warnings": warnings,
    }
    if ctx.json:
        emit_json(result)
        return 0
    if created:
        banner = "=" * 64
        out(banner)
        out(f"  A NEW, EMPTY registry was created in {result['home']}")
        out(banner)
    else:
        out(f"A registry already exists in {result['home']}; nothing was changed.")
    out(f"Config          {result['config_path']}")
    out(f"Database        {result['db_path']}")
    out(f"ID format       {result['id_format']}")
    out(f"Contents        {result['batches']} batch(es) ({result['retired']} retired), "
        f"integrity {result['integrity']}")
    _print_warnings(warnings)
    return 0


def cmd_doctor(ctx: Context) -> int:
    with ctx.service() as svc:
        result = svc.doctor()
    # Informational only: a host problem never changes doctor's exit code.
    result["native_host"] = _native_host_status("firefox")
    result["native_host_chrome"] = _native_host_status("chrome")
    if ctx.json:
        emit_json(result)
    else:
        stats = result["stats"]
        out(f"Home            {result['home']}")
        out(f"Config          {result['config_path']}")
        out(f"Database        {result['db_path']}"
            f"{'' if result['db_exists'] else ' (missing)'}"
            f"{' (created now)' if result['db_created_now'] else ''}")
        out(f"ID format       {result['id_format']}")
        out(f"Default mode    {result['default_stereo_mode']}")
        out(f"Records         {stats.get('cores', '?')} cores, {stats.get('stereos', '?')} stereo "
            f"variants, {stats.get('batches', '?')} batches ({stats.get('retired', '?')} retired)")
        out(f"Integrity       {result['integrity']}")
        out(f"Backup folder   {result['backup_dir']}")
        out(f"Compound folders {result.get('files_folder') or '-'}")
        out(f"Backups         {result['backups']} (last: {result['last_backup_at'] or 'never'})")
        out(f"RDKit / InChI   {result['rdkit_version']} / {result['inchi_version']}")
        fp = result["chemistry_fingerprint"]
        out(f"Chemistry       stored {fp['stored'] or '(none yet)'}"
            f"{'  CHANGED' if fp['changed'] else ''}")
        out(f"                current {fp['current']}")
        journal = result.get("journal")
        if journal is not None:
            missing = journal["missing_in_database"]
            out(f"Journal         {journal['path']} ({journal['entries']} entries)")
            out(f"Journal mirror  {journal['mirror_path'] or '-'}")
            if missing:
                out(f"Missing in DB   {len(missing)} ID(s): {', '.join(missing[:10])}"
                    f"{' ...' if len(missing) > 10 else ''}")
        generator = result.get("name_generator") or {}
        out(f"Name generator  {generator.get('label') or 'none found'}")
        out(f"Python          {result['python']}")
        _print_native_hosts({"firefox": result["native_host"],
                             "chrome": result["native_host_chrome"]},
                            os.path.normcase(result["home"]))
        _print_warnings(result["warnings"])
    return 0 if result["integrity"] in ("ok", "unknown") else 1


# --------------------------------------------------------------------------- browser host

def _confirm_create_registry(ctx: Context):
    """The prompt for a missing registry (None when nobody can be asked)."""
    if not ctx.can_prompt:
        return None

    def confirm(path: str) -> bool:
        from .config import load_config, sync_folder_warning

        out()
        out(f"No registry exists in {path}.")
        try:
            sync_warning = sync_folder_warning(load_config(Path(path), create=False).db_path)
        except RegistryError:
            sync_warning = None
        if sync_warning:
            out(f"warning: {sync_warning}")
        out("Create a NEW, EMPTY registry there? Do this only for a FIRST installation. If your")
        out("registry was moved or is on a drive that is not connected, answer n and run")
        out("install-host again with --home pointing to it.")
        return _ask("Create a new, empty registry? [y/N] ").lower() in ("y", "yes")

    return confirm


_BROWSER_CHOICES = {"firefox": ("firefox",), "chrome": ("chrome",), "both": ("firefox", "chrome")}
_BROWSER_NAMES = {"firefox": "Firefox", "chrome": "Chrome"}


def _browsers_of(args: argparse.Namespace) -> tuple[str, ...]:
    return _BROWSER_CHOICES[getattr(args, "browser", None) or "firefox"]


def cmd_install_host(ctx: Context) -> int:
    args = ctx.args
    browsers = _browsers_of(args)
    result = install_host.install(
        ctx.home, default_home=bool(getattr(args, "default_home", False)),
        create_registry=bool(getattr(args, "create_registry", False)),
        force=bool(getattr(args, "force", False)),
        confirm_create=_confirm_create_registry(ctx), browsers=browsers,
        id_format=getattr(args, "id_format", None))
    if ctx.json:
        emit_json(result)
        return 0
    reg = result["registry"]
    if result["registry_created"]:
        banner = "=" * 64
        out(banner)
        out(f"  A NEW, EMPTY registry was created in {reg['home']}")
        out(banner)
    names = " and ".join(_BROWSER_NAMES[b] for b in browsers)
    out(f"The registry host is now registered with {names} for the current user.")
    for message in result["messages"]:
        out(f"  - {message}")
    for browser in browsers:
        entry = result["registrations"][browser]
        label = "Manifest" if browser == "firefox" else "Chrome manifest"
        out(f"{label:<16}{entry['manifest_path']}")
    out(f"Host program    {result['launcher']}")
    for browser in browsers:
        entry = result["registrations"][browser]
        if entry["registry_key"]:
            out(f"Registry key    {entry['registry_key']}")
    if browsers != ("firefox",):
        out(f"Host config     {result['sidecar_path']} (shared by Firefox and Chrome)")
    out(f"Extension uses  {reg['home']}")
    out(f"                ({result['extension_home_note']})")
    out(f"Database        {reg['db_path']}")
    out(f"Contents        {reg['batches']} batch(es) ({reg['retired']} retired), "
        f"integrity {reg['integrity']}")
    _print_warnings(result["warnings"])
    out()
    out("Next steps:")
    from . import setup_wizard

    for line in setup_wizard.next_steps(browsers, root=setup_wizard.find_project_root(),
                                        build_commands=True):
        out(line)
    return 0


def cmd_setup(ctx: Context) -> int:
    """The guided installation (what install.ps1 / install.sh start)."""
    from . import setup_wizard

    args = ctx.args
    if ctx.json:
        raise RegistryError("'setup' has no --json output.", code="invalid_argument")
    options = setup_wizard.Options(
        home=getattr(args, "home", None), browser=args.browser, eln_origin=args.eln_origin,
        id_prefix=args.id_prefix, id_format=args.id_format,
        create_registry=args.create_registry, mnova=args.mnova,
        skip_extension=args.skip_extension, force=args.force, yes=args.yes,
        upgrade=args.upgrade, dry_run=args.dry_run)
    env = setup_wizard.real_environment(lambda prompt: input(_ascii(prompt)), out,
                                        _stdin_is_terminal())
    return setup_wizard.run_setup(options, env)


def cmd_uninstall_host(ctx: Context) -> int:
    browsers = _browsers_of(ctx.args)
    result = install_host.uninstall(force=bool(getattr(ctx.args, "force", False)),
                                    browsers=browsers)
    if ctx.json:
        emit_json(result)
        return 0
    if result["removed"]:
        names = " and ".join(_BROWSER_NAMES[b] for b in browsers)
        out(f"The registry host is no longer registered with {names}. Removed:")
        for item in result["removed"]:
            out(f"  - {item}")
    for message in result["messages"]:
        out(message)
    out("The registry itself (database, journal, backups) was not touched.")
    return 0


# --------------------------------------------------------------------------- parser

def _common_options(json_default: Any = False, home_default: Any = None) -> argparse.ArgumentParser:
    """``--home`` and ``--json``, accepted before or after the subcommand."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--home", metavar="PATH", default=home_default,
                   help="registry home folder (default: $COMPOUND_REGISTRY_HOME, "
                        "else ~/CompoundRegistry)")
    p.add_argument("--json", action="store_true", default=json_default,
                   help="machine output: exactly one JSON document on stdout")
    return p


def _structure_source(group: argparse._MutuallyExclusiveGroup) -> None:
    group.add_argument("--molfile", metavar="FILE", help="read the structure from a molfile")
    group.add_argument("--smiles", metavar="SMILES", help="the structure as (CX)SMILES")
    group.add_argument("--stdin", action="store_true",
                       help="read a molfile or a SMILES from standard input")


def _mode_option(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=MODE_OVERRIDES,
                   help="stereo mode: abs (as drawn), rac (racemate), rel (relative), "
                        "undefined; default: as detected from the file")


def add_register_parser(sub, common) -> None:
    p = sub.add_parser("register", parents=[common], help="issue (or return) the ID of a product",
                       description="Issue the batch ID of a compound in an experiment, or return "
                                   "the existing one.")
    _structure_source(p.add_mutually_exclusive_group(required=True))
    p.add_argument("--experiment-ref", required=True, metavar="REF",
                   help="stable experiment identifier (Sciformation internal key)")
    p.add_argument("--experiment-code", metavar="CODE", help="human code, e.g. ABC-008")
    p.add_argument("--experiment-url", metavar="URL", help="link to the experiment")
    p.add_argument("--product-index", type=int, default=1, metavar="N",
                   help="1-based position of the product in the reaction (default 1)")
    _mode_option(p)
    p.add_argument("--yes", action="store_true",
                   help="accept the detected stereo mode of a new stereo variant without asking")
    p.add_argument("--accept-ignored-stereo", action="store_true",
                   help="register even though some drawn stereo is not part of the registry "
                        "keys (stereoisomers differing only there would share an ID)")
    p.set_defaults(handler=cmd_register)


def add_lookup_parser(sub, common) -> None:
    p = sub.add_parser("lookup", parents=[common], help="show what is registered (read-only)",
                       description="Look up a structure, an ID or an experiment. With a "
                                   "structure, --experiment-ref gives the experiment context.")
    g = p.add_mutually_exclusive_group()
    _structure_source(g)
    g.add_argument("--id", metavar="FULL_ID", help="show one batch")
    p.add_argument("--experiment-ref", metavar="REF",
                   help="list the batches of an experiment (alone) or check a structure "
                        "against it")
    _mode_option(p)
    p.set_defaults(handler=cmd_lookup, _validate=_validate_lookup, _parser=p)


def _validate_lookup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    has_structure = args.molfile is not None or args.smiles is not None or args.stdin
    if not has_structure and args.id is None and args.experiment_ref is None:
        parser.error("lookup needs one of --molfile, --smiles, --stdin, --id or --experiment-ref")
    if args.id is not None and args.experiment_ref is not None:
        parser.error("--id cannot be combined with --experiment-ref")
    if args.mode is not None and not has_structure:
        parser.error("--mode only applies to a structure lookup")


def add_experiments_parser(sub, common) -> None:
    p = sub.add_parser("experiments", parents=[common],
                       help="list every experiment in which a compound was made",
                       description="List every experiment in which the compound named by ID was "
                                   "made. ID is a full ID (000042_02_003: the compound that batch "
                                   "belongs to), a CORE_STEREO (000042_02) or a CORE number "
                                   "(000042 or 42: all stereo variants). Read-only.")
    p.add_argument("id", metavar="ID")
    p.set_defaults(handler=cmd_experiments)


def add_compounds_parser(sub, common) -> None:
    p = sub.add_parser("compounds", parents=[common],
                       help="list the registered compounds (one entry per CORE), optionally "
                            "filtered",
                       description="List every registered CORE with its stereo variants. "
                                   "--text matches an ID, InChIKey, SMILES, InChI, part of a "
                                   "stored name, a formula or an ELN number; a structure "
                                   "(--smiles / --molfile) is matched as a substructure "
                                   "(default) or exactly (same constitution). With both, a CORE "
                                   "must match both. Read-only.")
    p.add_argument("--text", metavar="TEXT", help="one line of search text")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--smiles", metavar="SMILES", help="query structure as SMILES")
    g.add_argument("--molfile", metavar="FILE", help="read the query structure from a molfile")
    p.add_argument("--match", choices=MATCH_MODES, default="substructure",
                   help="how the query structure is compared (default: substructure)")
    p.set_defaults(handler=cmd_compounds)


def add_name_parser(sub, common) -> None:
    p = sub.add_parser("name", parents=[common],
                       help="show, store, generate or remove the name of a stereo variant",
                       description="Show the name stored for CORE_STEREO (e.g. 000042_01), "
                                   "store NAME (one line, e.g. the IUPAC name; quote it), "
                                   "let the ChemDraw installed on this PC generate it "
                                   "(--generate) or remove it with --remove. A name is an "
                                   "annotation: it is never part of an ID, and every change is "
                                   "recorded in the registry's event log.")
    p.add_argument("core_stereo", metavar="CORE_STEREO")
    g = p.add_mutually_exclusive_group()
    g.add_argument("name", nargs="?", metavar="NAME", help="the name to store")
    g.add_argument("--remove", action="store_true", help="remove the stored name")
    g.add_argument("--generate", action="store_true",
                   help="ask the local ChemDraw for the name and store it (kept: an already "
                        "stored name, unless --replace)")
    p.add_argument("--replace", action="store_true",
                   help="with --generate: replace an already stored name")
    p.set_defaults(handler=cmd_name, _validate=_validate_name, _parser=p)


def _validate_name(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.replace and not args.generate:
        parser.error("--replace only applies to --generate")


def add_spectra_parser(sub, common) -> None:
    p = sub.add_parser("spectra", parents=[common],
                       help="show the NMR data recorded from MestReNova",
                       description="Show the spectra recorded by the MestReNova add-on "
                                   "(multiplet report, saved files) for one batch ID, for "
                                   "every batch of a CORE_STEREO, or for the whole registry.")
    p.add_argument("id", nargs="?", metavar="ID", help="a full ID or a CORE_STEREO")
    p.set_defaults(handler=cmd_spectra)


def add_generate_names_parser(sub, common) -> None:
    p = sub.add_parser("generate-names", parents=[common],
                       help="let the local ChemDraw name every stereo variant without a name",
                       description="Ask the ChemDraw installed on this PC for the name of every "
                                   "stereo variant that has no stored name, one after another, "
                                   "and store each name that can be trusted. Prints one line per "
                                   "variant: the CORE_STEREO, then the name or why there is "
                                   "none. Stored names are never replaced.")
    p.add_argument("--limit", type=_positive_int, metavar="N",
                   help="try at most N variants")
    p.set_defaults(handler=cmd_generate_names)


def add_yields_parser(sub, common) -> None:
    p = sub.add_parser("yields", parents=[common],
                       help="list the yields of the products of every experiment",
                       description="List every batch (one product of one experiment, retired "
                                   "ones included) by ELN number and product number, with its "
                                   "isolated and analytical yield in percent as read from the "
                                   "ELN ('-' when none is recorded) and the name of its stereo "
                                   "variant. Read-only.")
    p.add_argument("--text", metavar="TEXT",
                   help="keep rows whose ELN number, full ID or name contains TEXT (ignoring "
                        "case), or whose internal experiment key is TEXT")
    p.add_argument("--only-recorded", action="store_true",
                   help="list only batches that have a yield")
    p.set_defaults(handler=cmd_yields)


def add_reaction_parser(sub, common) -> None:
    p = sub.add_parser("reaction", parents=[common],
                       help="show the reaction stored for an experiment",
                       description="Show what the extension stored about the reaction of an "
                                   "experiment (reactants, reagents, solvents, temperature, "
                                   "reference amount), as the ELN page showed it. Read-only.")
    p.add_argument("experiment", metavar="EXP",
                   help="the internal key of the experiment or its ELN number (e.g. ABC-002)")
    p.set_defaults(handler=cmd_reaction)


def add_route_parser(sub, common) -> None:
    p = sub.add_parser("route", parents=[common],
                       help="draw the synthesis route of a compound as a ChemDraw file",
                       description="Draw every stored reaction that led to a stereo variant as "
                                   "a ChemDraw document (CDXML) and save it as a new file in the "
                                   "compound's folder (an existing file is never overwritten), "
                                   "then open it with Windows. Prints the file, the experiments "
                                   "drawn and the experiments whose reaction is not stored yet.")
    p.add_argument("core_stereo", metavar="CORE_STEREO",
                   help="the stereo variant (e.g. 000042_01)")
    p.add_argument("--no-open", action="store_true", help="write the file but do not open it")
    p.set_defaults(handler=cmd_route)


def _limiting_row(text: str) -> int | None:
    """``auto`` -> None, else a reactant row number from 1."""
    value = text.strip()
    if value.casefold() == "auto":
        return None
    try:
        row = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a reactant row number or 'auto', got {text!r}") from None
    if not 1 <= row <= MAX_PRODUCT_INDEX:
        raise argparse.ArgumentTypeError(
            f"the row must be from 1 to {MAX_PRODUCT_INDEX}, got {row}")
    return row


def add_limiting_parser(sub, common) -> None:
    p = sub.add_parser("limiting", parents=[common],
                       help="choose the starting material (SM) of an experiment in routes",
                       description="Choose which reactant of an experiment is its starting "
                                   "material: its mass is the 'SM' on the experiment's line in "
                                   "the synthesis route ('route'). ROW is the reactant's row in "
                                   "the stored reaction (see 'reaction EXP'); 'auto' goes back "
                                   "to the automatic choice (the reactant with 1 equivalent "
                                   "whose amount is the reference amount). The choice is kept "
                                   "when the reaction is stored again, and ignored (with a "
                                   "note in the route) once that row holds another reactant.")
    p.add_argument("experiment", metavar="EXP",
                   help="the internal key of the experiment or its ELN number (e.g. ABC-002)")
    p.add_argument("row", metavar="ROW|auto", type=_limiting_row,
                   help="the reactant row (1, 2, ...) or 'auto'")
    p.set_defaults(handler=cmd_limiting)


def add_folders_parser(sub, common) -> None:
    p = sub.add_parser("folders", parents=[common],
                       help="make the missing compound folders (one per stereo variant)",
                       description="Make the folder of every registered stereo variant that has "
                                   "none yet, in the folder set by [files] folder in "
                                   "config.toml, each with the structure as CORE_STEREO.mol, "
                                   "and print one line per variant. With CORE_STEREO only that "
                                   "variant's folder is made, and its path is printed. Nothing "
                                   "that exists is changed or overwritten.")
    p.add_argument("core_stereo", nargs="?", metavar="CORE_STEREO",
                   help="only this stereo variant (e.g. 000042_01)")
    p.set_defaults(handler=cmd_folders)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def add_export_parser(sub, common) -> None:
    p = sub.add_parser("export", parents=[common], help="export all batches to CSV or SDF")
    p.add_argument("--format", required=True, choices=EXPORT_FORMATS)
    p.add_argument("--output", required=True, metavar="FILE")
    p.add_argument("--active-only", action="store_true", help="leave out retired batches")
    p.set_defaults(handler=cmd_export)


def add_backup_parser(sub, common) -> None:
    p = sub.add_parser("backup", parents=[common], help="back up now, list or verify backups")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list the existing backups")
    g.add_argument("--verify", metavar="FILE", help="check that FILE is an intact backup")
    p.set_defaults(handler=cmd_backup)


def add_retire_parser(sub, common) -> None:
    p = sub.add_parser("retire", parents=[common], help="retire a wrongly issued batch ID")
    p.add_argument("full_id", metavar="FULL_ID")
    p.add_argument("--reason", required=True, metavar="TEXT")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(handler=cmd_retire)


def add_rekey_check_parser(sub, common) -> None:
    p = sub.add_parser("rekey-check", parents=[common],
                       help="check that every stored structure still gives the same keys")
    p.add_argument("--accept", action="store_true",
                   help="if there is no mismatch, record the current chemistry version "
                        "(lifts the block after an RDKit/InChI update)")
    p.set_defaults(handler=cmd_rekey_check)


def add_recover_parser(sub, common) -> None:
    p = sub.add_parser("recover", parents=[common],
                       help="re-create IDs the database lost (e.g. after restoring a backup) "
                            "from the issued-ID journal",
                       description="Re-create, exactly as journalled, the IDs and retirements "
                                   "that the issued-ID journal holds but the database lacks.")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(handler=cmd_recover)


def add_init_parser(sub, common) -> None:
    p = sub.add_parser("init", parents=[common],
                       help="create the registry (first installation only)",
                       description="Create the registry folder, its config.toml and an empty "
                                   "database, in --home or the default folder. Does nothing if "
                                   "the registry already exists. Besides the installer "
                                   "('setup', 'install-host --create-registry') this is the "
                                   "only command that creates a registry.")
    p.add_argument("--id-format", metavar="TEMPLATE",
                   help="ID template of the NEW registry, e.g. "
                        "'EX{core:04d}_{stereo:02d}_{batch:03d}' for EX0042_02_003 "
                        "(default: {core:06d}_{stereo:02d}_{batch:03d}; the text in front is "
                        "free: letters, '_', '-', '.'). It cannot be changed once the registry "
                        "exists.")
    p.set_defaults(handler=cmd_init)


def add_doctor_parser(sub, common) -> None:
    p = sub.add_parser("doctor", parents=[common], help="show configuration and health")
    p.set_defaults(handler=cmd_doctor)


def add_install_host_parser(sub, common) -> None:
    # Own parent instead of ``common``: here --home means "the registry the
    # EXTENSION should use", and the help text has to say so.
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--home", metavar="PATH", default=argparse.SUPPRESS,
                        help="pin the registry folder the browser extension should use (written "
                             "to the host configuration next to the manifest)")
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine output: exactly one JSON document on stdout")
    p = sub.add_parser("install-host", parents=[parent],
                       help="register the native messaging host with the browser (current user)",
                       description="Register this installation's compound-registry-host with "
                                   "the browser (--browser firefox, chrome or both) for the "
                                   "current user (no administrator rights). This is the "
                                   "low-level command; 'setup' asks for everything and also "
                                   "builds the extension. "
                                   "Which registry the extension uses: --home PATH (here or "
                                   "before the command) pins PATH; --default-home switches to "
                                   "the default registry ($COMPOUND_REGISTRY_HOME as the browser "
                                   "sees it, else ~/CompoundRegistry); with neither, the "
                                   "registry chosen at a previous installation is kept (the "
                                   "default registry if none was chosen). That registry must "
                                   "exist; it is created only with --create-registry or after "
                                   "a confirmation.")
    p.add_argument("--default-home", action="store_true",
                   help="use the default registry and remove a pinned one")
    p.add_argument("--create-registry", action="store_true",
                   help="create a NEW, EMPTY registry if none exists at the chosen place "
                        "(first installation only)")
    p.add_argument("--id-format", metavar="TEMPLATE",
                   help="ID template of the registry that is created here (see 'init "
                        "--id-format'); refused when the registry already exists")
    p.add_argument("--force", action="store_true",
                   help="replace a registration that belongs to another installation")
    p.add_argument("--browser", choices=sorted(_BROWSER_CHOICES), default="firefox",
                   help="which browser to register the host with (default: firefox). Both "
                        "browsers share one host configuration, so they always use the same "
                        "registry; 'chrome' alone never changes the registry already "
                        "configured (use 'both' with --home to switch)")
    p.set_defaults(handler=cmd_install_host, _validate=_validate_install_host, _parser=p)


def _validate_install_host(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if getattr(args, "home", None) and args.default_home:
        parser.error("--home and --default-home cannot be combined")


def add_host_status_parser(sub, common) -> None:
    p = sub.add_parser("host-status", parents=[common],
                       help="show how the native messaging host is registered with the browsers",
                       description="Show, for Firefox and Chrome, whether this installation's "
                                   "host is registered, which registry the extension uses and "
                                   "what is wrong. Opens no registry, so it also works when the "
                                   "registry is missing. Exit code 1 when no browser is "
                                   "registered or a registered one has a problem.")
    p.add_argument("--browser", choices=sorted(_BROWSER_CHOICES), default="both",
                   help="which browser to look at (default: both)")
    p.set_defaults(handler=cmd_host_status)


def add_setup_parser(sub, common) -> None:
    # Own --home (not ``common``): here it is the data folder to install for.
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--home", metavar="PATH", default=argparse.SUPPRESS,
                        help="the data folder: where the registry database, its backups and "
                             "the compound files are stored (not asked then)")
    p = sub.add_parser("setup", parents=[parent],
                       help="install or update everything for the current user, with questions",
                       description="The guided installation that install.ps1 / install.sh start: "
                                   "asks for the data folder, the browser(s), the ELN address "
                                   "and the MestReNova add-on (Enter accepts the default shown "
                                   "in brackets), then registers the registry program with the "
                                   "browser, builds the extension and says what is left to do "
                                   "by hand. Every option answers its question in advance.")
    p.add_argument("--browser", choices=sorted(_BROWSER_CHOICES),
                   help="the browser(s) to set up")
    p.add_argument("--eln-origin", metavar="URL",
                   help="address of the Sciformation ELN the extension is built for, e.g. "
                        "https://eln.example.org (https, host and optional port only)")
    p.add_argument("--create-registry", action="store_true",
                   help="create a NEW, EMPTY registry in the data folder without asking, if "
                        "none exists there (first installation only)")
    p.add_argument("--id-prefix", metavar="TEXT",
                   help="letters put in front of every ID of a NEW registry, e.g. ABC for "
                        "ABC000001_01_001 (permanent)")
    p.add_argument("--id-format", metavar="TEMPLATE",
                   help="full ID template of a NEW registry instead of --id-prefix (see 'init "
                        "--id-format'; permanent)")
    p.add_argument("--mnova", choices=("yes", "no"),
                   help="install / refresh the MestReNova add-on, or leave it alone")
    p.add_argument("--skip-extension", action="store_true",
                   help="do not build the extension packages")
    p.add_argument("--force", action="store_true",
                   help="replace a browser registration that belongs to another installation")
    p.add_argument("--yes", action="store_true",
                   help="ask nothing: take every default or previous choice. Never creates a "
                        "registry (--create-registry) and never switches to another registry "
                        "(--home) unless these options say so")
    p.add_argument("--upgrade", action="store_true",
                   help="ask nothing and keep everything as it is installed (registry, "
                        "browsers, ELN address, add-on): refresh the registration, rebuild the "
                        "extension, refresh the add-on if it is installed")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be done and every file that would be written; "
                        "change nothing")
    p.set_defaults(handler=cmd_setup)


def add_uninstall_host_parser(sub, common) -> None:
    p = sub.add_parser("uninstall-host", parents=[common],
                       help="remove the browser registration of the native messaging host",
                       description="Remove the host manifest, its host configuration and (on "
                                   "Windows) the registry key, if they belong to this "
                                   "installation. The registry itself is not touched.")
    p.add_argument("--force", action="store_true",
                   help="also remove a registration that belongs to another installation")
    p.add_argument("--browser", choices=sorted(_BROWSER_CHOICES), default="firefox",
                   help="which browser's registration to remove (default: firefox); the "
                        "shared host configuration stays while the other browser uses it")
    p.set_defaults(handler=cmd_uninstall_host)


#: Subcommand parser builders, in help order. Add new commands here.
COMMANDS: list[Callable[[Any, argparse.ArgumentParser], None]] = [
    add_setup_parser,
    add_init_parser,
    add_register_parser,
    add_lookup_parser,
    add_experiments_parser,
    add_compounds_parser,
    add_name_parser,
    add_generate_names_parser,
    add_yields_parser,
    add_reaction_parser,
    add_route_parser,
    add_limiting_parser,
    add_spectra_parser,
    add_folders_parser,
    add_export_parser,
    add_backup_parser,
    add_retire_parser,
    add_rekey_check_parser,
    add_recover_parser,
    add_doctor_parser,
    add_install_host_parser,
    add_uninstall_host_parser,
    add_host_status_parser,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        parents=[_common_options()],
        description="Local permanent compound ID registry (CORE_STEREO_BATCH).",
    )
    # In subcommands the defaults are SUPPRESS so an option given before the
    # subcommand is not overwritten by the subparser's default.
    common = _common_options(json_default=argparse.SUPPRESS, home_default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    for add in COMMANDS:
        add(sub, common)
    return parser


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate = getattr(args, "_validate", None)
        if validate is not None:
            validate(args._parser, args)
    except SystemExit as exc:  # argparse: --help (0) or usage error (2)
        return exc.code if isinstance(exc.code, int) else 2

    as_json = bool(getattr(args, "json", False))
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    ctx = Context(args=args, json=as_json, home=home)
    try:
        return args.handler(ctx)
    except RegistryError as exc:
        if as_json:
            emit_json({"error": exc.to_dict()})
        else:
            err(f"error [{exc.code}]: {exc.message}")
        return 1
    except KeyboardInterrupt:
        if as_json:
            emit_json({"error": {"code": "cancelled", "message": "Interrupted."}})
        else:
            err("error [cancelled]: interrupted; nothing more was done.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
