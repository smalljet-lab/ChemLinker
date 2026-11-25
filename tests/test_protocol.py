"""Tests for compound_registry.protocol: parse_request accept/reject table, is_safe_url."""

from __future__ import annotations

import copy

import pytest

from compound_registry import protocol
from compound_registry.protocol import (
    MAX_ID_TEXT,
    MAX_IGNORED_ITEM_TEXT,
    MAX_IGNORED_ITEMS,
    MAX_PRODUCT_INDEX,
    MAX_REASON_TEXT,
    MAX_REF_TEXT,
    MAX_STRUCTURE_CHARS,
    MAX_URL_TEXT,
    ProtocolError,
    Request,
    error_response,
    is_safe_url,
    ok_response,
    parse_request,
)

CONFIRM = {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N", "stereo_mode": "none",
           "key_digest": "0" * 64}

BASE = {
    "ping": {"id": 1, "v": 1, "op": "ping", "params": {}},
    "analyze": {"id": "a-1", "v": 1, "op": "analyze", "params": {"smiles": "CCO"}},
    "register": {"id": 2, "v": 1, "op": "register",
                 "params": {"smiles": "CCO", "experiment_ref": "4180", "product_index": 1,
                            "confirm": dict(CONFIRM)}},
    "retire": {"id": 3, "v": 1, "op": "retire",
               "params": {"id": "000001_01_001", "reason": "wrong structure"}},
}


def req(op: str, *, drop: tuple[str, ...] = (), top: dict | None = None, **params) -> dict:
    """A copy of BASE[op] with params changed (None removes nothing; use drop)."""
    message = copy.deepcopy(BASE[op])
    for key in drop:
        message["params"].pop(key, None)
    message["params"].update(params)
    if top:
        message.update(top)
    return message


def rejected(message, code: str = "bad_request") -> ProtocolError:
    with pytest.raises(ProtocolError) as info:
        parse_request(message)
    assert info.value.code == code, info.value.message
    return info.value


# --------------------------------------------------------------------------- accepted

def test_ping_minimal():
    r = parse_request(BASE["ping"])
    assert r == Request(id=1, op="ping", params={})


def test_ping_without_params_key():
    assert parse_request({"id": 1, "v": 1, "op": "ping"}).params == {}


def test_analyze_defaults_are_none():
    r = parse_request(BASE["analyze"])
    assert r.params == {"molfile": None, "smiles": "CCO", "mode": None,
                        "experiment_ref": None, "product_index": None}


def test_analyze_smiles_is_stripped_and_blank_ref_is_none():
    r = parse_request(req("analyze", smiles="  CCO  ", experiment_ref="   "))
    assert r.params["smiles"] == "CCO" and r.params["experiment_ref"] is None


def test_analyze_molfile_keeps_newlines():
    mol = "\n  RDKit          2D\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\r\nM  END\n"
    r = parse_request(req("analyze", drop=("smiles",), molfile=mol, mode="rac",
                          experiment_ref="E1", product_index=3))
    assert r.params["molfile"] == mol and r.params["mode"] == "rac"
    assert r.params["product_index"] == 3


def test_register_full():
    r = parse_request(req("register", experiment_code="ABC-008", mode="abs",
                          experiment_url="https://eln.example.org/exp?id=4180",
                          accept_ignored_stereo=["a", "b"]))
    assert r.op == "register"
    assert r.params == {
        "molfile": None, "smiles": "CCO", "mode": "abs", "experiment_ref": "4180",
        "experiment_code": "ABC-008", "experiment_url": "https://eln.example.org/exp?id=4180",
        "product_index": 1, "confirm": CONFIRM, "accept_ignored_stereo": ["a", "b"],
    }


def test_register_optional_defaults():
    p = parse_request(BASE["register"]).params
    assert p["experiment_code"] is None and p["experiment_url"] is None
    assert p["accept_ignored_stereo"] == [] and p["mode"] is None


def test_register_blank_optional_texts_become_none():
    p = parse_request(req("register", experiment_code=" ", experiment_url="")).params
    assert p["experiment_code"] is None and p["experiment_url"] is None


def test_register_non_ascii_text_is_fine():
    p = parse_request(req("register", experiment_code="José-001 µ")).params
    assert p["experiment_code"] == "José-001 µ"


def test_retire():
    r = parse_request(BASE["retire"])
    assert r.params == {"id": "000001_01_001", "reason": "wrong structure"}


def test_retire_reason_may_span_lines():
    assert parse_request(req("retire", reason="line 1\nline 2\ttab")).params["reason"]


@pytest.mark.parametrize("request_id", [0, 7, 2**53, "x", "a" * MAX_ID_TEXT, "req-ü"])
def test_accepted_ids(request_id):
    assert parse_request(req("ping", top={"id": request_id})).id == request_id


@pytest.mark.parametrize("value", [1, MAX_PRODUCT_INDEX])
def test_product_index_bounds_accepted(value):
    assert parse_request(req("register", product_index=value)).params["product_index"] == value


def test_limits_exactly_at_the_maximum_are_accepted():
    parse_request(req("analyze", smiles="C" * MAX_STRUCTURE_CHARS))
    parse_request(req("register", experiment_ref="r" * MAX_REF_TEXT,
                      experiment_code="c" * MAX_REF_TEXT,
                      accept_ignored_stereo=["x" * MAX_IGNORED_ITEM_TEXT] * MAX_IGNORED_ITEMS))
    parse_request(req("retire", reason="r" * MAX_REASON_TEXT))
    url = "https://e.org/" + "a" * (MAX_URL_TEXT - len("https://e.org/"))
    parse_request(req("register", experiment_url=url))


# --------------------------------------------------------------------------- version

@pytest.mark.parametrize("version", [2, 0, "1", None, True, 1.5])
def test_other_versions_are_a_mismatch(version):
    message = req("analyze", top={"v": version})
    err = rejected(message, "protocol_mismatch")
    assert err.request_id == "a-1"


def test_missing_version_is_a_mismatch():
    message = copy.deepcopy(BASE["retire"])
    del message["v"]
    rejected(message, "protocol_mismatch")


@pytest.mark.parametrize("version", [2, None, "x", True])
def test_ping_is_exempt_from_the_version_rule(version):
    assert parse_request(req("ping", top={"v": version})).op == "ping"
    message = copy.deepcopy(BASE["ping"])
    del message["v"]
    assert parse_request(message).op == "ping"


def test_float_one_counts_as_version_one():
    # JSON 1.0 decodes to the float 1.0, which equals PROTOCOL_VERSION.
    assert parse_request(req("analyze", top={"v": 1.0})).op == "analyze"


# --------------------------------------------------------------------------- envelope rejects

@pytest.mark.parametrize("message", [None, [], "ping", 1, [BASE["ping"]]])
def test_not_an_object(message):
    err = rejected(message)
    assert err.request_id is None


@pytest.mark.parametrize("request_id", [None, -1, 2**53 + 1, True, False, "", "a" * (MAX_ID_TEXT + 1),
                                        "a\nb", "\x00", 1.5, [], {}])
def test_bad_ids(request_id):
    err = rejected(req("ping", top={"id": request_id}))
    assert err.request_id is None


def test_missing_id():
    message = copy.deepcopy(BASE["ping"])
    del message["id"]
    assert rejected(message).request_id is None


def test_unknown_top_level_field():
    err = rejected(req("ping", top={"extra": 1}))
    assert err.request_id == 1 and "extra" in err.message


@pytest.mark.parametrize("op", ["", "PING", "delete", None, 5, "x" * 100])
def test_unknown_op(op):
    err = rejected(req("ping", top={"op": op}), "unknown_op")
    assert err.request_id == 1


def test_unknown_op_name_is_not_echoed_when_long():
    err = rejected(req("ping", top={"op": "y" * 100}), "unknown_op")
    assert "y" * 100 not in err.message


@pytest.mark.parametrize("params", [[], "x", 1, None])  # explicit null is not an object either
def test_params_must_be_an_object(params):
    assert rejected(req("ping", top={"params": params})).request_id == 1


def test_tab_is_a_control_character_in_single_line_text():
    rejected(req("analyze", smiles="CCO\t"))


def test_ping_refuses_params():
    rejected(req("ping", anything=1))


# --------------------------------------------------------------------------- params rejects

STRUCTURE_REJECTS = [
    ("both", {"molfile": "M  END", "smiles": "CCO"}),
    ("neither", {"smiles": None}),
    ("blank smiles", {"smiles": "   "}),
    ("smiles not text", {"smiles": 5}),
    ("smiles with newline", {"smiles": "CC\nO"}),
    ("smiles with control", {"smiles": "CC\x07O"}),
    ("smiles too long", {"smiles": "C" * (MAX_STRUCTURE_CHARS + 1)}),
    ("molfile too long", {"smiles": None, "molfile": "M" * (MAX_STRUCTURE_CHARS + 1)}),
    ("molfile with NUL", {"smiles": None, "molfile": "abc\x00\nM  END"}),
    ("molfile not text", {"smiles": None, "molfile": ["M  END"]}),
]


@pytest.mark.parametrize("op", ["analyze", "register"])
@pytest.mark.parametrize("label,changes", STRUCTURE_REJECTS, ids=[c[0] for c in STRUCTURE_REJECTS])
def test_structure_rejects(op, label, changes):
    message = req(op)
    for key, value in changes.items():
        if value is None:
            message["params"].pop(key, None)
        else:
            message["params"][key] = value
    err = rejected(message)
    assert err.request_id == message["id"]


ANALYZE_REJECTS = [
    ("mode not text", {"mode": 1}),
    ("mode too long", {"mode": "a" * (MAX_ID_TEXT + 1)}),
    ("ref not text", {"experiment_ref": 4180}),
    ("ref too long", {"experiment_ref": "r" * (MAX_REF_TEXT + 1)}),
    ("index zero", {"product_index": 0}),
    ("index too big", {"product_index": MAX_PRODUCT_INDEX + 1}),
    ("index bool", {"product_index": True}),
    ("index float", {"product_index": 1.0}),
    ("index text", {"product_index": "1"}),
    ("unknown key", {"confirm": dict(CONFIRM)}),
    ("unknown key 2", {"experiment_code": "X"}),
]


@pytest.mark.parametrize("label,changes", ANALYZE_REJECTS, ids=[c[0] for c in ANALYZE_REJECTS])
def test_analyze_rejects(label, changes):
    rejected(req("analyze", **changes))


REGISTER_REJECTS = [
    ("no ref", {"experiment_ref": None}),
    ("blank ref", {"experiment_ref": "  "}),
    ("no index", {"product_index": None}),
    ("negative index", {"product_index": -1}),
    ("no confirm", {"confirm": None}),
    ("confirm not object", {"confirm": "XYZ"}),
    ("confirm missing key", {"confirm": {"inchikey": "X"}}),
    ("confirm without digest", {"confirm": {"inchikey": "X", "stereo_mode": "abs"}}),
    ("confirm extra key", {"confirm": dict(CONFIRM, smiles="CCO")}),
    ("confirm empty key", {"confirm": dict(CONFIRM, inchikey="")}),
    ("confirm not text", {"confirm": dict(CONFIRM, inchikey=1)}),
    ("confirm too long", {"confirm": dict(CONFIRM, inchikey="K" * (MAX_REF_TEXT + 1))}),
    ("code too long", {"experiment_code": "c" * (MAX_REF_TEXT + 1)}),
    ("code with newline", {"experiment_code": "ABC\n008"}),
    ("url javascript", {"experiment_url": "javascript:alert(1)"}),
    ("url data", {"experiment_url": "data:text/html,<b>x</b>"}),
    ("url relative", {"experiment_url": "/exp/4180"}),
    ("url ftp", {"experiment_url": "ftp://eln.example.org/x"}),
    ("url no host", {"experiment_url": "https:///x"}),
    ("url with space", {"experiment_url": "https://eln.example.org/a b"}),
    ("url too long", {"experiment_url": "https://e.org/" + "a" * MAX_URL_TEXT}),
    ("url not text", {"experiment_url": 5}),
    ("ignored not list", {"accept_ignored_stereo": "x"}),
    ("ignored item not text", {"accept_ignored_stereo": ["x", 1]}),
    ("ignored too many", {"accept_ignored_stereo": ["x"] * (MAX_IGNORED_ITEMS + 1)}),
    ("ignored item too long", {"accept_ignored_stereo": ["x" * (MAX_IGNORED_ITEM_TEXT + 1)]}),
    ("unknown key", {"source": "cli"}),
    ("with_svg not allowed", {"with_svg": True}),
]


@pytest.mark.parametrize("label,changes", REGISTER_REJECTS, ids=[c[0] for c in REGISTER_REJECTS])
def test_register_rejects(label, changes):
    message = req("register")
    for key, value in changes.items():
        if value is None:
            message["params"].pop(key, None)
        else:
            message["params"][key] = value
    err = rejected(message)
    assert err.request_id == 2


RETIRE_REJECTS = [
    ("no id", {"id": None}),
    ("blank id", {"id": " "}),
    ("id not text", {"id": 1}),
    ("id too long", {"id": "0" * (MAX_REF_TEXT + 1)}),
    ("no reason", {"reason": None}),
    ("blank reason", {"reason": " \n "}),
    ("reason too long", {"reason": "r" * (MAX_REASON_TEXT + 1)}),
    ("reason with control", {"reason": "bad\x1bthing"}),
    ("unknown key", {"yes": True}),
]


@pytest.mark.parametrize("label,changes", RETIRE_REJECTS, ids=[c[0] for c in RETIRE_REJECTS])
def test_retire_rejects(label, changes):
    message = req("retire")
    for key, value in changes.items():
        if value is None:
            message["params"].pop(key, None)
        else:
            message["params"][key] = value
    assert rejected(message).request_id == 3


def test_params_null_value_counts_as_absent():
    # An explicit null for an optional key is the same as leaving it out.
    p = parse_request(req("analyze", mode=None, experiment_ref=None)).params
    assert p["mode"] is None


# --------------------------------------------------------------------------- responses / constants

def test_response_helpers():
    assert ok_response(1, {"a": 1}) == {"id": 1, "ok": True, "result": {"a": 1}}
    assert error_response(None, {"code": "x", "message": "y"}) == {
        "id": None, "ok": False, "error": {"code": "x", "message": "y"}}


def test_protocol_error_to_dict():
    err = ProtocolError("m", code="unknown_op", request_id=4)
    assert err.to_dict() == {"code": "unknown_op", "message": "m"} and err.request_id == 4


def test_constants_are_consistent():
    assert protocol.MAX_RESPONSE_BYTES < 1024 * 1024
    assert set(protocol.OPS) == {"ping", "analyze", "register", "retire", "find", "compounds",
                                 "set_name", "generate_name", "set_yield", "yields",
                                 "compound_files", "read_pdf", "set_reaction", "route",
                                 "set_limiting", "recognition_status", "recognize",
                                 "describe_structures", "reaction_to_chemdraw"}
    assert "files_folder" in protocol.PING_RESULT_KEYS
    assert protocol.YIELD_KINDS == ("isolated", "analytical")
    assert 1 <= protocol.DEFAULT_YIELD_PAGE_SIZE <= protocol.MAX_YIELD_PAGE_SIZE
    assert protocol.MAX_YIELD_PERCENT >= 100
    assert protocol.NAME_SOURCES == ("user", "chemdraw")
    assert "name_generator" in protocol.PING_RESULT_KEYS
    assert protocol.NAME_GENERATION_TIMEOUT_S > 0
    assert set(protocol.OPS) == set(protocol._PARAM_READERS)
    assert protocol.HOME_SOURCES == ("host-config", "env", "default")
    assert "confirmation_mismatch" in protocol.HOST_ERROR_CODES
    assert protocol.MATCH_MODES == ("substructure", "exact")
    assert protocol.ORDERS == ("asc", "desc")
    assert protocol.MATCH_REASONS == ("id", "inchikey", "structure", "name", "formula",
                                      "experiment", "drawn")
    assert 1 <= protocol.DEFAULT_PAGE_SIZE <= protocol.MAX_PAGE_SIZE
    assert protocol.MAX_ITEM_EXPERIMENTS >= 1
    assert protocol.MAX_NAME_TEXT <= protocol.MAX_SEARCH_TEXT < protocol.MAX_STRUCTURE_CHARS
    assert set(protocol.__all__) <= set(dir(protocol))


# --------------------------------------------------------------------------- compounds

def compounds(**params) -> dict:
    return {"id": "c-1", "v": 1, "op": "compounds", "params": params}


def test_compounds_defaults():
    r = parse_request(compounds())
    assert r.op == "compounds"
    assert r.params == {"text": None, "molfile": None, "smiles": None, "match": "substructure",
                        "order": "asc", "offset": 0, "limit": protocol.DEFAULT_PAGE_SIZE}


def test_compounds_full():
    mol = "\n  x\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"
    r = parse_request(compounds(text="  ABC-008 ", molfile=mol, match="exact", order="desc",
                                offset=48, limit=protocol.MAX_PAGE_SIZE))
    assert r.params == {"text": "ABC-008", "molfile": mol, "smiles": None, "match": "exact",
                        "order": "desc", "offset": 48, "limit": protocol.MAX_PAGE_SIZE}
    p = parse_request(compounds(smiles=" c1ccccc1 ")).params
    assert p["smiles"] == "c1ccccc1" and p["molfile"] is None


def test_compounds_blank_values_are_absent():
    p = parse_request(compounds(text="  ", smiles="", molfile=" \n ", match=None, order=None,
                                offset=None, limit=None)).params
    assert (p["text"], p["smiles"], p["molfile"]) == (None, None, None)
    assert (p["match"], p["order"], p["offset"], p["limit"]) == (
        "substructure", "asc", 0, protocol.DEFAULT_PAGE_SIZE)


def test_compounds_limits_at_the_maximum_are_accepted():
    parse_request(compounds(text="t" * protocol.MAX_SEARCH_TEXT,
                            smiles="C" * MAX_STRUCTURE_CHARS, offset=protocol.MAX_OFFSET,
                            limit=1))


COMPOUNDS_REJECTS = [
    ("both structures", {"smiles": "CCO", "molfile": "M  END"}),
    ("text too long", {"text": "t" * (protocol.MAX_SEARCH_TEXT + 1)}),
    ("text with newline", {"text": "a\nb"}),
    ("text with tab", {"text": "a\tb"}),
    ("text not text", {"text": 5}),
    ("smiles with newline", {"smiles": "CC\nO"}),
    ("molfile too long", {"molfile": "M" * (MAX_STRUCTURE_CHARS + 1)}),
    ("molfile with NUL", {"molfile": "x\x00\nM  END"}),
    ("unknown match", {"match": "similarity"}),
    ("match not text", {"match": 1}),
    ("unknown order", {"order": "random"}),
    ("negative offset", {"offset": -1}),
    ("offset too big", {"offset": protocol.MAX_OFFSET + 1}),
    ("offset float", {"offset": 1.0}),
    ("offset bool", {"offset": True}),
    ("limit zero", {"limit": 0}),
    ("limit too big", {"limit": protocol.MAX_PAGE_SIZE + 1}),
    ("limit text", {"limit": "10"}),
    ("unknown key", {"with_svg": False}),
    ("unknown key 2", {"id": "000001"}),
]


@pytest.mark.parametrize("label,changes", COMPOUNDS_REJECTS, ids=[c[0] for c in COMPOUNDS_REJECTS])
def test_compounds_rejects(label, changes):
    assert rejected(compounds(**changes)).request_id == "c-1"


# --------------------------------------------------------------------------- set_name

def set_name(**params) -> dict:
    return {"id": 9, "v": 1, "op": "set_name", "params": params}


def test_set_name():
    r = parse_request(set_name(core_stereo_id=" 000042_01 ", name="  (2S)-butan-2-ol "))
    assert r.params == {"core_stereo_id": "000042_01", "name": "(2S)-butan-2-ol"}


def test_set_name_null_removes():
    assert parse_request(set_name(core_stereo_id="000042_01", name=None)).params["name"] is None


def test_set_name_unicode_and_maximum_length():
    long = "ä" * protocol.MAX_NAME_TEXT
    assert parse_request(set_name(core_stereo_id="000042_01", name=long)).params["name"] == long
    text = "(1R,2S)-2-(méthyl)-1′-ol"
    assert parse_request(set_name(core_stereo_id="000042_01", name=text)).params["name"] == text


SET_NAME_REJECTS = [
    ("no name key", {"core_stereo_id": "000042_01"}),
    ("no id", {"name": "x"}),
    ("blank id", {"core_stereo_id": " ", "name": "x"}),
    ("id too long", {"core_stereo_id": "0" * (MAX_REF_TEXT + 1), "name": "x"}),
    ("blank name", {"core_stereo_id": "000042_01", "name": "   "}),
    ("empty name", {"core_stereo_id": "000042_01", "name": ""}),
    ("name with newline", {"core_stereo_id": "000042_01", "name": "a\nb"}),
    ("name with control", {"core_stereo_id": "000042_01", "name": "a\x1bb"}),
    ("name too long", {"core_stereo_id": "000042_01", "name": "n" * (protocol.MAX_NAME_TEXT + 1)}),
    ("name not text", {"core_stereo_id": "000042_01", "name": 5}),
    ("name list", {"core_stereo_id": "000042_01", "name": ["x"]}),
    ("unknown key", {"core_stereo_id": "000042_01", "name": "x", "remove": True}),
]


@pytest.mark.parametrize("label,params", SET_NAME_REJECTS, ids=[c[0] for c in SET_NAME_REJECTS])
def test_set_name_rejects(label, params):
    assert rejected(set_name(**params)).request_id == 9


# --------------------------------------------------------------------------- generate_name

def generate_name(**params) -> dict:
    return {"id": "g-1", "v": 1, "op": "generate_name", "params": params}


def test_generate_name_defaults_and_replace():
    r = parse_request(generate_name(core_stereo_id=" 000042_01 "))
    assert r.op == "generate_name"
    assert r.params == {"core_stereo_id": "000042_01", "replace": False}
    for value in (True, False):
        p = parse_request(generate_name(core_stereo_id="000042_01", replace=value)).params
        assert p["replace"] is value
    assert parse_request(generate_name(core_stereo_id="000042_01",
                                       replace=None)).params["replace"] is False


GENERATE_NAME_REJECTS = [
    ("no id", {}),
    ("id null", {"core_stereo_id": None}),
    ("blank id", {"core_stereo_id": "  "}),
    ("id not text", {"core_stereo_id": 42}),
    ("id too long", {"core_stereo_id": "0" * (MAX_REF_TEXT + 1)}),
    ("replace text", {"core_stereo_id": "000042_01", "replace": "true"}),
    ("replace number", {"core_stereo_id": "000042_01", "replace": 1}),
    ("replace list", {"core_stereo_id": "000042_01", "replace": [True]}),
    ("unknown key", {"core_stereo_id": "000042_01", "name": "x"}),
    ("unknown key 2", {"core_stereo_id": "000042_01", "timeout": 5}),
]


@pytest.mark.parametrize("label,params", GENERATE_NAME_REJECTS,
                         ids=[c[0] for c in GENERATE_NAME_REJECTS])
def test_generate_name_rejects(label, params):
    error = rejected(generate_name(**params))
    assert error.request_id == "g-1" and error.message.startswith("generate_name: ")


def test_generate_name_needs_the_protocol_version():
    message = generate_name(core_stereo_id="000042_01")
    message["v"] = 2
    rejected(message, "protocol_mismatch")


# --------------------------------------------------------------------------- is_safe_url

@pytest.mark.parametrize("url", [
    "https://eln.example.org/experiment?id=4180",
    "http://localhost:8080/x",
    "HTTPS://ELN.EXAMPLE.ORG/",
    "https://192.168.1.10/a#frag",
])
def test_safe_urls(url):
    assert is_safe_url(url)


@pytest.mark.parametrize("url", [
    None, 5, "", "javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x",
    "file:///C:/x", "ftp://host/x", "//host/path", "/relative", "relative",
    "https://", "https:///path", "https://host/a b", "https://host/\x00",
    "https://host/\x7f", " https://host/", "https://host/\n", "https://[::1",
    "https://e.org/" + "a" * MAX_URL_TEXT,
])
def test_unsafe_urls(url):
    assert not is_safe_url(url)


# --------------------------------------------------------------------------- capture reaction

MOLFILE = "\n  invented\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n" \
          "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n"


def capture(op: str, **params) -> dict:
    return {"id": "r-1", "v": 1, "op": op, "params": params}


def test_capture_constants():
    assert protocol.CAPTURE_OPS == ("recognition_status", "recognize", "describe_structures",
                                    "reaction_to_chemdraw")
    assert set(protocol.CAPTURE_OPS) <= set(protocol.OPS)
    # The largest picture, as base64 in a request, fits one frame with room to spare.
    assert protocol.MAX_IMAGE_BASE64_CHARS + 1000 < protocol.MAX_REQUEST_BYTES
    assert protocol.MAX_IMAGE_BASE64_CHARS % 4 == 0
    assert 1 <= protocol.MIN_IMAGE_SIDE < protocol.MAX_IMAGE_SIDE
    assert protocol.RECOGNITION_MODES == ("reaction", "molecule")
    assert protocol.RECOGNITION_STATES == ("not_installed", "stopped", "loading", "ready",
                                           "failed")
    assert protocol.MOLECULE_WARNING_CODES == (
        "unparsable", "placeholder", "isotope", "unusual_element", "stereo", "charged_or_salt",
        "low_confidence", "duplicate", "smiles_molfile_differ")
    assert protocol.STRUCTURE_WARNING_CODES == protocol.MOLECULE_WARNING_CODES[:6]
    assert protocol.RECOGNITION_WARNING_CODES == ("no_molecules", "no_reaction",
                                                  "unassigned_molecules", "truncated")
    for code in ("recognition_not_installed", "recognition_failed", "image_invalid"):
        assert code in protocol.HOST_ERROR_CODES
    assert protocol.RECOGNIZE_TIMEOUT_S == 270
    assert (protocol.MAX_DESCRIBE_STRUCTURES, protocol.MAX_CHEMDRAW_STEPS) == (50, 12)
    assert protocol.MAX_CHEMDRAW_STEP_MOLECULES >= 1 and protocol.MAX_CHEMDRAW_CONDITION_LINES >= 1
    assert protocol.MAX_RECOGNITION_MOLECULES >= 1
    assert protocol.CHEMDRAW_STEP_KEYS == ("reactants", "agents", "products", "conditions")


def test_recognition_status():
    assert parse_request(capture("recognition_status")).params == {"start": False}
    assert parse_request(capture("recognition_status", start=None)).params == {"start": False}
    assert parse_request(capture("recognition_status", start=True)).params == {"start": True}
    assert parse_request({"id": 1, "v": 1, "op": "recognition_status"}).params == {"start": False}


@pytest.mark.parametrize("params", [{"start": 1}, {"start": "true"}, {"start": []},
                                    {"start": True, "mode": "reaction"}, {"image": "AAAA"}])
def test_recognition_status_rejected(params):
    rejected(capture("recognition_status", **params))


def test_recognize():
    r = parse_request(capture("recognize", image="iVBORw0KGgo=", mode="reaction"))
    assert r.op == "recognize" and r.params == {"image": "iVBORw0KGgo=", "mode": "reaction"}
    assert parse_request(capture("recognize", image="AAAA", mode="molecule")).params["mode"] \
        == "molecule"


def test_recognize_image_at_the_limit_is_accepted_and_one_more_is_not():
    image = "A" * protocol.MAX_IMAGE_BASE64_CHARS
    assert parse_request(capture("recognize", image=image, mode="reaction")).params["image"] \
        is image
    err = rejected(capture("recognize", image=image + "A", mode="reaction"))
    assert "too long" in err.message and len(err.message) < 300      # never quotes the image


def test_recognize_does_not_judge_the_content_of_the_image():
    # Whether it is base64 of a PNG is the host's check (image_invalid), not bad_request.
    assert parse_request(capture("recognize", image="not base64 !", mode="reaction")).params[
        "image"] == "not base64 !"


@pytest.mark.parametrize("params", [
    {"mode": "reaction"},                                  # no image
    {"image": None, "mode": "reaction"},
    {"image": "", "mode": "reaction"},
    {"image": 12, "mode": "reaction"},
    {"image": ["AAAA"], "mode": "reaction"},
    {"image": {"data": "AAAA"}, "mode": "reaction"},
    {"image": "AAAA"},                                     # no mode
    {"image": "AAAA", "mode": None},
    {"image": "AAAA", "mode": "scheme"},
    {"image": "AAAA", "mode": "Reaction"},
    {"image": "AAAA", "mode": 1},
    {"image": "AAAA", "mode": "reaction", "start": True},  # unknown key
    {"image": "AAAA", "mode": "reaction", "width": 100},
])
def test_recognize_rejected(params):
    rejected(capture("recognize", **params))


def test_describe_structures():
    r = parse_request(capture("describe_structures",
                              structures=[{"smiles": "  CCO "}, {"molfile": MOLFILE},
                                          {"molfile": None, "smiles": "c1ccccc1"}]))
    assert r.params == {"structures": [{"molfile": None, "smiles": "CCO"},
                                       {"molfile": MOLFILE, "smiles": None},
                                       {"molfile": None, "smiles": "c1ccccc1"}]}


def test_describe_structures_count_limits():
    full = [{"smiles": "C"}] * protocol.MAX_DESCRIBE_STRUCTURES
    assert len(parse_request(capture("describe_structures", structures=full))
               .params["structures"]) == protocol.MAX_DESCRIBE_STRUCTURES
    rejected(capture("describe_structures", structures=full + [{"smiles": "C"}]))
    rejected(capture("describe_structures", structures=[]))


@pytest.mark.parametrize("structures", [
    None, "CCO", {"smiles": "CCO"}, 3,
    ["CCO"], [None], [[]],
    [{}],                                                  # neither
    [{"molfile": MOLFILE, "smiles": "C"}],                 # both
    [{"smiles": None}], [{"molfile": None, "smiles": None}],
    [{"smiles": 1}], [{"molfile": ["x"]}],
    [{"smiles": "   "}], [{"molfile": "\n\n"}],
    [{"smiles": "C\nC"}],                                  # a SMILES is one line
    [{"molfile": "a\x00b"}],
    [{"smiles": "C", "name": "x"}],                        # unknown key
    [{"smiles": "C"}, {"smiles": 5}],                      # a later one is checked too
    [{"smiles": "C" * (MAX_STRUCTURE_CHARS + 1)}],
])
def test_describe_structures_rejected(structures):
    rejected(capture("describe_structures", structures=structures))


def test_describe_structures_needs_the_key_and_refuses_others():
    rejected(capture("describe_structures"))
    rejected(capture("describe_structures", structures=[{"smiles": "C"}], smiles="C"))


def step(**changes) -> dict:
    out = {"reactants": [MOLFILE], "products": [MOLFILE]}
    out.update(changes)
    return out


def test_reaction_to_chemdraw_minimal_fills_the_optional_lists():
    r = parse_request(capture("reaction_to_chemdraw", steps=[step()]))
    assert r.params == {"steps": [{"reactants": [MOLFILE], "agents": [], "products": [MOLFILE],
                                   "conditions": []}]}
    assert list(r.params["steps"][0]) == list(protocol.CHEMDRAW_STEP_KEYS)


def test_reaction_to_chemdraw_full():
    r = parse_request(capture("reaction_to_chemdraw", steps=[
        step(agents=[MOLFILE], conditions=["  Pd/C, H2 ", "", "   ", "EtOH, rt, 16 h"]),
        step(reactants=[], agents=None, conditions=None),
        step(products=[]),
    ]))
    steps = r.params["steps"]
    assert steps[0]["agents"] == [MOLFILE]
    assert steps[0]["conditions"] == ["Pd/C, H2", "EtOH, rt, 16 h"]   # trimmed, blanks dropped
    assert steps[1] == {"reactants": [], "agents": [], "products": [MOLFILE], "conditions": []}
    assert steps[2]["products"] == [] and steps[2]["reactants"] == [MOLFILE]


def test_reaction_to_chemdraw_limits():
    most = protocol.MAX_CHEMDRAW_STEP_MOLECULES
    lines = protocol.MAX_CHEMDRAW_CONDITION_LINES
    big = step(reactants=[MOLFILE] * most, agents=[MOLFILE] * most, products=[MOLFILE] * most,
               conditions=["x" * protocol.MAX_REACTION_TEXT] * lines)
    r = parse_request(capture("reaction_to_chemdraw", steps=[big] * protocol.MAX_CHEMDRAW_STEPS))
    assert len(r.params["steps"]) == protocol.MAX_CHEMDRAW_STEPS
    rejected(capture("reaction_to_chemdraw", steps=[step()] * (protocol.MAX_CHEMDRAW_STEPS + 1)))
    rejected(capture("reaction_to_chemdraw", steps=[]))
    for key in ("reactants", "agents", "products"):
        rejected(capture("reaction_to_chemdraw", steps=[step(**{key: [MOLFILE] * (most + 1)})]))
    rejected(capture("reaction_to_chemdraw", steps=[step(conditions=["x"] * (lines + 1))]))
    rejected(capture("reaction_to_chemdraw",
                     steps=[step(conditions=["x" * (protocol.MAX_REACTION_TEXT + 1)])]))
    rejected(capture("reaction_to_chemdraw",
                     steps=[step(products=["x" * (MAX_STRUCTURE_CHARS + 1)])]))


@pytest.mark.parametrize("steps", [
    None, "x", {"reactants": [MOLFILE], "products": [MOLFILE]}, 1,
    [None], ["x"], [[]],
    [{}],
    [{"reactants": [MOLFILE]}],                            # products missing
    [{"products": [MOLFILE]}],                             # reactants missing
    [{"reactants": None, "products": [MOLFILE]}],
    [{"reactants": [], "products": []}],                   # nothing to draw
    [{"reactants": [], "products": [], "agents": [MOLFILE]}],
    [{"reactants": MOLFILE, "products": [MOLFILE]}],       # not a list
    [{"reactants": [MOLFILE], "products": [MOLFILE], "agents": MOLFILE}],
    [{"reactants": [{"molfile": MOLFILE}], "products": [MOLFILE]}],
    [{"reactants": [None], "products": [MOLFILE]}],
    [{"reactants": [7], "products": [MOLFILE]}],
    [{"reactants": ["  \n "], "products": [MOLFILE]}],
    [{"reactants": ["a\x01b"], "products": [MOLFILE]}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "conditions": "rt"}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "conditions": [5]}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "conditions": [None]}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "conditions": ["two\nlines"]}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "conditions": ["tab\there"]}],
    [{"reactants": [MOLFILE], "products": [MOLFILE], "smiles": "C>>C"}],   # unknown key
    [{"reactants": [MOLFILE], "products": [MOLFILE]}, {"reactants": [], "products": []}],
])
def test_reaction_to_chemdraw_rejected(steps):
    rejected(capture("reaction_to_chemdraw", steps=steps))


def test_reaction_to_chemdraw_needs_the_key_and_refuses_others():
    rejected(capture("reaction_to_chemdraw"))
    rejected(capture("reaction_to_chemdraw", steps=[step()], open=False))


def test_reaction_to_chemdraw_error_names_the_place_not_the_content():
    err = rejected(capture("reaction_to_chemdraw",
                           steps=[step(), step(conditions=["ok", "secret\x02text"])]))
    assert "steps[1].conditions[1]" in err.message and "secret" not in err.message


@pytest.mark.parametrize("op", protocol.CAPTURE_OPS)
def test_capture_ops_follow_the_envelope_rules(op):
    params = {"recognition_status": {}, "recognize": {"image": "AAAA", "mode": "reaction"},
              "describe_structures": {"structures": [{"smiles": "C"}]},
              "reaction_to_chemdraw": {"steps": [step()]}}[op]
    assert parse_request({"id": "e", "v": 1, "op": op, "params": params}).op == op
    rejected({"id": "e", "v": 2, "op": op, "params": params}, "protocol_mismatch")
    rejected({"id": "e", "v": 1, "op": op, "params": [params]})
    rejected({"id": "e", "v": 1, "op": op, "params": {**params, "extra": 1}})
