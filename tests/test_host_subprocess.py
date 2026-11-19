"""The native-messaging host as a REAL process speaking frames over pipes.

Runs ``python -m compound_registry.host`` and, when it exists, the installed
``compound-registry-host`` launcher of this environment. Every process gets
``COMPOUND_REGISTRY_HOME`` and the user profile folder inside ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from rdkit import Chem
from rdkit.Chem import rdDepictor

from compound_registry import protocol
from compound_registry.service import RegistryService

AMINE_R = "C[C@H](N)c1ccccc1"
ETHANOL = "CCO"
PROPANOL = "CCCO"
IGNORED_SMILES = "C[C@H](C)O"
TIMEOUT = 120

LAUNCHER = Path(sys.prefix) / ("Scripts/compound-registry-host.exe" if os.name == "nt"
                               else "bin/compound-registry-host")
COMMANDS = {"module": [sys.executable, "-m", "compound_registry.host"]}
if LAUNCHER.is_file():
    COMMANDS["launcher"] = [str(LAUNCHER)]


def molfile(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol)


# --------------------------------------------------------------------------- fixtures

def create_registry(path: Path) -> Path:
    """The host never creates a registry, so tests create it explicitly."""
    RegistryService.open(path).close()
    return path


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    """Environment of every host process; the env registry exists."""
    create_registry(tmp_path / "env-home")
    e = dict(os.environ)
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        e.pop(var, None)
    e["COMPOUND_REGISTRY_HOME"] = str(tmp_path / "env-home")
    e["USERPROFILE"] = str(tmp_path / "user")   # Path.home() on Windows
    e["HOME"] = str(tmp_path / "user")          # Path.home() elsewhere
    return e


@pytest.fixture
def env_home(tmp_path) -> Path:
    return tmp_path / "env-home"


@pytest.fixture(params=sorted(COMMANDS))
def cmd(request) -> list[str]:
    return COMMANDS[request.param]


MODULE = COMMANDS["module"]


# --------------------------------------------------------------------------- helpers

def frame(document) -> bytes:
    body = document if isinstance(document, bytes) else json.dumps(document).encode("utf-8")
    return struct.pack("=I", len(body)) + body


def parse_frames(data: bytes) -> list[dict]:
    """Parse ALL of stdout as frames: any other byte fails the test."""
    docs, pos = [], 0
    while pos < len(data):
        assert len(data) - pos >= 4, f"stray bytes on stdout: {data[pos:]!r}"
        (n,) = struct.unpack_from("=I", data, pos)
        body = data[pos + 4: pos + 4 + n]
        assert len(body) == n, f"stray bytes on stdout: {data[pos:pos + 80]!r}"
        docs.append(json.loads(body.decode("utf-8")))
        pos += 4 + n
    return docs


def run_host(env, data: bytes, args=(), command=None) -> tuple[int, list[dict], str]:
    """Send ``data``, close stdin, wait. Returns (exit code, frames, stderr)."""
    p = subprocess.run(list(command or MODULE) + list(args), input=data, capture_output=True,
                       env=env, timeout=TIMEOUT)
    stderr = p.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr, stderr
    return p.returncode, parse_frames(p.stdout), stderr


def request(op, params=None, request_id=1, v=1) -> dict:
    return {"id": request_id, "v": v, "op": op, "params": params or {}}


class Conversation:
    """One host process; ``ask`` sends a request and reads exactly its answer."""

    def __init__(self, env, command=None, args=()):
        self.p = subprocess.Popen(list(command or MODULE) + list(args), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.next_id = 0

    def ask(self, op, params=None) -> dict:
        self.next_id += 1
        self.p.stdin.write(frame(request(op, params, self.next_id)))
        self.p.stdin.flush()
        header = self.p.stdout.read(4)
        assert len(header) == 4, "the host closed its output"
        (n,) = struct.unpack("=I", header)
        doc = json.loads(self.p.stdout.read(n).decode("utf-8"))
        assert doc["id"] == self.next_id
        return doc

    def close(self) -> tuple[int, bytes, str]:
        """Close stdin; returns (exit code, remaining stdout, stderr)."""
        out, err = self.p.communicate(timeout=TIMEOUT)
        return self.p.returncode, out, err.decode("utf-8", "replace")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.p.poll() is None:
            self.p.kill()
            self.p.communicate()


def ok(doc):
    assert doc["ok"] is True, doc
    return doc["result"]


def err(doc, code):
    assert doc["ok"] is False and doc["error"]["code"] == code, doc
    return doc["error"]


def confirm_of(analysis: dict) -> dict:
    s = analysis["structure"]
    return {key: s[key] for key in protocol.CONFIRM_KEYS}


def batch_count(home: Path) -> int:
    if not (home / "registry.sqlite").exists():
        return 0
    with RegistryService.open(home) as svc:
        return svc.doctor()["stats"]["batches"]


# --------------------------------------------------------------------------- basic

ISOLATION_PROBE = """
import os
from compound_registry import host
frames_in, frames_out = host._isolate_stdio()
print("python print after isolation")
os.write(1, b"C-level write to fd 1")
host._write_frame(frames_out, b'{"probe":true}')
"""


def test_stdout_isolation_keeps_print_and_c_output_off_the_frame_stream(env):
    p = subprocess.run([sys.executable, "-c", ISOLATION_PROBE], capture_output=True, env=env,
                       timeout=TIMEOUT)
    assert p.returncode == 0, p.stderr
    assert p.stdout == frame(b'{"probe":true}')
    assert b"python print after isolation" in p.stderr


def test_ping(env, env_home, cmd):
    code, docs, _ = run_host(env, frame(request("ping")), command=cmd)
    assert code == 0 and len(docs) == 1
    r = ok(docs[0])
    assert r["home_source"] == "env" and r["home"] == str(env_home)
    assert r["protocol_version"] == protocol.PROTOCOL_VERSION
    assert r["mode_options"] == ["abs", "rac", "rel", "undefined"]
    assert (env_home / "logs" / "host.log").is_file()


def test_default_home_source(env, tmp_path):
    del env["COMPOUND_REGISTRY_HOME"]
    create_registry(tmp_path / "user" / "CompoundRegistry")
    code, docs, _ = run_host(env, frame(request("ping")))
    r = ok(docs[0])
    assert r["home_source"] == "default"
    assert r["home"] == str(tmp_path / "user" / "CompoundRegistry")


def test_stdin_closed_immediately_exits_0(env):
    code, docs, _ = run_host(env, b"")
    assert code == 0 and docs == []


def test_analyze(env):
    code, docs, _ = run_host(env, frame(request("analyze", {"smiles": AMINE_R})))
    r = ok(docs[0])
    assert r["status"] == "new" and r["structure"]["svg"].startswith("<svg")
    assert r["structure"]["stereo_mode"] == "abs"


def test_several_frames_in_one_go(env):
    data = b"".join(frame(request("analyze", {"smiles": s}, i))
                    for i, s in enumerate([ETHANOL, PROPANOL, AMINE_R], 1))
    code, docs, _ = run_host(env, data + frame(request("ping", request_id=4)))
    assert code == 0 and [d["id"] for d in docs] == [1, 2, 3, 4]
    assert all(d["ok"] for d in docs)


def test_malformed_json_then_valid_request(env):
    code, docs, _ = run_host(env, frame(b'{"id": 1, "op": ') + frame(request("ping", request_id=2)))
    assert code == 0 and len(docs) == 2
    assert docs[0]["id"] is None and err(docs[0], "bad_request")
    assert docs[1]["id"] == 2 and docs[1]["ok"]


def test_zero_length_frame(env):
    code, docs, _ = run_host(env, struct.pack("=I", 0) + frame(request("ping", request_id=2)))
    assert code == 0
    assert err(docs[0], "bad_request") and docs[0]["id"] is None
    assert docs[1]["ok"]


def test_oversized_declared_length(env):
    data = struct.pack("=I", protocol.MAX_REQUEST_BYTES + 1) + b"{}" + frame(request("ping"))
    code, docs, _ = run_host(env, data)
    assert code != 0
    assert len(docs) == 1 and docs[0]["id"] is None
    err(docs[0], "frame_too_large")


@pytest.mark.parametrize("tail", [b"\x10\x00", struct.pack("=I", 50) + b'{"id": 1'])
def test_truncated_frame(env, tail):
    code, docs, _ = run_host(env, frame(request("ping")) + tail)
    assert code == 1 and len(docs) == 1 and docs[0]["ok"]


def test_forbidden_caller(env, tmp_path, env_home):
    manifest = tmp_path / "compound_registry.json"
    manifest.write_text("{}", encoding="utf-8")
    code, docs, _ = run_host(env, frame(request("ping")),
                             args=[str(manifest), "someone-else@example.org"])
    assert code == 1 and len(docs) == 1
    assert docs[0]["id"] is None
    err(docs[0], "forbidden_caller")
    assert batch_count(env_home) == 0


def test_firefox_style_arguments_are_accepted(env, tmp_path):
    manifest = tmp_path / "compound_registry.json"
    manifest.write_text("{}", encoding="utf-8")
    code, docs, _ = run_host(env, frame(request("ping")),
                             args=[str(manifest), protocol.EXTENSION_ID])
    assert code == 0 and ok(docs[0])["home_source"] == "env"


# --------------------------------------------------------------------------- registration

def test_register_conversation(env, env_home, cmd):
    mol = molfile(AMINE_R)
    with Conversation(env, cmd) as c:
        a = ok(c.ask("analyze", {"molfile": mol, "experiment_ref": "4180", "product_index": 1}))
        assert a["status"] == "new"
        params = {"molfile": mol, "experiment_ref": "4180", "product_index": 1,
                  "experiment_code": "ABC-008", "confirm": confirm_of(a)}
        first = ok(c.ask("register", params))
        assert first["created"] is True and first["id"] == "000001_01_001"
        again = ok(c.ask("register", params))
        assert again["id"] == first["id"] and again["created"] is False
        code, rest, _ = c.close()
    assert code == 0 and rest == b""

    # visible to a second host process ...
    code, docs, _ = run_host(env, frame(request(
        "analyze", {"smiles": AMINE_R, "experiment_ref": "4180"})), command=cmd)
    r = ok(docs[0])
    assert r["status"] == "registered_here" and r["id"] == "000001_01_001"
    # ... and to the service opened directly
    with RegistryService.open(env_home) as svc:
        b = svc.get("000001_01_001")
    assert b["source"] == "extension" and b["experiment_code"] == "ABC-008"

    log = (env_home / "logs" / "host.log").read_text(encoding="utf-8")
    assert "op=register" in log and "full_id=000001_01_001" in log
    for line in mol.splitlines():
        if line.strip():
            assert line not in log, f"molfile text in the log: {line!r}"
    for secret in (AMINE_R, a["structure"]["inchikey"], a["structure"]["inchi"],
                   a["structure"]["smiles"], "4180", "ABC-008", "M  END"):
        assert secret not in log


def test_retire(env, env_home):
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        r = ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1", "product_index": 1,
                                  "confirm": confirm_of(a)}))
        retired = ok(c.ask("retire", {"id": r["id"], "reason": "wrong product"}))
        assert retired["batch"]["retired"] is True
        err(c.ask("retire", {"id": r["id"], "reason": "again"}), "already_retired")
        assert c.close()[0] == 0
    log = (env_home / "logs" / "host.log").read_text(encoding="utf-8")
    assert f"op=retire id=3 ok" in log and "wrong product" not in log


def test_confirmation_mismatch_writes_nothing(env, env_home):
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": PROPANOL}))
        e = err(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                   "product_index": 1, "confirm": confirm_of(a)}),
                "confirmation_mismatch")
        assert "Nothing was registered" in e["message"]
        wrong_mode = dict(confirm_of(ok(c.ask("analyze", {"smiles": ETHANOL}))), stereo_mode="rac")
        e = err(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1", "product_index": 1,
                                   "confirm": wrong_mode}),
                "confirmation_mismatch")
        assert "stereo mode" in e["message"]
        assert c.close()[0] == 0
    assert batch_count(env_home) == 0
    journal = env_home / "issued-ids.jsonl"
    assert not journal.exists() or journal.read_text(encoding="utf-8").strip() == ""


def test_ignored_stereo_acceptance(env, env_home):
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": IGNORED_SMILES}))
        ignored = a["structure"]["ignored_stereo"]
        assert ignored
        params = {"smiles": IGNORED_SMILES, "experiment_ref": "E1", "product_index": 1,
                  "confirm": confirm_of(a)}
        err(c.ask("register", params), "stereo_not_in_key")
        err(c.ask("register", dict(params, accept_ignored_stereo=["not the same text"])),
            "stereo_not_in_key")
        assert batch_count(env_home) == 0
        r = ok(c.ask("register", dict(params, accept_ignored_stereo=ignored)))
        assert r["created"] and r["accepted_ignored_stereo"] == ignored
        assert c.close()[0] == 0


def test_slot_conflict_carries_existing_id(env):
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        first = ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                      "product_index": 1, "confirm": confirm_of(a)}))
        b = ok(c.ask("analyze", {"smiles": PROPANOL, "experiment_ref": "E1", "product_index": 1}))
        assert b["slot_conflict"]["full_id"] == first["id"]
        e = err(c.ask("register", {"smiles": PROPANOL, "experiment_ref": "E1",
                                   "product_index": 1, "confirm": confirm_of(b)}),
                "slot_conflict")
        assert e["existing_id"] == first["id"]
        assert c.close()[0] == 0


def test_non_ascii_experiment_code_survives(env, env_home):
    code_text = "José-Ω-001 µmol"
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        r = ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E-ü", "product_index": 1,
                                  "experiment_code": code_text, "confirm": confirm_of(a)}))
        assert r["batch"]["experiment_code"] == code_text
        a2 = ok(c.ask("analyze", {"smiles": ETHANOL, "experiment_ref": "E-ü"}))
        assert a2["batches"][0]["experiment_code"] == code_text
        assert c.close()[0] == 0
    with RegistryService.open(env_home) as svc:
        assert svc.get(r["id"])["experiment_code"] == code_text


def test_reactions_then_route_conversation(env, tmp_path, capsys):
    """What the extension does after "Create ID": store each experiment's
    reaction, then draw the route. Opening stays off (COMPOUND_REGISTRY_NO_OPEN
    is inherited from the test environment)."""
    from conftest import build_example_registry, example_data

    home = build_example_registry(tmp_path / "example", reactions=False)
    capsys.readouterr()
    env["COMPOUND_REGISTRY_HOME"] = str(home)
    assert env.get("COMPOUND_REGISTRY_NO_OPEN") == "1"
    with Conversation(env) as c:
        for e in example_data()["experiments"].values():
            r = ok(c.ask("set_reaction", {"experiment_ref": e["experiment_ref"],
                                          "experiment_code": e["experiment_code"],
                                          "experiment_url": e["experiment_url"],
                                          "reaction": e["reaction"]}))
            assert r["changed"] is True and r["warnings"] == []
        r = ok(c.ask("route", {"core_stereo_id": "EX0004_01"}))
        assert c.close()[0] == 0
    assert r["steps"] == 4 and r["missing"] == [] and r["opened"] is False
    assert "COMPOUND_REGISTRY_NO_OPEN" in r["open_message"]
    assert r["experiments"] == ["ABC-001", "ABC-002", "ABC-004", "ABC-003", "ABC-006", "ABC-008"]
    path = Path(r["file"]["path"])
    assert path.is_file() and path.parent.name == "EX0004_01"
    assert path.read_text(encoding="utf-8") == r["cdxml"]
    log = (home / "logs" / "host.log").read_text(encoding="utf-8")
    assert "op=route" in log and "op=set_reaction" in log and "M  END" not in log


def test_cli_change_while_the_host_stays_alive(env, env_home):
    with Conversation(env) as c:
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        params = {"smiles": ETHANOL, "experiment_ref": "E1", "product_index": 1,
                  "confirm": confirm_of(a)}
        first = ok(c.ask("register", params))
        with RegistryService.open(env_home) as svc:   # what 'compound-registry retire' does
            svc.retire(first["id"], "retired in the CLI")
        second = ok(c.ask("register", params))
        assert second["created"] and second["id"] != first["id"]
        assert c.close()[0] == 0


# --------------------------------------------------------------------------- host configuration

def _manifest_with_sidecar(tmp_path, content: bytes | None) -> Path:
    folder = tmp_path / "native-host"
    folder.mkdir()
    manifest = folder / "compound_registry.json"
    manifest.write_text("{}", encoding="utf-8")
    if content is not None:
        (folder / "compound_registry.config.json").write_bytes(content)
    return manifest


def test_home_from_host_config(env, tmp_path, env_home):
    chosen = create_registry(tmp_path / "chosen-registry")
    manifest = _manifest_with_sidecar(tmp_path, json.dumps({"home": str(chosen)}).encode())
    with Conversation(env, args=[str(manifest), protocol.EXTENSION_ID]) as c:
        r = ok(c.ask("ping"))
        assert r["home_source"] == "host-config" and r["home"] == str(chosen)
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1", "product_index": 1,
                              "confirm": confirm_of(a)}))
        assert c.close()[0] == 0
    assert batch_count(chosen) == 1
    assert (chosen / "logs" / "host.log").is_file()
    assert batch_count(env_home) == 0 and not (env_home / "logs").exists()


@pytest.mark.parametrize("with_env", [True, False])
def test_invalid_host_config_does_not_touch_another_registry(env, tmp_path, env_home, with_env):
    if not with_env:
        del env["COMPOUND_REGISTRY_HOME"]
    manifest = _manifest_with_sidecar(tmp_path, b'{"home": "relative/path"}')
    sidecar = manifest.parent / "compound_registry.config.json"
    data = (frame(request("ping", request_id=1))
            + frame(request("analyze", {"smiles": ETHANOL}, 2))
            + frame(request("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                         "product_index": 1,
                                         "confirm": {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                                                     "stereo_mode": "none",
                                                     "key_digest": "0" * 64}}, 3)))
    code, docs, _ = run_host(env, data, args=[str(manifest), protocol.EXTENSION_ID])
    assert code == 0 and [d["id"] for d in docs] == [1, 2, 3]
    for d in docs:
        e = err(d, "config_invalid")
        assert str(sidecar) in e["message"] and "absolute" in e["message"]
    assert batch_count(env_home) == 0 and not (env_home / "logs").exists()
    assert not (tmp_path / "user" / "CompoundRegistry").exists()
    assert not (manifest.parent / "relative").exists()


# --------------------------------------------------------------------------- registry missing / changed

def test_moved_registry_is_never_recreated(env, tmp_path):
    """Register, move the registry folder away while the host stays alive:
    every request is answered registry_missing and nothing appears at the old
    path (the reviewer's proof issued 000001_01_001 a second time here)."""
    home = create_registry(tmp_path / "Registry")
    manifest = _manifest_with_sidecar(tmp_path, json.dumps({"home": str(home)}).encode())
    moved = tmp_path / "Registry-moved"
    with Conversation(env, args=[str(manifest), protocol.EXTENSION_ID]) as c:
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        first = ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                      "product_index": 1, "confirm": confirm_of(a)}))
        assert first["id"] == "000001_01_001"
        home.rename(moved)   # possible while connected: the host holds no file open
        e = err(c.ask("ping"), "registry_missing")
        assert str(home) in e["message"] and "never creates a registry" in e["message"]
        e = err(c.ask("register", {"smiles": PROPANOL, "experiment_ref": "E2",
                                   "product_index": 1, "confirm": confirm_of(a)}),
                "registry_missing")
        err(c.ask("analyze", {"smiles": PROPANOL}), "registry_missing")
        assert c.close()[0] == 0
    assert not home.exists()
    assert batch_count(moved) == 1
    # a new process does not create it either
    code, docs, _ = run_host(env, frame(request("ping")),
                             args=[str(manifest), protocol.EXTENSION_ID])
    assert code == 0 and err(docs[0], "registry_missing")
    assert not home.exists()


def test_missing_default_registry_is_not_created(env, tmp_path):
    del env["COMPOUND_REGISTRY_HOME"]
    code, docs, _ = run_host(env, frame(request("ping")))
    assert code == 0 and err(docs[0], "registry_missing")
    assert not (tmp_path / "user" / "CompoundRegistry").exists()


def test_changed_host_config_ends_the_connection(env, tmp_path):
    """Modelled on the reviewer's stale_sidecar.py: install-host --home NEW
    while the host still serves OLD."""
    old = create_registry(tmp_path / "OLD")
    new = create_registry(tmp_path / "NEW")
    manifest = _manifest_with_sidecar(tmp_path, json.dumps({"home": str(old)}).encode())
    sidecar = manifest.parent / "compound_registry.config.json"
    args = [str(manifest), protocol.EXTENSION_ID]
    with Conversation(env, args=args) as c:
        assert ok(c.ask("ping"))["home"] == str(old)
        sidecar.write_text(json.dumps({"home": str(new)}), encoding="utf-8")
        e = err(c.ask("ping"), "registry_changed")
        assert str(old) in e["message"] and str(new) in e["message"]
        assert "Nothing was written" in e["message"]
        code, rest, _ = c.close()
        assert code == 0 and rest == b""
    # the next connection uses the new configuration
    code, docs, _ = run_host(env, frame(request("ping")), args=args)
    assert ok(docs[0])["home"] == str(new)


# --------------------------------------------------------------------------- started by Chrome

#: Runs the host's real ``main`` like the launcher does, but on a machine in
#: tmp_path: ``install_host.default_target`` returns a target whose HKCU is a
#: read-only dictionary from the JSON file in argv[1] (never the real registry).
CHROME_WRAPPER = """
import json, sys
from pathlib import Path
from compound_registry import host, install_host

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

class Hkcu:
    def read_default(self, key):
        return cfg["hkcu"].get(key)
    def write_default(self, key, value):
        raise RuntimeError("the host wrote to the registry")
    delete_key = write_default

target = install_host.HostTarget(
    cfg["platform"], Path(cfg["manifest_dir"]), Path(cfg["launcher"]), Hkcu(),
    chrome_manifest_dir=Path(cfg["chrome_dir"]) if cfg["chrome_dir"] else None)
install_host.default_target = lambda **kw: target
raise SystemExit(host.main(sys.argv[2:]))
"""


class _Hkcu:
    def __init__(self):
        self.values: dict[str, str] = {}

    def read_default(self, key):
        return self.values.get(key)

    def write_default(self, key, value):
        self.values[key] = value

    def delete_key(self, key):
        return self.values.pop(key, None) is not None


def _machine(tmp_path, platform):
    """Install both browsers (in tmp_path) with the real installer code."""
    from compound_registry import install_host

    hkcu = _Hkcu()
    t = install_host.default_target(platform=platform, prefix=tmp_path / "venv",
                                    user_home=tmp_path / "user", registry=hkcu)
    t.launcher.parent.mkdir(parents=True)
    t.launcher.write_bytes(b"launcher")
    cfg = tmp_path / "machine.json"

    def save():
        cfg.write_text(json.dumps({
            "platform": platform, "manifest_dir": str(t.manifest_dir),
            "launcher": str(t.launcher), "hkcu": hkcu.values,
            "chrome_dir": str(t.chrome_manifest_dir) if t.chrome_manifest_dir else None}),
            encoding="utf-8")
        return [sys.executable, "-c", CHROME_WRAPPER, str(cfg)]

    return t, save


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_chrome_start_uses_the_registry_firefox_uses(env, tmp_path, env_home, platform):
    from compound_registry import install_host

    shared = create_registry(tmp_path / "shared")
    t, save = _machine(tmp_path, platform)
    install_host.install(shared, target=t)                         # Firefox first ...
    install_host.install(target=t, browsers=("chrome",))           # ... then Chrome
    command = save()
    # Firefox's way: manifest path + add-on ID
    code, docs, _ = run_host(env, frame(request("ping")),
                             args=[str(t.manifest_path), protocol.EXTENSION_ID])
    assert code == 0 and ok(docs[0])["home"] == str(shared)
    # Chrome's way: origin (+ --parent-window on Windows)
    tail = ["--parent-window=0"] if platform == "windows" else []
    with Conversation(env, command, args=[protocol.CHROME_ORIGIN, *tail]) as c:
        r = ok(c.ask("ping"))
        assert r["home"] == str(shared) and r["home_source"] == "host-config"
        a = ok(c.ask("analyze", {"smiles": ETHANOL}))
        reg = ok(c.ask("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                    "product_index": 1, "confirm": confirm_of(a)}))
        code, rest, stderr = c.close()
    assert code == 0 and rest == b"" and "Traceback" not in stderr
    assert reg["id"] == "000001_01_001"
    assert batch_count(shared) == 1 and batch_count(env_home) == 0
    log = (shared / "logs" / "host.log").read_text(encoding="utf-8")
    assert "started by chrome" in log and "started by firefox" in log
    assert f"full_id={reg['id']}" in log
    # Firefox sees the ID Chrome issued
    code, docs, _ = run_host(env, frame(request("analyze", {"smiles": ETHANOL,
                                                            "experiment_ref": "E1"})),
                             args=[str(t.manifest_path), protocol.EXTENSION_ID])
    assert ok(docs[0])["id"] == reg["id"]


def test_chrome_start_without_parent_window(env, tmp_path):
    from compound_registry import install_host

    shared = create_registry(tmp_path / "shared")
    t, save = _machine(tmp_path, "windows")
    install_host.install(shared, target=t, browsers="both")
    code, docs, _ = run_host(env, frame(request("ping")), args=[protocol.CHROME_ORIGIN],
                             command=save())
    assert code == 0 and ok(docs[0])["home"] == str(shared)


def test_another_chrome_extension_is_refused(env, tmp_path):
    from compound_registry import install_host

    shared = create_registry(tmp_path / "shared")
    t, save = _machine(tmp_path, "windows")
    install_host.install(shared, target=t, browsers="both")
    data = frame(request("register", {"smiles": ETHANOL, "experiment_ref": "E1",
                                      "product_index": 1,
                                      "confirm": {"inchikey": "x", "stereo_mode": "none",
                                                  "key_digest": "0" * 64}}))
    for origin in ("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
                   protocol.CHROME_ORIGIN.rstrip("/"), protocol.CHROME_ORIGIN.upper()):
        code, docs, _ = run_host(env, data, args=[origin, "--parent-window=0"],
                                 command=save())
        assert code == 1 and len(docs) == 1 and docs[0]["id"] is None
        e = err(docs[0], "forbidden_caller")
        assert protocol.CHROME_EXTENSION_ID in e["message"]
    assert batch_count(shared) == 0
    assert "refused: started by Chrome extension" in (
        shared / "logs" / "host.log").read_text(encoding="utf-8")


def test_chrome_start_without_registration_touches_no_registry(env, tmp_path, env_home):
    t, save = _machine(tmp_path, "windows")    # nothing installed
    code, docs, _ = run_host(env, frame(request("ping")) + frame(request("analyze", {
        "smiles": ETHANOL}, 2)), args=[protocol.CHROME_ORIGIN, "--parent-window=0"],
        command=save())
    assert code == 0 and [d["id"] for d in docs] == [1, 2]
    for d in docs:
        assert "--browser chrome" in err(d, "config_invalid")["message"]
    assert not (env_home / "logs").exists() and batch_count(env_home) == 0


def test_removed_host_config_ends_the_connection(env, tmp_path, env_home):
    old = create_registry(tmp_path / "OLD")
    manifest = _manifest_with_sidecar(tmp_path, json.dumps({"home": str(old)}).encode())
    with Conversation(env, args=[str(manifest), protocol.EXTENSION_ID]) as c:
        ok(c.ask("ping"))
        (manifest.parent / "compound_registry.config.json").unlink()
        a = c.ask("analyze", {"smiles": ETHANOL})
        e = err(a, "registry_changed")
        assert str(env_home) in e["message"]
        assert c.close()[0] == 0
