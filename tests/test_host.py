"""In-process tests of compound_registry.host: framing, dispatch, limits, home choice.

``serve`` runs on in-memory streams with a registry in ``tmp_path``. The real
process (stdout protection, caller check, exit codes) is covered by
test_host_subprocess.py.
"""

from __future__ import annotations

import io
import json
import logging
import struct
from pathlib import Path

import pytest

from compound_registry import __version__, host, install_host, protocol
from compound_registry.errors import ConfigError
from compound_registry.host import HomeChoice, encode_response, resolve_home, serve
from compound_registry.models import MODE_OVERRIDES
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"
AMINE_S = "C[C@@H](N)c1ccccc1"
ETHANOL = "CCO"
PROPANOL = "CCCO"
IGNORED_SMILES = "C[C@H](C)O"   # '@' on a non-stereocentre: reported as ignored stereo

_REAL_DEFAULT_TARGET = install_host.default_target   # only ever called with tmp_path folders


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def create_registry(path: Path) -> Path:
    """The host never creates a registry, so tests create it explicitly."""
    RegistryService.open(path).close()
    return path


@pytest.fixture
def home(tmp_path) -> Path:
    return create_registry(tmp_path / "reg")


@pytest.fixture
def choice(home) -> HomeChoice:
    return HomeChoice(home, "host-config")


# --------------------------------------------------------------------------- helpers

def frame(document) -> bytes:
    body = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
    return struct.pack("=I", len(body)) + body


def parse_frames(data: bytes) -> list[dict]:
    """Every byte must belong to a frame."""
    docs, pos = [], 0
    while pos < len(data):
        assert len(data) - pos >= 4, "trailing bytes after the last frame"
        (n,) = struct.unpack_from("=I", data, pos)
        body = data[pos + 4: pos + 4 + n]
        assert len(body) == n, "truncated frame in the output"
        docs.append(json.loads(body.decode("utf-8")))
        pos += 4 + n
    return docs


def run(choice, *messages, **kwargs) -> tuple[int, list[dict]]:
    data = b"".join(m if isinstance(m, bytes) else frame(m) for m in messages)
    out = io.BytesIO()
    code = serve(io.BytesIO(data), out, choice, **kwargs)
    return code, parse_frames(out.getvalue())


def call(choice, op, params=None, request_id=1, **kwargs) -> dict:
    code, docs = run(choice, {"id": request_id, "v": 1, "op": op, "params": params or {}},
                     **kwargs)
    assert code == 0 and len(docs) == 1
    assert docs[0]["id"] == request_id
    return docs[0]


def ok(doc: dict) -> dict:
    assert doc["ok"] is True, doc
    return doc["result"]


def error(doc: dict, code: str) -> dict:
    assert doc["ok"] is False and doc["error"]["code"] == code, doc
    return doc["error"]


def analyze(choice, smiles, **params) -> dict:
    return ok(call(choice, "analyze", {"smiles": smiles, **params}))


def confirm_of(structure: dict, **changed) -> dict:
    """The ``confirm`` value for an analysed structure, optionally falsified."""
    return {**{key: structure[key] for key in protocol.CONFIRM_KEYS}, **changed}


def register(choice, smiles, ref="E1", index=1, confirm=None, serve_kwargs=None,
             **params) -> dict:
    if confirm is None:
        s = analyze(choice, smiles, **({"mode": params["mode"]} if "mode" in params else {}))
        confirm = confirm_of(s["structure"])
    return call(choice, "register", {"smiles": smiles, "experiment_ref": ref,
                                     "product_index": index, "confirm": confirm, **params},
                **(serve_kwargs or {}))


def batches(home) -> int:
    with RegistryService.open(home) as svc:
        return svc.doctor()["stats"]["batches"]


# --------------------------------------------------------------------------- framing

def test_clean_eof_without_frames():
    out = io.BytesIO()
    assert serve(io.BytesIO(b""), out, HomeChoice(None, "default")) == 0
    assert out.getvalue() == b""


def test_ping(choice, home):
    result = ok(call(choice, "ping"))
    assert set(result) == set(protocol.PING_RESULT_KEYS) | {
        "protocol_version", "host_version", "home_source", "mode_options"}
    assert result["protocol_version"] == protocol.PROTOCOL_VERSION
    assert result["host_version"] == __version__
    assert result["home_source"] == "host-config"
    assert result["mode_options"] == list(MODE_OVERRIDES)
    assert result["home"] == str(home)
    assert result["integrity"] == "ok"


def test_ping_with_another_version_still_answers(choice):
    code, docs = run(choice, {"id": "p", "v": 99, "op": "ping"})
    assert ok(docs[0])["protocol_version"] == protocol.PROTOCOL_VERSION


def test_several_frames_in_one_go_are_answered_in_order(choice):
    code, docs = run(choice, *({"id": i, "v": 1, "op": "ping"} for i in range(5)))
    assert code == 0 and [d["id"] for d in docs] == [0, 1, 2, 3, 4]


class Trickle(io.RawIOBase):
    """Delivers at most one byte per read (short reads)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def readable(self):
        return True

    def read(self, n=-1):
        if self.pos >= len(self.data):
            return b""
        self.pos += 1
        return self.data[self.pos - 1: self.pos]


def test_short_reads_are_handled(choice):
    data = frame({"id": 1, "v": 1, "op": "ping"}) + frame({"id": 2, "v": 1, "op": "ping"})
    out = io.BytesIO()
    assert serve(Trickle(data), out, choice) == 0
    assert [d["id"] for d in parse_frames(out.getvalue())] == [1, 2]


@pytest.mark.parametrize("body,fragment", [
    (b"", "empty"),
    (b"\xff\xfe{}", "UTF-8"),
    (b"{not json", "JSON"),
    (b'{"id": 1, "v": NaN, "op": "ping"}', "NaN"),
    (b'{"id": 1, "v": Infinity, "op": "ping"}', "Infinity"),
    (b'{"id": 1, "v": -Infinity, "op": "ping"}', "Infinity"),
    (b'{"id": 1, "id": 2, "v": 1, "op": "ping"}', "twice"),
    (b"[" * 200_000 + b"]" * 200_000, "nested"),
    (b'{"id": 1, "v": 1, "op": "analyze", "params": {"smiles": "\\ud800"}}', "Unicode"),
    ("﻿{}".encode("utf-8"), "JSON"),
    (b"1" * 5000, "JSON"),   # integer longer than Python's int digit limit
])
def test_undecodable_frames_get_bad_request_and_the_loop_continues(choice, body, fragment):
    code, docs = run(choice, frame(body), {"id": "next", "v": 1, "op": "ping"})
    assert code == 0 and len(docs) == 2
    assert docs[0]["id"] is None
    assert fragment.lower() in error(docs[0], "bad_request")["message"].lower()
    assert docs[1]["id"] == "next" and docs[1]["ok"]


def test_protocol_errors_echo_the_request_id(choice):
    code, docs = run(choice,
                     {"id": "a", "v": 1, "op": "nope"},
                     {"id": "b", "v": 2, "op": "analyze", "params": {"smiles": "C"}},
                     {"id": "c", "v": 1, "op": "analyze", "params": {"smiles": "C", "x": 1}},
                     {"id": True, "v": 1, "op": "ping"},
                     [1, 2])
    assert [d["id"] for d in docs] == ["a", "b", "c", None, None]
    error(docs[0], "unknown_op")
    error(docs[1], "protocol_mismatch")
    error(docs[2], "bad_request")
    error(docs[3], "bad_request")
    error(docs[4], "bad_request")


def test_frame_too_large_stops_the_host(choice):
    big = {"id": 1, "v": 1, "op": "analyze", "params": {"smiles": "C" * 200}}
    code, docs = run(choice, big, {"id": 2, "v": 1, "op": "ping"}, max_request_bytes=100)
    assert code == 1
    assert len(docs) == 1 and docs[0]["id"] is None
    error(docs[0], "frame_too_large")


def test_frame_at_the_request_limit_is_accepted(choice):
    body = json.dumps({"id": 1, "v": 1, "op": "ping"}).encode()
    code, docs = run(choice, frame(body), max_request_bytes=len(body))
    assert code == 0 and docs[0]["ok"]


def test_declared_length_over_the_real_limit(choice):
    header = struct.pack("=I", protocol.MAX_REQUEST_BYTES + 1)
    code, docs = run(choice, header + b"{}")
    assert code == 1 and docs[0]["error"]["code"] == "frame_too_large"


@pytest.mark.parametrize("data", [b"\x05", b"\x05\x00\x00", struct.pack("=I", 10) + b"{}"])
def test_truncated_input_exits_with_1(choice, data):
    out = io.BytesIO()
    assert serve(io.BytesIO(frame({"id": 1, "v": 1, "op": "ping"}) + data), out, choice) == 1
    assert len(parse_frames(out.getvalue())) == 1   # the complete frame was still answered


class BrokenOut(io.RawIOBase):
    def writable(self):
        return True

    def write(self, data):
        raise BrokenPipeError(32, "Broken pipe")


def test_broken_output_stops_quietly(choice):
    data = frame({"id": 1, "v": 1, "op": "ping"}) * 2
    assert serve(io.BytesIO(data), BrokenOut(), choice) == 0


def test_output_is_utf8_not_ascii_escaped(choice):
    r = ok(register(choice, ETHANOL, experiment_code="José-µ"))
    assert r["batch"]["experiment_code"] == "José-µ"
    raw = encode_response(protocol.ok_response(1, {"t": "José"}), "ping")
    assert "José".encode("utf-8") in raw and b"\\u" not in raw


# --------------------------------------------------------------------------- operations

def test_analyze_is_the_service_result_with_svg(choice, home):
    result = analyze(choice, AMINE_R, experiment_ref="E1", product_index=1)
    with RegistryService.open(home) as svc:
        expected = svc.analyze(smiles=AMINE_R, experiment_ref="E1", product_index=1,
                               with_svg=True, with_molfile=False)
    assert result == expected
    assert result["status"] == "new" and result["structure"]["svg"].startswith("<svg")


def test_register_and_repeat(choice, home):
    first = ok(register(choice, AMINE_R, ref="4180", experiment_code="ABC-008",
                        experiment_url="https://eln.example.org/e/4180"))
    assert first["created"] is True and first["id"] == "000001_01_001"
    assert first["batch"]["source"] == protocol.SOURCE
    assert first["batch"]["experiment_url"] == "https://eln.example.org/e/4180"
    assert "molfile" not in first["batch"]
    again = ok(register(choice, AMINE_R, ref="4180"))
    assert again["id"] == first["id"] and again["created"] is False
    with RegistryService.open(home) as svc:
        assert svc.get("000001_01_001")["source"] == "extension"
    a = analyze(choice, AMINE_R, experiment_ref="4180")
    assert a["status"] == "registered_here" and a["id"] == "000001_01_001"


def test_register_with_mode(choice):
    r = ok(register(choice, AMINE_R, mode="rac"))
    assert r["structure"]["stereo_mode"] == "rac"


def test_confirmation_mismatch_on_inchikey_writes_nothing(choice, home):
    s = analyze(choice, AMINE_S)["structure"]
    doc = register(choice, AMINE_R, confirm=confirm_of(s))
    message = error(doc, "confirmation_mismatch")["message"]
    assert s["inchikey"] in message and "Nothing was registered" in message
    assert "look the structure up again" in message
    assert batches(home) == 0


def test_confirmation_mismatch_on_stereo_mode_writes_nothing(choice, home):
    s = analyze(choice, AMINE_R)["structure"]
    doc = register(choice, AMINE_R, confirm=confirm_of(s, stereo_mode="rac"))
    message = error(doc, "confirmation_mismatch")["message"]
    assert "'rac'" in message and "'abs'" in message and "stereo mode" in message
    assert "InChIKey" not in message.split("stereo mode")[0]  # the key itself matched
    assert batches(home) == 0
    # the same with the mode the user did choose
    rac = analyze(choice, AMINE_R, mode="rac")["structure"]
    assert rac["inchikey"] == s["inchikey"] and rac["key_digest"] != s["key_digest"]
    ok(register(choice, AMINE_R, mode="rac", confirm=confirm_of(rac)))


def test_confirmation_mismatch_on_key_digest_alone_writes_nothing(choice, home):
    # Same InChIKey, same mode ("mixed"), but the racemic centre is another one:
    # two different stereo variants that only the key digest tells apart.
    one, other = "C[C@H](O)[C@H](C)N |a:1,&1:3|", "C[C@H](O)[C@H](C)N |&1:1,a:3|"
    s, t = analyze(choice, one)["structure"], analyze(choice, other)["structure"]
    assert s["inchikey"] == t["inchikey"] and s["stereo_mode"] == t["stereo_mode"] == "mixed"
    assert s["key_digest"] != t["key_digest"]
    message = error(register(choice, other, confirm=confirm_of(s)),
                    "confirmation_mismatch")["message"]
    assert "stereo variant" in message and "Nothing was registered" in message
    assert batches(home) == 0
    first = ok(register(choice, one, confirm=confirm_of(s)))
    second = ok(register(choice, other, ref="E2", confirm=confirm_of(t)))
    assert first["batch"]["core_no"] == second["batch"]["core_no"]
    assert first["batch"]["core_stereo_id"] != second["batch"]["core_stereo_id"]


def test_confirmation_uses_the_configured_default_mode(choice, home):
    s = analyze(choice, AMINE_R)["structure"]
    assert s["stereo_mode"] == "abs"
    config = home / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        'default_stereo_mode = "abs"', 'default_stereo_mode = "rac"'), encoding="utf-8")
    doc = register(choice, AMINE_R, confirm=confirm_of(s))
    error(doc, "confirmation_mismatch")
    assert batches(home) == 0


def test_ignored_stereo_needs_the_exact_set(choice, home):
    s = analyze(choice, IGNORED_SMILES)["structure"]
    ignored = s["ignored_stereo"]
    assert ignored
    confirm = confirm_of(s)
    error(register(choice, IGNORED_SMILES, confirm=confirm), "stereo_not_in_key")
    error(register(choice, IGNORED_SMILES, confirm=confirm,
                   accept_ignored_stereo=ignored + ["something else"]), "stereo_not_in_key")
    error(register(choice, IGNORED_SMILES, confirm=confirm,
                   accept_ignored_stereo=["something else"]), "stereo_not_in_key")
    assert batches(home) == 0
    r = ok(register(choice, IGNORED_SMILES, confirm=confirm,
                    accept_ignored_stereo=list(reversed(ignored)) + ignored))
    assert r["created"] and r["accepted_ignored_stereo"] == ignored


def test_accept_list_without_ignored_stereo_is_harmless(choice):
    r = ok(register(choice, ETHANOL, accept_ignored_stereo=["x"]))
    assert r["created"] and r["accepted_ignored_stereo"] == []


def test_slot_conflict_carries_existing_id(choice, home):
    first = ok(register(choice, ETHANOL, ref="E1", index=1))
    e = error(register(choice, PROPANOL, ref="E1", index=1), "slot_conflict")
    assert e["existing_id"] == first["id"]
    assert batches(home) == 1


def test_retire(choice, home):
    first = ok(register(choice, ETHANOL))
    r = ok(call(choice, "retire", {"id": first["id"], "reason": "wrong product"}))
    assert r["id"] == first["id"] and r["batch"]["retired"] is True
    assert r["batch"]["retired_reason"] == "wrong product"
    error(call(choice, "retire", {"id": first["id"], "reason": "again"}), "already_retired")
    error(call(choice, "retire", {"id": "999999_01_001", "reason": "x"}), "not_found")


def test_chemistry_errors_are_registry_errors(choice):
    error(call(choice, "analyze", {"smiles": "C1CC("}), "structure_parse_failed")
    error(call(choice, "analyze", {"smiles": "CCO", "mode": "weird"}), "invalid_mode")


# --------------------------------------------------------------------------- service lifetime

def test_a_fresh_service_per_request(choice, home):
    opened, closed = [], []

    class Spy(RegistryService):
        def close(self):
            closed.append(self)
            super().close()

    def open_service(h):
        opened.append(h)
        return Spy.open(h)

    run(choice, *({"id": i, "v": 1, "op": "ping"} for i in range(3)), open_service=open_service)
    assert opened == [home] * 3 and len(closed) == 3 and len(set(map(id, closed))) == 3


def test_changes_made_by_the_cli_between_requests_are_seen(choice, home):
    first = ok(register(choice, ETHANOL, ref="E1"))
    with RegistryService.open(home) as svc:   # e.g. 'compound-registry retire'
        svc.retire(first["id"], "made in the CLI")
    second = ok(register(choice, ETHANOL, ref="E1"))
    assert second["created"] is True and second["id"] != first["id"]


def test_service_is_closed_after_an_error(choice):
    closed = []

    class Spy(RegistryService):
        def close(self):
            closed.append(True)
            super().close()

    error(call(choice, "analyze", {"smiles": "C1CC("}, open_service=Spy.open),
          "structure_parse_failed")
    assert closed == [True]


def test_registry_error_while_opening(choice):
    def open_service(h):
        raise ConfigError("config.toml is broken")

    e = error(call(choice, "ping", open_service=open_service), "config_invalid")
    assert e["message"] == "config.toml is broken"


def test_unexpected_exception_is_internal_error_without_details(choice, caplog):
    def open_service(h):
        message = "|".join(["hidden", AMINE_R])   # built at run time: not in the source line
        raise ValueError(message)

    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        doc = call(choice, "analyze", {"smiles": AMINE_R}, open_service=open_service)
    e = error(doc, "internal_error")
    assert "hidden" not in e["message"] and AMINE_R not in e["message"]
    assert "ValueError" in caplog.text and "open_service" in caplog.text   # type + traceback
    assert "hidden|" not in caplog.text and AMINE_R not in caplog.text


def test_invalid_host_config_answers_every_request_with_config_invalid(tmp_path):
    bad = HomeChoice(None, "host-config", error=ConfigError("The host configuration X is invalid"))
    opened = []
    code, docs = run(bad, {"id": 1, "v": 1, "op": "ping"},
                     {"id": 2, "v": 1, "op": "analyze", "params": {"smiles": "C"}},
                     frame(b"garbage"),
                     open_service=lambda h: opened.append(h))
    assert code == 0 and opened == []
    assert [d["id"] for d in docs] == [1, 2, None]
    error(docs[0], "config_invalid")
    error(docs[1], "config_invalid")
    error(docs[2], "bad_request")
    assert not (tmp_path / "env-home").exists()


# --------------------------------------------------------------------------- response size

def _analysis_sizes(choice) -> tuple[int, int, int, dict]:
    ok(register(choice, AMINE_R, ref="E1"))
    result = analyze(choice, AMINE_S)
    assert result["other_stereos"] and result["other_stereos"][0]["svg"]
    full = len(encode_response(protocol.ok_response(1, json.loads(json.dumps(result))), "analyze"))
    no_others = json.loads(json.dumps(result))
    for s in no_others["other_stereos"]:
        s["svg"] = None
    no_others["warnings"].append(host.THUMBNAILS_OTHERS_DROPPED)
    mid = len(encode_response(protocol.ok_response(1, no_others), "analyze"))
    no_svg = json.loads(json.dumps(no_others))
    no_svg["structure"]["svg"] = None
    no_svg["warnings"].append(host.THUMBNAIL_MAIN_DROPPED)
    small = len(encode_response(protocol.ok_response(1, no_svg), "analyze"))
    assert small < mid < full
    return full, mid, small, result


def test_large_analysis_drops_other_thumbnails_first(choice):
    full, mid, small, _ = _analysis_sizes(choice)
    r = ok(call(choice, "analyze", {"smiles": AMINE_S}, max_response_bytes=mid))
    assert all(s["svg"] is None for s in r["other_stereos"])
    assert r["structure"]["svg"].startswith("<svg")
    assert r["warnings"] == [host.THUMBNAILS_OTHERS_DROPPED]


def test_large_analysis_then_drops_its_own_thumbnail(choice):
    full, mid, small, _ = _analysis_sizes(choice)
    r = ok(call(choice, "analyze", {"smiles": AMINE_S}, max_response_bytes=small))
    assert r["structure"]["svg"] is None
    assert r["warnings"] == [host.THUMBNAILS_OTHERS_DROPPED, host.THUMBNAIL_MAIN_DROPPED]


def test_analysis_that_never_fits_is_response_too_large(choice):
    full, mid, small, _ = _analysis_sizes(choice)
    doc = call(choice, "analyze", {"smiles": AMINE_S}, max_response_bytes=small - 1)
    assert "too large" in error(doc, "response_too_large")["message"]


def test_analysis_without_other_variants_drops_only_its_thumbnail(choice):
    result = analyze(choice, ETHANOL)
    size = len(encode_response(protocol.ok_response(1, result), "analyze"))
    r = ok(call(choice, "analyze", {"smiles": ETHANOL}, max_response_bytes=size - 1))
    assert r["structure"]["svg"] is None and r["warnings"] == [host.THUMBNAIL_MAIN_DROPPED]


def test_other_ops_get_response_too_large(choice):
    error(call(choice, "ping", max_response_bytes=200), "response_too_large")


def test_every_protocol_op_is_served():
    assert set(host._OPERATIONS) == set(protocol.OPS)


def test_large_route_answer_drops_the_document_but_keeps_the_file():
    result = {"core_stereo_id": "000001_01", "file": {"path": "C:/x.cdxml", "name": "x.cdxml"},
              "opened": True, "open_message": None, "steps": 1, "experiments": ["E1"],
              "missing": [], "notes": [], "cdxml": "<CDXML>" + "x" * 3000 + "</CDXML>",
              "warnings": []}
    doc = json.loads(encode_response(protocol.ok_response(1, result), "route", max_bytes=1000))
    r = ok(doc)
    assert r["cdxml"] is None and r["warnings"] == [host.ROUTE_DOCUMENT_DROPPED]
    assert r["file"]["name"] == "x.cdxml" and r["opened"] is True


def test_too_large_register_answer_still_names_the_issued_id(choice, home, caplog):
    s = analyze(choice, ETHANOL)["structure"]
    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        doc = register(choice, ETHANOL, confirm=confirm_of(s),
                       serve_kwargs={"max_response_bytes": 300})
    e = error(doc, "response_too_large")
    assert "000001_01_001" in e["message"]
    assert batches(home) == 1
    assert "full_id=000001_01_001" in caplog.text


# --------------------------------------------------------------------------- logging

def test_log_lines_carry_op_id_status_and_full_id_but_no_structures(choice, caplog):
    mol_marker = "M  END"
    with caplog.at_level(logging.INFO, logger=host.LOGGER_NAME):
        r = ok(register(choice, AMINE_R, ref="E-log"))
        call(choice, "retire", {"id": r["id"], "reason": "reason text"}, request_id="ret-1")
        error(call(choice, "analyze", {"smiles": "C1CC("}), "structure_parse_failed")
    text = caplog.text
    assert "op=register" in text and f"full_id={r['id']}" in text
    assert "op=retire id='ret-1' ok" in text
    assert "structure_parse_failed" in text
    for secret in (AMINE_R, r["structure"]["inchikey"], r["structure"]["inchi"], "C1CC(",
                   mol_marker, "E-log", "reason text"):
        assert secret not in text


# --------------------------------------------------------------------------- resolve_home

def _manifest(tmp_path) -> Path:
    folder = tmp_path / "native-host"
    folder.mkdir(exist_ok=True)
    manifest = folder / "compound_registry.json"
    manifest.write_text("{}", encoding="utf-8")
    return manifest


def test_home_from_host_config(tmp_path):
    manifest = _manifest(tmp_path)
    target = tmp_path / "chosen"
    (manifest.parent / "compound_registry.config.json").write_text(
        json.dumps({"home": str(target)}), encoding="utf-8")
    c = resolve_home([str(manifest), protocol.EXTENSION_ID])
    assert c == HomeChoice(target, "host-config", log_dir=target / "logs")


def test_manifest_without_sidecar_uses_env(tmp_path):
    manifest = _manifest(tmp_path)
    c = resolve_home([str(manifest), protocol.EXTENSION_ID])
    assert c.source == "env" and c.home is None and c.error is None
    assert c.log_dir == tmp_path / "env-home" / "logs"


def test_default_when_env_is_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", "  ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    c = resolve_home([])
    assert c.source == "default" and c.home is None
    assert c.log_dir == tmp_path / "user" / "CompoundRegistry" / "logs"


def test_relative_env_home_gives_no_log_dir(monkeypatch):
    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", "relative/reg")
    c = resolve_home([])
    assert c.source == "env" and c.log_dir is None and c.error is None


def test_sidecar_is_only_read_next_to_a_real_manifest(tmp_path):
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    (tmp_path / "compound_registry.config.json").write_text(
        json.dumps({"home": str(tmp_path / "x")}), encoding="utf-8")
    assert resolve_home([str(other), protocol.EXTENSION_ID]).source == "env"
    missing = tmp_path / "gone" / "compound_registry.json"
    assert resolve_home([str(missing), protocol.EXTENSION_ID]).source == "env"


@pytest.mark.parametrize("content,problem", [
    (b"not json", "not valid JSON"),
    (b"[1, 2]", "JSON object"),
    (b"{}", "no text value"),
    (b'{"home": 5}', "no text value"),
    (b'{"home": "  "}', "no text value"),
    (b'{"home": "relative/registry"}', "absolute"),
    (b"\xff\xfe", "UTF-8"),
])
def test_invalid_sidecar_never_falls_back(tmp_path, content, problem):
    manifest = _manifest(tmp_path)
    sidecar = manifest.parent / "compound_registry.config.json"
    sidecar.write_bytes(content)
    c = resolve_home([str(manifest), protocol.EXTENSION_ID])
    assert c.source == "host-config" and c.home is None and c.log_dir is None
    assert c.error is not None and c.error.code == "config_invalid"
    assert str(sidecar) in c.error.message and problem in c.error.message


def test_unreadable_sidecar(tmp_path):
    manifest = _manifest(tmp_path)
    (manifest.parent / "compound_registry.config.json").mkdir()   # a folder cannot be read
    c = resolve_home([str(manifest)])
    assert c.error is not None and "cannot be read" in c.error.message


def test_sidecar_with_bom_is_accepted(tmp_path):
    manifest = _manifest(tmp_path)
    target = tmp_path / "bom"
    (manifest.parent / "compound_registry.config.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"home": str(target)}).encode())
    assert resolve_home([str(manifest)]).home == target


# --------------------------------------------------------------------------- the host never creates a registry

def test_missing_registry_is_never_created(tmp_path):
    missing = tmp_path / "Registry"
    choice = HomeChoice(missing, "host-config")
    for op, params in (("ping", {}), ("analyze", {"smiles": ETHANOL}),
                       ("retire", {"id": "000001_01_001", "reason": "x"})):
        e = error(call(choice, op, params), "registry_missing")
        assert str(missing) in e["message"] and "never creates a registry" in e["message"]
        assert "install-host --create-registry" in e["message"]
    e = error(call(choice, "register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                        "product_index": 1,
                                        "confirm": {"inchikey": "x", "stereo_mode": "none",
                                                    "key_digest": "0" * 64}}),
              "registry_missing")
    assert not missing.exists()


def test_env_or_default_registry_missing(tmp_path):
    e = error(call(HomeChoice(None, "env"), "ping"), "registry_missing")
    assert str(tmp_path / "env-home") in e["message"]
    assert not (tmp_path / "env-home").exists()


def test_config_without_database_is_missing(tmp_path):
    home = tmp_path / "half"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")
    e = error(call(HomeChoice(home, "host-config"), "ping"), "registry_missing")
    assert "registry.sqlite" in e["message"]
    assert not (home / "registry.sqlite").exists()


def test_database_without_config_is_missing(tmp_path, home):
    (home / "config.toml").unlink()
    error(call(HomeChoice(home, "host-config"), "ping"), "registry_missing")
    assert not (home / "config.toml").exists()


def test_moved_registry_does_not_reissue_ids(tmp_path, home):
    """Reviewer's proof, now asserting the safe behaviour."""
    import shutil

    choice = HomeChoice(home, "host-config")
    first = ok(register(choice, ETHANOL, ref="E1"))["id"]
    shutil.move(str(home), str(tmp_path / "Registry-moved"))
    error(call(choice, "ping"), "registry_missing")
    doc = call(choice, "register", {"smiles": PROPANOL, "experiment_ref": "E2",
                                    "product_index": 1,
                                    "confirm": {"inchikey": "x", "stereo_mode": "none",
                                                "key_digest": "0" * 64}})
    error(doc, "registry_missing")
    assert not home.exists()
    assert first == "000001_01_001"


def test_logging_never_creates_the_home(tmp_path):
    log = host._setup_logging(tmp_path / "absent-home" / "logs")
    try:
        assert not (tmp_path / "absent-home").exists()
        assert all(isinstance(h, logging.NullHandler) for h in log.handlers)
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
        log.propagate = True


def test_logging_inside_an_existing_home(home):
    log = host._setup_logging(home / "logs")
    try:
        log.info("hello")
        assert (home / "logs" / host.LOG_FILENAME).is_file()
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)
        log.propagate = True


# --------------------------------------------------------------------------- registry_changed

def test_registry_changed_answers_and_stops(tmp_path, home):
    other = create_registry(tmp_path / "other")
    started = HomeChoice(home, "host-config")
    choices = iter([started, HomeChoice(other, "host-config")])
    opened = []
    code, docs = run(started,
                     {"id": "a", "v": 1, "op": "ping"},
                     {"id": "b", "v": 1, "op": "analyze", "params": {"smiles": ETHANOL}},
                     {"id": "c", "v": 1, "op": "ping"},
                     resolve=lambda: next(choices),
                     open_service=lambda h: opened.append(h) or RegistryService.open(h))
    assert code == 0 and [d["id"] for d in docs] == ["a", "b"]
    ok(docs[0])
    e = error(docs[1], "registry_changed")
    assert str(home) in e["message"] and str(other) in e["message"]
    assert "Nothing was written" in e["message"]
    assert opened == [home]   # the changed request never reached a service


@pytest.mark.parametrize("new", [
    HomeChoice(None, "env"),
    HomeChoice(None, "host-config", error=ConfigError("broken sidecar")),
])
def test_any_change_of_source_or_error_state_counts(home, new):
    code, docs = run(HomeChoice(home, "host-config"), {"id": 1, "v": 1, "op": "ping"},
                     resolve=lambda: new)
    assert code == 0 and docs[0]["id"] == 1
    error(docs[0], "registry_changed")


def test_unchanged_configuration_keeps_serving(home):
    same = HomeChoice(home, "host-config")
    code, docs = run(same, *({"id": i, "v": 1, "op": "ping"} for i in range(3)),
                     resolve=lambda: HomeChoice(Path(str(home)), "host-config",
                                                log_dir=home / "logs"))
    assert code == 0 and all(d["ok"] for d in docs)


def test_registry_changed_on_an_unreadable_frame_has_id_null(home, tmp_path):
    code, docs = run(HomeChoice(home, "host-config"), frame(b"{bad"),
                     resolve=lambda: HomeChoice(tmp_path / "x", "host-config"))
    assert code == 0 and docs[0]["id"] is None
    error(docs[0], "registry_changed")


# --------------------------------------------------------------------------- Chrome as caller

class FakeRegistry:
    """Read-only stand-in for HKCU (the host only reads)."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def read_default(self, key):
        return self.values.get(key)

    def write_default(self, key, value):
        pytest.fail("the host must not write to the registry")

    def delete_key(self, key):
        pytest.fail("the host must not write to the registry")


@pytest.fixture(autouse=True)
def _no_real_target(monkeypatch):
    """The real default target (real HKCU, real profile folders) is never used."""
    monkeypatch.setattr(install_host, "default_target",
                        lambda **kw: pytest.fail("the real default target was used"))


def _target(platform, tmp_path):
    """A machine (in tmp_path) where nothing is registered yet."""
    return _REAL_DEFAULT_TARGET(platform=platform, prefix=tmp_path / "venv",
                                user_home=tmp_path / "user", registry=FakeRegistry())


@pytest.fixture(params=["windows", "linux"])
def chrome_target(request, tmp_path):
    return _target(request.param, tmp_path)


def register_chrome(target, sidecar_home=None, *, firefox=False) -> None:
    """Write the files (and keys) the installer writes, without the installer."""
    from compound_registry.install_host import (CHROME_REGISTRY_KEY, REGISTRY_KEY,
                                                chrome_manifest_document, manifest_document)

    target.chrome_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    target.chrome_manifest_path.write_text(
        json.dumps(chrome_manifest_document(target.launcher)), encoding="utf-8")
    if target.platform == "windows":
        target.registry.values[CHROME_REGISTRY_KEY] = str(target.chrome_manifest_path)
    if firefox:
        target.manifest_dir.mkdir(parents=True, exist_ok=True)
        target.manifest_path.write_text(json.dumps(manifest_document(target.launcher)),
                                        encoding="utf-8")
        if target.platform == "windows":
            target.registry.values[REGISTRY_KEY] = str(target.manifest_path)
    if sidecar_home is not None:
        target.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        target.sidecar_path.write_text(json.dumps({"home": str(sidecar_home)}), encoding="utf-8")


@pytest.mark.parametrize("argv_tail", [[], ["--parent-window=0"], ["--parent-window=132456"]])
def test_chrome_uses_the_shared_host_config(chrome_target, tmp_path, argv_tail):
    chosen = tmp_path / "chosen"
    register_chrome(chrome_target, chosen, firefox=True)
    c = resolve_home([protocol.CHROME_ORIGIN, *argv_tail], target=chrome_target)
    assert c == HomeChoice(chosen, "host-config", log_dir=chosen / "logs")
    # exactly what Firefox gets from the same installation
    assert resolve_home([str(chrome_target.manifest_path), protocol.EXTENSION_ID]) == c


def test_chrome_alone_registered(chrome_target, tmp_path):
    chosen = tmp_path / "chosen"
    register_chrome(chrome_target, chosen)
    assert resolve_home([protocol.CHROME_ORIGIN], target=chrome_target).home == chosen


@pytest.mark.parametrize("env_value", ["", "ENV"])
def test_chrome_registration_without_host_config_is_refused(chrome_target, tmp_path, monkeypatch,
                                                            env_value):
    """Unpinned, Firefox and Chrome would each use $COMPOUND_REGISTRY_HOME as
    their OWN process sees it; this process cannot compare, so it refuses."""
    if env_value:
        monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / env_value))
    else:
        monkeypatch.delenv("COMPOUND_REGISTRY_HOME", raising=False)
    for firefox in (False, True):
        register_chrome(chrome_target, None, firefox=firefox)
        c = resolve_home([protocol.CHROME_ORIGIN], target=chrome_target)
        assert c.home is None and c.log_dir is None and c.error.code == "config_invalid"
        assert "not pinned" in c.error.message and "--browser chrome" in c.error.message
        error(call(c, "ping"), "config_invalid")
    # Firefox itself still uses the environment (unchanged behaviour)
    assert resolve_home([str(chrome_target.manifest_path), protocol.EXTENSION_ID]).error is None


def test_chrome_without_registration_never_guesses(chrome_target, tmp_path):
    # a host configuration exists, but nothing tells Chrome's host to use it
    chrome_target.sidecar_path.parent.mkdir(parents=True)
    chrome_target.sidecar_path.write_text(json.dumps({"home": str(tmp_path / "x")}),
                                          encoding="utf-8")
    c = resolve_home([protocol.CHROME_ORIGIN, "--parent-window=0"], target=chrome_target)
    assert c.home is None and c.log_dir is None and c.error.code == "config_invalid"
    assert "install-host --browser chrome" in c.error.message
    error(call(c, "ping"), "config_invalid")
    assert not (tmp_path / "env-home").exists()


def test_chrome_registry_value_without_manifest(tmp_path):
    from compound_registry.install_host import CHROME_REGISTRY_KEY

    t = _target("windows", tmp_path)
    t.registry.values[CHROME_REGISTRY_KEY] = str(tmp_path / "gone" / "compound_registry.chrome.json")
    c = resolve_home([protocol.CHROME_ORIGIN], target=t)
    assert c.error is not None and "gone" in c.error.message


def test_chrome_unreadable_registry_never_guesses(tmp_path):
    t = _target("windows", tmp_path)

    def broken(key):
        raise PermissionError(13, "Access is denied")

    t.registry.read_default = broken
    c = resolve_home([protocol.CHROME_ORIGIN], target=t)
    assert c.error is not None and c.error.code == "config_invalid"


def test_chrome_refuses_when_only_firefox_registration_is_unreadable(tmp_path):
    """Unknown is not "Firefox not registered": Firefox may be pinned elsewhere."""
    from compound_registry.install_host import REGISTRY_KEY

    t = _target("windows", tmp_path)
    register_chrome(t, tmp_path / "B")              # Chrome pinned to B, readable
    real = t.registry.read_default

    def firefox_key_broken(key):
        if key == REGISTRY_KEY:
            raise PermissionError(13, "Access is denied")
        return real(key)

    t.registry.read_default = firefox_key_broken
    c = resolve_home([protocol.CHROME_ORIGIN], target=t)
    assert c.home is None and c.error is not None and c.error.code == "config_invalid"
    assert "Firefox's registration of it cannot be read" in c.error.message
    t.registry.read_default = real                   # readable again, nothing registered: B
    assert resolve_home([protocol.CHROME_ORIGIN], target=t).home == tmp_path / "B"


def test_chrome_refuses_a_firefox_manifest_under_another_name(tmp_path, monkeypatch):
    """Firefox's host only reads the host configuration next to a manifest named
    compound_registry.json; with another name Firefox uses the environment."""
    from compound_registry.install_host import REGISTRY_KEY, firefox_host_config_path

    monkeypatch.setenv("COMPOUND_REGISTRY_HOME", str(tmp_path / "env-home"))
    t = _target("windows", tmp_path)
    register_chrome(t, tmp_path / "A")              # Chrome manifest + host config (A) in one folder
    other = t.manifest_dir / "other.json"
    other.write_text(json.dumps(install_host.manifest_document(t.launcher)), encoding="utf-8")
    t.registry.values[REGISTRY_KEY] = str(other)
    # Firefox, started with that manifest, does NOT use A ...
    assert firefox_host_config_path(other) is None
    fx = resolve_home([str(other), protocol.EXTENSION_ID])
    assert fx.home is None and fx.source == "env"
    # ... so Chrome must not serve A either
    c = resolve_home([protocol.CHROME_ORIGIN], target=t)
    assert c.home is None and c.error is not None and c.error.code == "config_invalid"
    assert "other.json" in c.error.message and "--browser both" in c.error.message
    # the regular name: both use A
    assert firefox_host_config_path(t.manifest_path) == t.sidecar_path
    t.manifest_path.write_text(json.dumps(install_host.manifest_document(t.launcher)),
                               encoding="utf-8")
    t.registry.values[REGISTRY_KEY] = str(t.manifest_path)
    assert resolve_home([protocol.CHROME_ORIGIN], target=t).home == tmp_path / "A"
    assert resolve_home([str(t.manifest_path), protocol.EXTENSION_ID]).home == tmp_path / "A"


def test_chrome_invalid_host_config_never_falls_back(chrome_target, tmp_path):
    register_chrome(chrome_target, None, firefox=True)
    chrome_target.sidecar_path.write_bytes(b'{"home": "relative"}')
    c = resolve_home([protocol.CHROME_ORIGIN], target=chrome_target)
    assert c.error is not None and c.error.code == "config_invalid" and c.home is None
    assert str(chrome_target.sidecar_path) in c.error.message


def test_chrome_and_firefox_naming_different_registries_is_refused(tmp_path):
    """Windows: a Chrome key pointing to another folder with another host configuration."""
    from compound_registry.install_host import CHROME_REGISTRY_KEY, SIDECAR_NAME

    t = _target("windows", tmp_path)
    register_chrome(t, tmp_path / "A", firefox=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "compound_registry.chrome.json").write_text("{}", encoding="utf-8")
    t.registry.values[CHROME_REGISTRY_KEY] = str(elsewhere / "compound_registry.chrome.json")
    (elsewhere / SIDECAR_NAME).write_text(json.dumps({"home": str(tmp_path / "B")}),
                                          encoding="utf-8")
    c = resolve_home([protocol.CHROME_ORIGIN], target=t)
    assert c.error is not None and "DIFFERENT" in c.error.message and c.home is None
    # the same registry in both host configurations is fine
    (elsewhere / SIDECAR_NAME).write_text(json.dumps({"home": str(tmp_path / "A")}),
                                          encoding="utf-8")
    assert resolve_home([protocol.CHROME_ORIGIN], target=t).home == tmp_path / "A"


def test_another_chrome_origin_resolves_the_same_way(chrome_target, tmp_path):
    """The caller check is main's job (forbidden_caller); the home lookup is
    the same, so the refusal is logged in the configured registry."""
    register_chrome(chrome_target, tmp_path / "chosen")
    other = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
    assert resolve_home([other], target=chrome_target).home == tmp_path / "chosen"
    assert host.is_chrome_caller([other]) and not host.is_chrome_caller(["x.json", "id"])
    assert host._caller([protocol.CHROME_ORIGIN]) == "chrome"
    assert host._caller(["m.json", protocol.EXTENSION_ID]) == "firefox"
    assert host._caller([]) == "manual"


def test_firefox_resolution_never_looks_at_chrome(tmp_path, monkeypatch):
    from compound_registry import install_host

    monkeypatch.setattr(install_host, "default_target",
                        lambda **kw: pytest.fail("Firefox must not look up Chrome"))
    manifest = _manifest(tmp_path)
    target = tmp_path / "chosen"
    (manifest.parent / "compound_registry.config.json").write_text(
        json.dumps({"home": str(target)}), encoding="utf-8")
    assert resolve_home([str(manifest), protocol.EXTENSION_ID]) == \
        HomeChoice(target, "host-config", log_dir=target / "logs")
    assert resolve_home([]).source == "env"
