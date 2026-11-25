"""Native-messaging host: the browser (Firefox or Chrome) starts this program and
talks to it in frames.

The protocol (framing, requests, responses, error codes) is specified in
:mod:`compound_registry.protocol`; this module does the I/O around it:

* :func:`main` protects stdout (only frames may ever reach it), checks the
  caller, decides the registry home once (:func:`resolve_home`), sets up the
  log ``<home>/logs/host.log`` and runs :func:`serve`. Chrome starts the same
  program for the Chrome build of the extension: its first argument is then
  the caller's origin, which must be ``protocol.CHROME_ORIGIN``, and the
  registry is the one of the host configuration Chrome's registration shares
  with Firefox (see :func:`_resolve_chrome_home`).
* :func:`serve` reads frames from a binary stream and writes one response
  frame per request, strictly one after another. An answer over the size
  limit first loses its thumbnails (``analyze``: the other variants' first,
  then its own; ``find``: the variants'; ``compounds``: the items'; ``route``:
  the document text ``cdxml``, the file is written all the same), with a
  warning in ``result.warnings``, before it becomes ``response_too_large``. It takes its streams and its
  home as parameters so tests can run it on in-memory streams.
* Every request opens a FRESH :class:`~compound_registry.service.RegistryService`
  and closes it afterwards: the service caches its journal and chemistry
  checks per instance, and the CLI (``recover``, ``retire``, a restore) may
  change the registry while the browser keeps the host alive.
* The operations of ``protocol.CAPTURE_OPS`` ("Capture reaction") are the
  exception: they need no registry, so none is opened for them and they are
  answered even when the registry is missing or the host configuration is
  invalid (:mod:`compound_registry.recognition`). The recognition worker they
  start is stopped when the host ends.
* The host NEVER creates a registry (or its home folder, not even for the
  log): a missing ``config.toml`` or database is answered with
  ``registry_missing``. Before every request the home is decided again; if
  it changed since start-up the request is answered with
  ``registry_changed`` and the host exits.

The log never contains structures (molfile, SMILES, InChI, InChIKey), names,
search texts or other request parameters; per request it records the operation, the request id,
the duration, ``ok`` or the error code, and for ``register`` / ``retire`` the
full ID.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import struct
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import __version__, install_host, protocol
from .errors import ConfigError, RegistryError
from .install_host import (
    locate_registry,
    read_host_config,
    registry_missing_error,
)
from .models import MODE_OVERRIDES

__all__ = ["HomeChoice", "resolve_home", "serve", "handle_message", "encode_response", "main",
           "LOG_FILENAME"]

CHROME_ORIGIN_PREFIX = "chrome-extension://"
#: Chrome on Windows adds ``--parent-window=<handle>`` after the origin.
CHROME_PARENT_WINDOW = "--parent-window="

LOGGER_NAME = "compound_registry.host"
LOG_FILENAME = "host.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

_HEADER = struct.Struct("=I")  # native byte order, unsigned 32-bit

THUMBNAILS_OTHERS_DROPPED = ("The thumbnails of the other stereo variants were left out because "
                             "the answer was too large to send to the browser.")
THUMBNAIL_MAIN_DROPPED = ("The thumbnail of this structure was left out because the answer was "
                          "too large to send to the browser.")
THUMBNAILS_DROPPED = ("The structure drawings were left out because the answer was too large "
                      "to send to the browser.")
ROUTE_DOCUMENT_DROPPED = ("The ChemDraw document was left out of the answer because it was too "
                          "large to send to the browser; open the file from the compound's "
                          "folder.")

# Ops whose answer can lose thumbnails before it counts as too large: the list
# in the result that holds them.
_THUMBNAIL_LISTS = {"compounds": "items", "find": "variants"}

OpenService = Callable[[Path | None], Any]


# --------------------------------------------------------------------------- home

@dataclass(frozen=True)
class HomeChoice:
    """The registry home, decided once at start-up.

    ``home`` None means "``$COMPOUND_REGISTRY_HOME`` or the default", resolved
    by :mod:`compound_registry.config`. ``error`` set means the host
    configuration is unusable: every request is answered with it and NO other
    registry is used (that could issue IDs in the wrong registry).
    """

    home: Path | None
    source: str                       # one of protocol.HOME_SOURCES
    error: RegistryError | None = None
    log_dir: Path | None = None

    def identity(self) -> tuple[str | None, str, str | None]:
        """What must stay the same for the host to keep serving."""
        return (str(self.home) if self.home is not None else None, self.source,
                self.error.message if self.error is not None else None)

    def describe(self) -> str:
        if self.error is not None:
            return "an invalid host configuration"
        if self.home is not None:
            return f"{self.home} (host configuration)"
        from .config import default_home

        try:
            where = str(default_home())
        except RegistryError:
            where = "an invalid $COMPOUND_REGISTRY_HOME"
        return f"{where} ({'COMPOUND_REGISTRY_HOME' if self.source == 'env' else 'default registry'})"


def _sidecar_choice(sidecar: Path) -> HomeChoice:
    """The choice an EXISTING host configuration file makes (never falls back)."""
    home, problem = read_host_config(sidecar)
    if problem is not None:
        return HomeChoice(None, "host-config", error=ConfigError(
            f"The host configuration {sidecar} is invalid: {problem}. The "
            "extension does not fall back to another registry, because IDs could "
            "then be issued in the wrong one. Fix the file, or run "
            "'compound-registry install-host' (with --home PATH) again."))
    return HomeChoice(Path(home), "host-config", log_dir=Path(home) / "logs")


def _resolve_chrome_home(environ: Any, target: Any) -> HomeChoice:
    """The registry for a host started by Chrome: the host configuration of
    Chrome's registration - the one the installer shares with Firefox (see
    :func:`compound_registry.install_host.host_config_path`).

    * Registration found, host configuration present, and Firefox's
      registration (if any) resolves to the same registry: that registry.
    * Everything else is ``config_invalid`` for every request, because
      guessing could issue IDs in the wrong registry:
      registration NOT found (Windows: no ``HKCU`` Chrome key or its manifest
      is missing; macOS / Linux: no Chrome manifest); no host configuration
      (unpinned, each browser would use ``$COMPOUND_REGISTRY_HOME`` as its own
      process sees it, which this process cannot compare - the installer
      always pins the registry when Chrome is registered); Firefox's
      registration unreadable or using a manifest under another name (see
      :func:`~compound_registry.install_host.chrome_host_config_paths`); or
      Firefox's host configuration naming another registry.
    """
    try:
        target = target if target is not None else install_host.default_target()
        chrome_sidecar, firefox_sidecar = install_host.chrome_host_config_paths(target)
    except RegistryError as exc:
        return HomeChoice(None, "host-config", error=exc if isinstance(exc, ConfigError)
                          else ConfigError(exc.message))
    except Exception as exc:  # noqa: BLE001 - never guess a registry
        return HomeChoice(None, "host-config", error=ConfigError(
            f"Chrome's registration of the registry host could not be read "
            f"({type(exc).__name__}). Run 'compound-registry install-host --browser chrome' "
            "again."))

    def choice_for(sidecar: Path) -> HomeChoice:
        return _sidecar_choice(sidecar) if sidecar.exists() else _env_or_default(environ)

    if not chrome_sidecar.exists():
        # Unpinned, each browser would use $COMPOUND_REGISTRY_HOME as ITS process
        # sees it; this process cannot see Firefox's environment to compare.
        return HomeChoice(None, "host-config", error=ConfigError(
            f"Chrome's registration of the registry host has no host configuration "
            f"({chrome_sidecar}), so the registry is not pinned: Chrome and Firefox could each "
            "use $COMPOUND_REGISTRY_HOME as their own environment has it, i.e. DIFFERENT "
            "registries. The extension does not pick one, because IDs could then be issued in "
            "the wrong registry. Run 'compound-registry install-host --browser chrome' "
            "(install.ps1 -Browser Chrome) again: it pins the registry for both browsers."))
    choice = choice_for(chrome_sidecar)
    if firefox_sidecar is not None and not _same_file(firefox_sidecar, chrome_sidecar):
        firefox = choice_for(firefox_sidecar)
        if firefox.identity() != choice.identity():
            return HomeChoice(None, "host-config", error=ConfigError(
                f"Chrome's host configuration ({chrome_sidecar}: {choice.describe()}) and "
                f"Firefox's ({firefox_sidecar}: {firefox.describe()}) name DIFFERENT "
                "registries. The extension does not pick one, because IDs could then be issued "
                "in the wrong registry. Run 'compound-registry install-host --browser both' "
                "(with --home PATH) so that both browsers use one registry."))
    return choice


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _env_or_default(environ: Any) -> HomeChoice:
    from .config import HOME_ENV_VAR, default_home

    source = "env" if environ.get(HOME_ENV_VAR, "").strip() else "default"
    try:
        log_dir: Path | None = default_home() / "logs"
    except RegistryError:
        log_dir = None  # a relative $COMPOUND_REGISTRY_HOME: every request reports it
    return HomeChoice(None, source, log_dir=log_dir)


def is_chrome_caller(argv: list[str]) -> bool:
    """Chrome passes the caller's origin ``chrome-extension://<id>/`` first."""
    return bool(argv) and argv[0][:len(CHROME_ORIGIN_PREFIX)].lower() == CHROME_ORIGIN_PREFIX


def resolve_home(argv: list[str], environ: dict[str, str] | None = None, *,
                 target: Any = None) -> HomeChoice:
    """Where the registry is: host configuration next to the manifest Firefox
    passed as first argument, else ``$COMPOUND_REGISTRY_HOME``, else default.

    Started by Chrome (first argument a ``chrome-extension://`` origin): the
    host configuration of Chrome's registration, see :func:`_resolve_chrome_home`.
    ``target`` (an :class:`~compound_registry.install_host.HostTarget`; default:
    this machine's) is only used for Chrome."""
    environ = os.environ if environ is None else environ
    if is_chrome_caller(argv):
        return _resolve_chrome_home(environ, target)
    if argv:
        manifest = Path(argv[0])
        sidecar = install_host.firefox_host_config_path(manifest)
        if sidecar is not None and manifest.is_file() and sidecar.exists():
            return _sidecar_choice(sidecar)
    return _env_or_default(environ)


# --------------------------------------------------------------------------- framing

def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Up to ``size`` bytes; fewer only at end of stream."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame(stream: BinaryIO, data: bytes) -> None:
    stream.write(_HEADER.pack(len(data)) + data)
    stream.flush()


def _dumps(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _error(request_id: str | int | None, code: str, message: str) -> dict[str, Any]:
    return protocol.error_response(request_id, {"code": code, "message": message})


class _BadJson(ValueError):
    pass


def _reject_constant(name: str) -> Any:
    raise _BadJson(f"the constant {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _BadJson("a key appears twice in one object")
        out[key] = value
    return out


def _decode(body: bytes) -> tuple[Any, str | None]:
    """(message, None) or (None, why it is not a usable JSON message)."""
    if not body:
        return None, "The message is empty."
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, "The message is not valid UTF-8."
    try:
        message = json.loads(text, parse_constant=_reject_constant,
                             object_pairs_hook=_no_duplicate_keys)
    except RecursionError:
        return None, "The message is nested too deeply."
    except _BadJson as exc:
        return None, f"The message is not valid JSON: {exc}."
    except json.JSONDecodeError as exc:
        return None, f"The message is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
    except ValueError:
        return None, "The message is not valid JSON."
    try:
        # Lone surrogates ("\ud800") decode fine but cannot be stored or sent back.
        json.dumps(message, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None, "The message contains invalid Unicode text (an unpaired surrogate)."
    except (RecursionError, ValueError):
        return None, "The message is nested too deeply."
    return message, None


# --------------------------------------------------------------------------- operations

def _default_open_service(home: Path | None) -> Any:
    """Open the EXISTING registry; never create one (``registry_missing``)."""
    from .service import RegistryService  # RDKit: imported only once stdout is safe

    location = locate_registry(home)       # load_config(create=False); creates nothing
    if not location.exists:
        raise registry_missing_error(location)
    return RegistryService(location.config)


def _structure_args(params: dict[str, Any]) -> dict[str, Any]:
    return {"molfile": params["molfile"], "smiles": params["smiles"], "mode": params["mode"],
            "experiment_ref": params["experiment_ref"], "product_index": params["product_index"]}


def _op_ping(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    doctor = svc.doctor()
    result = {key: doctor.get(key) for key in protocol.PING_RESULT_KEYS}
    result.update(protocol_version=protocol.PROTOCOL_VERSION, host_version=__version__,
                  home_source=choice.source, mode_options=list(MODE_OVERRIDES))
    return result


def _op_analyze(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.analyze(**_structure_args(params), with_svg=True, with_molfile=False)


def _mismatch_message(confirmed: dict[str, str], computed: dict[str, Any]) -> str:
    parts = []
    if computed["inchikey"] != confirmed["inchikey"]:
        parts.append(f"the structure (you confirmed InChIKey {confirmed['inchikey']}, the host "
                     f"now computes {computed['inchikey']})")
    if computed["stereo_mode"] != confirmed["stereo_mode"]:
        parts.append(f"the stereo mode (you confirmed '{confirmed['stereo_mode']}', the host now "
                     f"computes '{computed['stereo_mode']}')")
    if not parts:
        parts.append("the stereo variant (same InChIKey and stereo mode, but a different set "
                     "of stereoisomers, for example another centre marked as racemic)")
    return ("This is not exactly the compound you confirmed: " + " and ".join(parts) + ". The "
            "drawing, the chosen stereo mode or the registry settings may have changed since you "
            "looked it up. Nothing was registered; look the structure up again and confirm what "
            "is shown then.")


def _op_register(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    structure = _structure_args(params)
    analysis = svc.analyze(**structure, with_svg=False, with_molfile=False)
    computed = analysis["structure"]
    confirmed = params["confirm"]
    if any(computed[key] != confirmed[key] for key in protocol.CONFIRM_KEYS):
        raise RegistryError(_mismatch_message(confirmed, computed), code="confirmation_mismatch")
    ignored = computed["ignored_stereo"]
    accept = bool(ignored) and set(params["accept_ignored_stereo"]) == set(ignored)
    return svc.register(
        molfile=params["molfile"], smiles=params["smiles"], mode=params["mode"],
        experiment_ref=params["experiment_ref"], experiment_code=params["experiment_code"],
        experiment_url=params["experiment_url"], product_index=params["product_index"],
        source=protocol.SOURCE, accept_ignored_stereo=accept, with_molfile=False)


def _op_retire(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.retire(params["id"], params["reason"])


def _op_find(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.find_by_id(params["id"], with_svg=True)


def _op_compounds(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.compounds(text=params["text"], molfile=params["molfile"], smiles=params["smiles"],
                         match=params["match"], order=params["order"], offset=params["offset"],
                         limit=params["limit"], with_svg=True)


def _op_set_name(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.set_name(params["core_stereo_id"], params["name"])


def _op_generate_name(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.generate_name(params["core_stereo_id"], replace=params["replace"])


def _op_set_yield(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.set_yield(params["id"], params["experiment_ref"], params["product_index"],
                         params["isolated"], params["analytical"])


def _op_yields(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.yields(text=params["text"], only_recorded=params["only_recorded"],
                      order=params["order"], offset=params["offset"], limit=params["limit"])


def _op_compound_files(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.compound_files(params["core_stereo_id"])


def _op_read_pdf(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.read_pdf(params["core_stereo_id"], name=params["name"], full_id=params["full_id"],
                        spectrum_key=params["spectrum_key"], offset=params["offset"])


def _op_set_reaction(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.set_reaction(params["experiment_ref"], params["reaction"],
                            experiment_code=params["experiment_code"],
                            experiment_url=params["experiment_url"])


def _op_route(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.route(params["core_stereo_id"], open=params["open"])


def _op_set_limiting(svc: Any, params: dict[str, Any], choice: HomeChoice) -> dict[str, Any]:
    return svc.set_limiting(params["experiment_ref"], params["row"])


_OPERATIONS = {"ping": _op_ping, "analyze": _op_analyze, "register": _op_register,
               "retire": _op_retire, "find": _op_find, "compounds": _op_compounds,
               "set_name": _op_set_name, "generate_name": _op_generate_name,
               "set_yield": _op_set_yield, "yields": _op_yields,
               "compound_files": _op_compound_files, "read_pdf": _op_read_pdf,
               "set_reaction": _op_set_reaction, "route": _op_route,
               "set_limiting": _op_set_limiting}


def _capture_operation(op: str, params: dict[str, Any]) -> dict[str, Any]:
    """One operation of ``protocol.CAPTURE_OPS`` ("Capture reaction"). These
    need no registry: none is opened, and the host configuration is not even
    looked at, so they never answer ``registry_missing``."""
    from . import recognition   # RDKit: imported only once stdout is safe

    return recognition.OPERATIONS[op](params)


def _stop_recognition() -> None:
    """Stop the recognition worker of this host, if one was started."""
    module = sys.modules.get(__package__ + ".recognition")
    if module is not None:
        try:
            module.shutdown()
        except Exception:  # noqa: BLE001 - ending the host never fails on this
            pass


# --------------------------------------------------------------------------- one message

@dataclass
class _Outcome:
    response: dict[str, Any]
    op: str | None = None
    request_id: str | int | None = None
    full_id: str | None = None          # register / retire: the ID, for the log


def handle_message(body: bytes, choice: HomeChoice, *, open_service: OpenService | None = None,
                   log: logging.Logger | None = None) -> _Outcome:
    """Turn one request frame body into its response (never raises)."""
    log = log if log is not None else logging.getLogger(LOGGER_NAME)
    message, problem = _decode(body)
    if problem is not None:
        return _Outcome(_error(None, "bad_request", problem))
    try:
        request = protocol.parse_request(message)
    except protocol.ProtocolError as exc:
        op = message.get("op") if isinstance(message, dict) else None
        return _Outcome(protocol.error_response(exc.request_id, exc.to_dict()),
                        op if isinstance(op, str) and op in protocol.OPS else None,
                        exc.request_id)
    except Exception as exc:  # noqa: BLE001 - parse_request should only raise ProtocolError
        _log_internal(log, None, None, exc)
        return _Outcome(_internal_error(None))

    outcome = _Outcome({}, request.op, request.id)
    try:
        if request.op in protocol.CAPTURE_OPS:
            result = _capture_operation(request.op, request.params)
        else:
            if choice.error is not None:
                raise choice.error
            svc = (open_service or _default_open_service)(choice.home)
            try:
                result = _OPERATIONS[request.op](svc, request.params, choice)
            finally:
                svc.close()
        outcome.response = protocol.ok_response(request.id, result)
        if request.op in ("register", "retire"):
            outcome.full_id = result.get("id")
    except RegistryError as exc:
        outcome.response = protocol.error_response(request.id, exc.to_dict())
    except Exception as exc:  # noqa: BLE001 - reported generically, details in the log
        _log_internal(log, request.op, request.id, exc)
        outcome.response = _internal_error(request.id)
    return outcome


def _internal_error(request_id: str | int | None) -> dict[str, Any]:
    return _error(request_id, "internal_error",
                  "The registry host hit an unexpected error. Nothing more is known here; "
                  "the details are in the host log (logs/host.log in the registry folder).")


def _log_internal(log: logging.Logger, op: str | None, request_id: Any, exc: BaseException) -> None:
    # The traceback, but NOT the exception message: a message could quote
    # the structure, and structures never go to the log.
    stack = "".join(traceback.format_tb(exc.__traceback__))
    log.error("op=%s id=%r internal error %s\n%s", op, request_id, type(exc).__name__, stack)


def _status(response: dict[str, Any]) -> str:
    return "ok" if response.get("ok") else str((response.get("error") or {}).get("code"))


def encode_response(response: dict[str, Any], op: str | None, *,
                    max_bytes: int = protocol.MAX_RESPONSE_BYTES,
                    full_id: str | None = None) -> bytes:
    """Serialise ``response``; drop thumbnails (analyze, find, compounds) or
    the document text (route), or answer ``response_too_large`` when it does
    not fit ``max_bytes``."""
    return _encode(response, op, max_bytes, full_id)[0]


def _encode(response: dict[str, Any], op: str | None, max_bytes: int,
            full_id: str | None) -> tuple[bytes, str]:
    """(frame body, ``ok`` or the error code actually sent)."""
    data = _dumps(response)
    if len(data) <= max_bytes:
        return data, _status(response)
    result = response.get("result") if response.get("ok") else None
    if op == "analyze" and isinstance(result, dict):
        others = result.get("other_stereos") or []
        if any(s.get("svg") is not None for s in others):
            for s in others:
                s["svg"] = None
            result.setdefault("warnings", []).append(THUMBNAILS_OTHERS_DROPPED)
            data = _dumps(response)
            if len(data) <= max_bytes:
                return data, "ok"
        structure = result.get("structure") or {}
        if structure.get("svg") is not None:
            structure["svg"] = None
            result.setdefault("warnings", []).append(THUMBNAIL_MAIN_DROPPED)
            data = _dumps(response)
            if len(data) <= max_bytes:
                return data, "ok"
    if op in _THUMBNAIL_LISTS and isinstance(result, dict):
        entries = [e for e in (result.get(_THUMBNAIL_LISTS[op]) or []) if isinstance(e, dict)]
        if any(e.get("svg") is not None for e in entries):
            for e in entries:
                if "svg" in e:
                    e["svg"] = None
            result.setdefault("warnings", []).append(THUMBNAILS_DROPPED)
            data = _dumps(response)
            if len(data) <= max_bytes:
                return data, "ok"
    if op == "route" and isinstance(result, dict) and result.get("cdxml") is not None:
        result["cdxml"] = None
        result.setdefault("warnings", []).append(ROUTE_DOCUMENT_DROPPED)
        data = _dumps(response)
        if len(data) <= max_bytes:
            return data, "ok"
    message = (f"The answer is too large to send to the browser ({len(data)} bytes, limit "
               f"{max_bytes}).")
    if full_id is not None and op == "register":
        message += (f" The registration itself was carried out: the ID is {full_id}. Look it up "
                    f"with 'compound-registry lookup --id {full_id}'.")
    elif full_id is not None and op == "retire":
        message += f" {full_id} was retired."
    return _dumps(_error(response.get("id"), "response_too_large", message)), "response_too_large"


# --------------------------------------------------------------------------- loop

def serve(stdin: BinaryIO, stdout: BinaryIO, choice: HomeChoice, *,
          open_service: OpenService | None = None,
          max_request_bytes: int = protocol.MAX_REQUEST_BYTES,
          max_response_bytes: int = protocol.MAX_RESPONSE_BYTES,
          log: logging.Logger | None = None,
          resolve: Callable[[], HomeChoice] | None = None) -> int:
    """Answer frames from ``stdin`` on ``stdout`` until EOF. Returns the exit
    code: 0 at a clean end (EOF between frames, Firefox went away, or the
    registry configuration changed), 1 when the stream can no longer be
    trusted.

    ``resolve`` re-decides the home before every request; if the result
    differs from ``choice``, that request is answered with
    ``registry_changed`` and the loop ends, so the next connection starts
    from the new configuration.

    Requests are answered one at a time and in order, with ONE exception:
    ``recognize`` (10 to 60 seconds, minutes while the models load) is
    answered from a thread of its own, so that the other capture operations
    (the status of another capture page, a structure the user just edited) are
    not kept waiting behind it beyond the time the extension gives them. Its
    answer is therefore written when it is ready, possibly after the answers
    of later requests; the extension matches answers by ``id``. At the end of
    the input the loop waits for the recognitions still running."""
    log = log if log is not None else logging.getLogger(LOGGER_NAME)
    write_lock = threading.Lock()
    running: list[threading.Thread] = []

    def answer(body: bytes) -> bool:
        """Answer one request. False when the output is closed."""
        started = time.perf_counter()
        outcome = handle_message(body, choice, open_service=open_service, log=log)
        try:
            data, status = _encode(outcome.response, outcome.op, max_response_bytes,
                                   outcome.full_id)
        except (TypeError, ValueError) as exc:  # NaN, unpaired surrogate, odd type
            _log_internal(log, outcome.op, outcome.request_id, exc)
            data, status = _dumps(_internal_error(outcome.request_id)), "internal_error"
        try:
            with write_lock:
                _write_frame(stdout, data)
        except OSError:  # includes BrokenPipeError: Firefox went away
            log.info("output closed while answering; stopping")
            return False
        # The ID is logged for register / retire, also when the answer was too
        # large to send (the ID was issued all the same).
        extra = f" full_id={outcome.full_id}" if outcome.full_id else ""
        log.info("op=%s id=%r %s %.0f ms %d bytes%s", outcome.op, outcome.request_id, status,
                 (time.perf_counter() - started) * 1000, len(data), extra)
        return True

    def answer_aside(body: bytes) -> None:
        try:
            answer(body)
        except BaseException as exc:  # noqa: BLE001 - a thread must never print or raise
            _log_internal(log, "recognize", None, exc)

    try:
        return _serve_loop(stdin, stdout, choice, max_request_bytes, log, resolve, write_lock,
                           running, answer, answer_aside)
    finally:
        for thread in running:
            thread.join()


def _serve_loop(stdin: BinaryIO, stdout: BinaryIO, choice: HomeChoice, max_request_bytes: int,
                log: logging.Logger, resolve: Callable[[], HomeChoice] | None,
                write_lock: Any, running: list[threading.Thread],
                answer: Callable[[bytes], bool], answer_aside: Callable[[bytes], None]) -> int:
    """The loop of :func:`serve`."""
    while True:
        try:
            header = _read_exact(stdin, _HEADER.size)
            if not header:
                log.info("input closed; stopping")
                return 0
            if len(header) < _HEADER.size:
                log.error("input ended inside a frame header (%d of 4 bytes)", len(header))
                return 1
            (length,) = _HEADER.unpack(header)
            if length > max_request_bytes:
                log.error("request frame of %d bytes exceeds the limit of %d; stopping",
                          length, max_request_bytes)
                try:
                    with write_lock:
                        _write_frame(stdout, _dumps(_error(
                            None, "frame_too_large",
                            f"The request is {length} bytes; the limit is {max_request_bytes}. "
                            "The host stops because the rest of the input cannot be trusted.")))
                except OSError:
                    pass
                return 1
            body = _read_exact(stdin, length)
        except OSError as exc:
            log.error("reading the input failed (%s); stopping", type(exc).__name__)
            return 1
        if len(body) < length:
            log.error("input ended inside a frame (%d of %d bytes)", len(body), length)
            return 1

        capture_op = _capture_op_of(body)
        if resolve is not None and capture_op is None:
            changed = _changed_choice(choice, resolve)
            if changed is not None:
                request_id = _request_id_of(body)
                log.warning("op=? id=%r registry_changed (home source %s -> %s); stopping",
                            request_id, choice.source, changed.source)
                try:
                    with write_lock:
                        _write_frame(stdout, _dumps(_error(
                            request_id, "registry_changed",
                            f"The registry configuration changed while the extension was "
                            f"connected: it was {choice.describe()}, it is now "
                            f"{changed.describe()}. Nothing was written. Reopen the panel or "
                            "look the structure up again; the extension then uses the registry "
                            "that is configured now.")))
                except OSError:
                    pass
                return 0

        running[:] = [thread for thread in running if thread.is_alive()]
        if capture_op == "recognize" and len(running) < MAX_RECOGNITIONS_ASIDE:
            thread = threading.Thread(target=answer_aside, args=(body,), daemon=True,
                                      name="recognize")
            running.append(thread)
            thread.start()
        elif not answer(body):
            return 0


def _changed_choice(choice: HomeChoice, resolve: Callable[[], HomeChoice]) -> HomeChoice | None:
    """The new choice if the configuration no longer says ``choice``, else None."""
    try:
        current = resolve()
    except Exception as exc:  # noqa: BLE001 - resolve_home does not raise; be safe anyway
        current = HomeChoice(None, choice.source, error=ConfigError(
            f"The registry configuration could not be read ({type(exc).__name__})."))
    return None if current.identity() == choice.identity() else current


#: At most this many ``recognize`` requests wait or run beside the loop; one
#: more is answered in the loop itself, which then reads nothing until it is
#: done. (The worker reads one picture at a time anyway.)
MAX_RECOGNITIONS_ASIDE = 4


def _capture_op_of(body: bytes) -> str | None:
    """The operation, for a request that needs no registry (``CAPTURE_OPS``),
    else None. A changed registry configuration is no reason to refuse such a
    request (or to stop the host that owns the recognition worker)."""
    message, problem = _decode(body)
    if problem is None and isinstance(message, dict):
        op = message.get("op")
        if isinstance(op, str) and op in protocol.CAPTURE_OPS:
            return op
    return None


def _request_id_of(body: bytes) -> str | int | None:
    message, problem = _decode(body)
    if problem is not None:
        return None
    try:
        return protocol.parse_request(message).id
    except protocol.ProtocolError as exc:
        return exc.request_id
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- process

def _isolate_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Keep private binary handles on fd 0 and fd 1 for frames, then point
    both at the null device and ``sys.stdout`` at stderr, so neither ``print``
    nor C-level output can reach the protocol stream, and nothing but the
    frame loop can touch the request pipe. Returns (frames_in, frames_out).

    The input matters on Windows since ``recognize`` is answered from a thread
    (:func:`serve`): while the loop waits for the next frame, any other call
    on the same pipe handle blocks until a frame arrives. A library that looks
    at the standard input while it is loaded (seen: numpy's import, in the
    thread) would hang for good. With fd 0 on the null device it finds that
    instead; the frames come through a duplicate nobody else knows."""
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(in_fd, os.O_BINARY)
        msvcrt.setmode(out_fd, os.O_BINARY)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_fd, 1)
    os.close(null_fd)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.close(null_fd)
    sys.stdout = sys.stderr if sys.stderr is not None else open(os.devnull, "w")
    frames_out = open(out_fd, "wb")             # buffered; flushed after every frame
    frames_in = open(in_fd, "rb")
    sys.stdin = open(os.devnull, "r")           # nothing else may consume frames
    return frames_in, frames_out


class _ClosingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Closes the file after every record, so a long-running host never keeps
    a handle open inside the registry folder (Windows could not move or
    rename the folder while Firefox stays connected)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            if self.stream is not None:
                try:
                    self.stream.close()
                finally:
                    self.stream = None  # reopened (append) at the next record


def _setup_logging(log_dir: Path | None) -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False
    logging.raiseExceptions = False   # a failed rotation (file in use) must stay silent
    for handler in list(log.handlers):
        log.removeHandler(handler)
    handler: logging.Handler = logging.NullHandler()
    # Only inside an EXISTING home folder: creating the home here would make a
    # moved or missing registry folder look present.
    if log_dir is not None and log_dir.parent.is_dir():
        try:
            log_dir.mkdir(exist_ok=True)
            handler = _ClosingRotatingFileHandler(
                log_dir / LOG_FILENAME, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                encoding="utf-8", delay=True)
        except OSError:
            handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(message)s"))
    log.addHandler(handler)
    return log


def _caller(args: list[str]) -> str:
    """Who started the host, for the log: ``chrome``, ``firefox`` or ``manual``."""
    if is_chrome_caller(args):
        return "chrome"
    return "firefox" if len(args) >= 2 else "manual"


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``compound-registry-host``. Firefox passes the manifest
    path and the caller's add-on ID; Chrome passes the caller's origin (and on
    Windows ``--parent-window=...``, ignored); without arguments the host runs
    anyway (tests, manual runs)."""
    frames_in, frames_out = _isolate_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    choice = resolve_home(args)
    log = _setup_logging(choice.log_dir)
    caller = _caller(args)
    log.info("host %s started by %s (protocol %d, home source %s%s)", __version__, caller,
             protocol.PROTOCOL_VERSION, choice.source,
             ", host configuration INVALID" if choice.error is not None else "")
    try:
        if caller == "chrome":
            extra = [a for a in args[1:] if not a.startswith(CHROME_PARENT_WINDOW)]
            if extra:
                log.warning("ignored %d unexpected argument(s) after the Chrome origin", len(extra))
            if args[0] != protocol.CHROME_ORIGIN:
                log.warning("refused: started by Chrome extension %r, not %r", args[0][:200],
                            protocol.CHROME_ORIGIN)
                try:
                    _write_frame(frames_out, _dumps(_error(
                        None, "forbidden_caller",
                        f"This host only serves the extension {protocol.CHROME_EXTENSION_ID} "
                        f"in Chrome and the add-on {protocol.EXTENSION_ID} in Firefox.")))
                except OSError:
                    pass
                return 1
        elif len(args) >= 2 and args[1] != protocol.EXTENSION_ID:
            log.warning("refused: started by add-on %r, not %r", args[1][:200],
                        protocol.EXTENSION_ID)
            try:
                _write_frame(frames_out, _dumps(_error(
                    None, "forbidden_caller",
                    f"This host only serves the add-on {protocol.EXTENSION_ID}.")))
            except OSError:
                pass
            return 1
        code = serve(frames_in, frames_out, choice, log=log,
                     resolve=lambda: resolve_home(args))
        log.info("host stopped (exit code %d)", code)
        return code
    except BaseException as exc:  # noqa: BLE001 - last resort, never print to stdout
        _log_internal(log, None, None, exc)
        return 1
    finally:
        _stop_recognition()
        for handler in list(log.handlers):
            handler.close()
        try:
            frames_out.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
