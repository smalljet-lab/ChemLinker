# Development

For people who change the code. Users only need the [README](../README.md).

## Repository layout

```
install.cmd / install.ps1 / install.sh   bootstrap: uv, Python environment, then "compound-registry setup"
update.cmd  / update.ps1  / update.sh    fetch the new version, then install with --upgrade
pyproject.toml, uv.lock                  Python package definition and locked dependencies
scripts/set-version.py                   sets or checks the version in every file that carries it
src/compound_registry/                   the Python package
tests/                                   pytest tests; fixtures in tests/data
extension/                               the browser extension (TypeScript)
docs/                                    this documentation
.github/workflows/                       CI and release
```

Not in git: `.venv/`, `.python/`, `.uv-cache/` (made by the installer),
`extension/node_modules/`, the build outputs `extension/dist/`,
`extension/artifacts/` and `chrome-extension/`, and
`extension/local.config.json` (the ELN address chosen at installation).

### The Python package, `src/compound_registry`

The core:

| Module | Purpose |
| --- | --- |
| `errors.py` | `RegistryError` and its subclasses. Every error has a stable machine code and a message written for the user. |
| `models.py` | Frozen dataclasses shared by all layers. |
| `idformat.py` | `IdFormat`, the validated `CORE_STEREO_BATCH` template. |
| `config.py` | Finds the registry home folder, reads `config.toml`, resolves paths, warns about sync and network folders. |
| `chem.py` | The only place that decides what "the same compound" is: RDKit and standard InChI give `core_key` and `stereo_key`. Its docstring holds the rules. `KEY_ALGORITHM` versions the recipe. |
| `repository.py` | The storage protocol. |
| `sqlite_repo.py` | The SQLite backend: schema, permanence triggers, numbering check on open, schema upgrades, registry identity. |
| `journal.py` | `issued-ids.jsonl`, the append-only record of every ID outside the database. |
| `backup.py` | Timestamped, verified backups with retention. |
| `service.py` | `RegistryService`, the single facade used by the CLI, the native host and the MestReNova bridge. Also `create_registry` and `upgrade_registry`. |
| `search.py`, `properties.py`, `render.py`, `export.py` | Search for the compound list, display values, SVG thumbnails, CSV/SDF export. |

The outer shell:

| Module | Purpose |
| --- | --- |
| `protocol.py` | The native-messaging contract: constants, the list of operations, strict validation of every request. No I/O. |
| `host.py` | The program the browser starts (`compound-registry-host`). One frame in, one frame out. Never creates a registry. Log: `<home>/logs/host.log`. |
| `install_host.py` | Registers the host with Firefox and Chrome for the current user. All writes go through a `HostTarget`, which tests replace. |
| `setup_wizard.py` | `compound-registry setup`, the guided installation. Everything that touches the machine is in its `Environment`, which tests replace. |
| `cli.py` | `compound-registry <command>`. |

Outside programs:

| Module | Purpose |
| --- | --- |
| `route_model.py`, `route_data.py`, `routes.py`, `cdxml.py` | Build the synthesis route of a compound from stored reactions and write it as a ChemDraw document (CDXML). |
| `chemdraw.py`, `naming.py`, `chemdraw_name.ps1`, `chemdraw_cleanup.ps1` | Names from the local ChemDraw (Windows, through COM and Windows PowerShell). A name is accepted only if ChemDraw's InChI matches. |
| `files.py`, `spectra.py` | Compound folders and the records of spectra. |
| `mnova.py`, `mnova_addon/` | The bridge the MestReNova add-on calls (`python -m compound_registry.mnova ...`), the add-on itself (`compoundRegistry.qs`, `compoundRegistry.mui`) and its installer. |

Capture reaction (reading a reaction scheme from a picture):

| Module | Purpose |
| --- | --- |
| `recognition.py` | The host side of the four `CAPTURE_OPS`: where the optional part is installed, the worker process (start, time-outs, one restart, idle stop), turning the worker's raw answer into the protocol result with the registry's RDKit (canonical SMILES, warnings), and `reaction_to_chemdraw`. Needs no registry. Its docstring holds the SMILES-or-molfile measurement. |
| `depiction.py` | The molfile of each recognised molecule (card, structure editor, ChemDraw): the atoms where the picture has them (MolScribe's coordinates with their squeeze undone, evened out like "Clean Up Structure"), a computed layout only when the drawing cannot be used, wedges worked out from the structure and checked by reading the molfile back. Its docstring holds the rules. |
| `recognition_worker.py` | The worker: ONE standalone file that runs under the Python of the recognition environment, never under the registry's. Loads RxnScribe, MolScribe and EasyOCR once and answers JSON lines. Must not import `compound_registry`. |
| `recognition_install.py`, `recognition_requirements.txt`, `recognition_overrides.txt` | `compound-registry install-recognition`: creates the environment with uv from the pinned requirements and gets the model files (SHA-256 checked). Everything that touches the machine is in its `Tools`, which tests replace. |
| `cdxml.py` (`render_reaction`) | Draws reaction steps given as molfiles with the building blocks of the route writer. |

### The extension, `extension/`

One TypeScript tree, bundled by `build.mjs` (esbuild) into six scripts:

| Source | Bundle | Runs where |
| --- | --- | --- |
| `src/background/` | `background.js` | Firefox background script / Chrome service worker. Holds two native-messaging ports (two host processes): one for the registry operations, one for `CAPTURE_OPS`, because the host answers one request at a time and a recognition takes up to minutes. The second port is closed after 11 idle minutes. `message-handler.ts` decides who may send which operation. `capture.ts` takes the screenshot (`tabs.captureVisibleTab`, permission `activeTab`) and keeps it in memory until the capture page fetches it, once. |
| `src/content/` | `content.js` | Isolated world, in the ELN page. The panel, page reading, state machines. |
| `src/page/` | `page-bridge.js` | The ELN page's own world. Reads structures and the reaction table from the ELN's ZK widgets. Uses no extension API. |
| `src/compounds/` | `compounds.js` | The extension's own page (`static/compounds.html`): compound list, yields, import, files, routes, structure editor. |
| `src/capture/` | `capture.js` | The extension's own page for "Capture reaction" (`static/capture.html?id=...`): the box on the frozen screenshot, the result to check and correct. `geometry.ts` (crop and size arithmetic) and `model.ts` (molecules, roles, steps, reaction SMILES) are pure. |
| `src/popup/` | `popup.js` | The toolbar button's popup (`static/popup.html`): "Capture reaction" and "Open compound catalogue". Sends the background two messages and no host operation. |

`src/shared/` holds the contracts and no logic: `protocol.ts` (mirror of
`protocol.py`), `bridge.ts` (content script and page bridge), `import.ts`
(compounds page and content script), `adapter.ts`, `site.ts` (the ELN address
and the extension version, set at build time).

`static/` holds `manifest.json` (the Firefox manifest and the source of the
Chrome one), `chrome-key.json`, the three pages (compounds, capture, popup)
with their stylesheets and the icons. `scripts/`
holds the build helpers. Tests are in `extension/tests/`, fixtures in
`extension/tests/fixtures/`. All fixtures are invented data.

The only runtime dependency is OpenChemLib (the structure editor), bundled
only into the compounds page and the capture page. See
[browsers.md](browsers.md) for the two builds.

The capture page never touches the web page it shows: there is no content
script and no injected overlay for it, only the screenshot. Neither the
screenshot nor anything read from it is logged or stored; `static-checks.test.ts`
holds these rules (permissions, pages, bundles, where OpenChemLib may be).

## Setting up

You need uv and Node.js (20.19 or newer of the 20 series, or 22.12 or newer).

```
uv sync            # creates .venv with the dev group (pytest)
cd extension
npm ci
```

Notes:

- The installer runs `uv sync --locked --no-dev`, which removes pytest from
  `.venv`. After running the installer or the update script in a development
  checkout, run `uv sync` again.
- The installer keeps Python and the uv cache inside the project folder by
  setting `UV_PYTHON_INSTALL_DIR` to `.python` and `UV_CACHE_DIR` to
  `.uv-cache`. Set the same variables if you want your own `uv` calls to use
  them.
- RDKit is pinned to one exact version in `pyproject.toml`, because the keys
  that identify a compound depend on it. Read "Rules for compatible upgrades"
  before you change it.

To try the installer without changing anything on your PC:

```
.venv\Scripts\compound-registry.exe setup --dry-run
```

It reads the current browser registration and prints every file it would
write. Do not run `setup`, `install-host` or `uninstall-host` without
`--dry-run` in a development checkout unless you want your browsers to use
that checkout.

## Running the tests

Python:

```
uv run pytest
uv run pytest tests/test_chem.py
```

The tests never start ChemDraw and never open a file with another program:
`tests/conftest.py` sets `COMPOUND_REGISTRY_NO_CHEMDRAW=1` and
`COMPOUND_REGISTRY_NO_OPEN=1` for every test. CI sets
`COMPOUND_REGISTRY_NO_CHEMDRAW=1` as well. Tests that register a host or run
the wizard use temporary folders and a fake `HostTarget`.

Two test files use the ChemDraw that is really installed. They are skipped
unless you ask for them (Windows, needs a working ChemDraw licence, slow):

```
$env:COMPOUND_REGISTRY_LIVE_CHEMDRAW = "1"
.venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_chemdraw_live.py tests/test_cdxml_chemdraw_live.py
```

The recognition tests need no torch: the worker is replaced by
`tests/data/fake_recognition_worker.py`, and `install-recognition` runs with
a fake uv and fake downloads. One test file uses the real models. It is
skipped unless you ask for it, and needs the optional part installed
(`compound-registry install-recognition`, about 2.8 GB):

```
$env:COMPOUND_REGISTRY_LIVE_RECOGNITION = "1"
.venv/Scripts/python.exe -m pytest -o addopts="" -q tests/test_recognition_live.py
```

Extension:

```
cd extension
npm run typecheck      # tsc on the sources and on the tests
npm test               # vitest
```

`npm test` starts vitest through `scripts/run-vitest.mjs`, which works around
a Windows problem with lower-case drive letters. Use it instead of calling
vitest directly.

## How the pieces talk

```
ELN page (ZK widgets)
   |  CustomEvents, shared/bridge.ts
page-bridge.js  <->  content.js  --runtime.sendMessage-->  background.js
                                                               |  native messaging, protocol.py / protocol.ts
compounds.html / compounds.js  --runtime.sendMessage-->        |
                                                     compound-registry-host (host.py)
                                                               |
MestReNova add-on --> python -m compound_registry.mnova --> RegistryService --> registry folder
compound-registry (cli.py) ------------------------------->
```

**Extension and native host.** The browser starts `compound-registry-host`
and exchanges frames with it: a 32-bit length in native byte order, then UTF-8
JSON. A request is `{"id", "v", "op", "params"}`; a response is
`{"id", "ok": true, "result"}` or `{"id", "ok": false, "error": {"code",
"message"}}`. `v` is `PROTOCOL_VERSION`. Unknown parameters are refused. The
host opens a fresh `RegistryService` for every request. `protocol.py` is the
written contract; its docstring describes every operation.

The host checks who calls it: Firefox passes the add-on ID, Chrome passes the
extension origin, and the host answers only the IDs in `protocol.py`. The
background script checks too: only the content script on the ELN address and
the extension's own pages are served, each with its own list: the compounds
page (`PAGE_OPS`), the capture page (`CAPTURE_OPS` and `capture-image`) and
the toolbar popup (no host operation, only `capture-start` and
`open-compounds`). `retire` is in the protocol but the extension never sends it; IDs
are retired with the CLI only.

**Capture reaction.** The operations of `CAPTURE_OPS` (`recognition_status`,
`recognize`, `describe_structures`, `reaction_to_chemdraw`) are the only ones
the host serves without opening a registry: `handle_message` hands them to
`recognition.py` before it looks at the registry at all, so they also work
when no registry exists. The host answers one request at a time and a
recognition takes 10 to 60 seconds, so the extension sends these operations
over a second connection, i.e. to a second host process; that process owns
the worker. On that connection `recognize` is the one request the host
answers from a thread of its own (`host.serve`): the status of a second
capture page or a structure the user just edited is answered while a
recognition runs, and the answer of `recognize` is written when it is ready.
Answers are matched by `id`, never by order. The structure work of the
capture operations (RDKit) still runs one job at a time.

```
capture page --runtime.sendMessage--> background.js --second native port--> compound-registry-host
                                                                                 |  JSON lines on stdin/stdout
                                                    <recognition home>/env python -I recognition_worker.py
```

The recognition home is `%LOCALAPPDATA%\CompoundRegistry\recognition`
(`$COMPOUND_REGISTRY_RECOGNITION_HOME` names another one) with `env/`,
`models/` and `installed.json`, which the installer writes last. That record
holds the checksum of the pinned requirements and of the model files; an
installation made for other pins counts as not installed (the worker comes
from this package and is written for exactly these libraries), and
`install-recognition` brings it up to date. The worker
is started at the first `recognition_status {start: true}` or `recognize`,
loads the models once, is stopped after 10 minutes without use (a
`recognition_status {start: true}` counts as use) and when the
host ends, and ends by itself when its stdin closes (so none is left when the
host is killed). It returns raw model output; the host makes the SMILES with
the registry's RDKit, so they are canonical like everywhere else.

The installer deletes nothing on the strength of a folder name: `env/` only
when it is a Python environment (`pyvenv.cfg`), `models/` as a whole only
when it created the folder (it leaves a tag file there), otherwise only the
model files it knows. The environment is built on the Python that uv keeps in
the project folder (`.python`), so it stops working when the project folder
is moved; `install-recognition` notices that its Python no longer starts and
makes the environment again, keeping the model files.

Rules for this part:

- The registry's environment gets no new dependency. torch and the models
  live in the recognition environment only; `pyproject.toml` and `uv.lock` do
  not change for it.
- `recognition_worker.py` stays one standalone file: standard library at the
  top, model libraries imported inside functions, nothing from
  `compound_registry` (a test checks this).
- Nothing read from a picture is logged or put into an error message: no
  image data, no structure, no text. The worker reports the name of an
  exception class only. The picture goes to the worker through its stdin; no
  file is written.
- `recognition_requirements.txt` pins every package exactly, and
  `recognition_install.MODEL_FILES` pins every model file by SHA-256. Change
  them on purpose and run the live test afterwards. RxnScribe and MolScribe
  are source archives of one commit each, so git is not needed;
  `recognition_overrides.txt` replaces RxnScribe's own "MolScribe from git,
  branch main".
- The tests never start ChemDraw: `reaction_to_chemdraw` obeys
  `COMPOUND_REGISTRY_NO_OPEN` like `route`.
- The molfile and the SMILES of a molecule are one structure, stereo
  included. `depiction.drawing_molfile` reads every molfile back before it
  returns it; do not hand out a molfile that did not pass (the card would
  show one thing and Copy give another). The wedge of the picture is kept
  only when it tells the truth on the final coordinates. A molfile with a
  wedge carries the chiral flag, and the card (`renderMolfileSvg`) marks
  every stereocentre as absolute: without that, OpenChemLib treats the
  molecule as a racemate, and its option that hides the "&1" label also
  hides the wedge.
- The worker's answer may gain fields (`seen` and `source` came later). The
  host keeps working when one is missing: tests and tools feed it saved
  answers.
- Two remedies in the worker recover molecules RxnScribe misses (its
  docstring; the measurement is `tools/recognition-e2e/missed/DECISION.md`):
  "peek" re-implements RxnScribe's greedy decoding and reads its tokenizer
  states, so it depends on the pinned RxnScribe archive: the worker's
  `RXNSCRIBE_COMMIT` must name the commit of `recognition_requirements.txt`
  (a test checks it), and the worker switches peek off when the installed
  RxnScribe's `direct_url.json` names another one. When you change the
  RxnScribe pin, read its inference loop (`pix2seq/transformer.py`) again,
  change `RXNSCRIBE_COMMIT`, and run `tests/test_recognition_live.py`,
  which compares peek's main sequence with `predict_images` on the real
  models. The check the worker runs on every picture only compares its
  step-by-step decoding with one full decoder pass; it does not notice a
  change in RxnScribe's own loop. "Leftover ink" uses OpenCV, numpy and
  RDKit of the recognition environment and never fails a picture; above
  `LEFTOVER_MAX_PIXELS` (4 million) it works on a reduced copy. It was
  measured on dark drawings on a light background only, and its branch
  "RxnScribe returned boxes but no molecule box" has never been reached by a
  real picture (only by unit tests). Their unit tests are
  `tests/test_recognition_remedy.py`; the one that needs OpenCV runs a script
  under the recognition environment's Python and is skipped when the
  optional part is not installed.
- The capture operations skip `chem.normalize` and with it InChI's limit of
  1023 atoms, and RDKit ends the whole process (stack overflow, nothing to
  catch) when it writes the SMILES of a chain of about 3000 atoms. Keep
  `recognition.MAX_STRUCTURE_ATOMS` in front of every SMILES that is written
  there.

**Mirrored constants.** `extension/src/shared/protocol.ts` repeats the
constants of `protocol.py` (protocol version, host name, both extension IDs,
size limits, operations, error codes). Change both files together.
`extension/tests/release-checks.test.ts` reads `protocol.py` as text and fails
when they differ. The same test file checks that the version is the same in
`extension/package.json`, `package-lock.json`, `static/manifest.json` and
`src/compound_registry/__init__.py`.

The ELN address rule exists twice as well: `parseElnOrigin` in
`extension/scripts/eln-origin.mjs` and `parse_eln_origin` in
`setup_wizard.py`. Keep them the same.

**Content script and page bridge.** The content script cannot reach the ELN's
JavaScript objects, so `page-bridge.js` runs in the page's world and answers
requests sent as CustomEvents (`shared/bridge.ts`). The event names contain
the extension version, so a bridge left over from before an update is not
used by the new content script. The selectors and widget calls are tested
against Sciformation with ZK 10.

**MestReNova.** The add-on script starts
`pythonw.exe -m compound_registry.mnova <command> --out <file>` and reads the
JSON answer from the file. `mnova install` copies the add-on into
MestReNova's user script folder and writes the path of the Python program and
the program version into it.

**Which registry.** The CLI uses `--home`, else `$COMPOUND_REGISTRY_HOME`,
else `~/CompoundRegistry`. The host uses the folder written into the host
configuration next to the browser manifest by the installer. The MestReNova
add-on follows the host configuration unless a folder was written into it
with `mnova install --home`.

## Releasing a new version

Program and extension carry one version number. `__version__` in
`src/compound_registry/__init__.py` is the Python source of it
(`pyproject.toml` reads it from there).

1. Set the version everywhere:

   ```
   python scripts/set-version.py X.Y.Z
   python scripts/set-version.py --check
   ```

   This writes `src/compound_registry/__init__.py`, `extension/package.json`,
   `extension/package-lock.json` and `extension/static/manifest.json`.
2. Add the new section to `CHANGELOG.md`. Write it for users. If users have
   to do something by hand, say so under "Upgrading".
3. Run the tests (Python and extension).
4. Commit, then tag and push:

   ```
   git commit -am "Release X.Y.Z"
   git tag vX.Y.Z
   git push origin main
   git push origin vX.Y.Z
   ```

What the workflows do:

- `.github/workflows/ci.yml` runs on every push to `main` and on every pull
  request. It runs the Python tests on Windows (Linux and macOS are switched off for now, see the comment in the file)
  (`uv sync --locked`, `uv run pytest`). On Linux with Node.js 22 it runs
  `npm ci`, `npm run typecheck`, `npm test`, `npm run package` and
  `npm run package:chrome`.
- `.github/workflows/release.yml` runs when a tag `v*` is pushed. It stops if
  the tag is not `v` plus the version in `extension/package.json` and
  `__init__.py`. Then it runs `npm ci` and `npm test`, builds both packages
  for the default ELN address and creates a GitHub release named after the
  tag, with generated notes and the two files
  `chemlinker-X.Y.Z-firefox.xpi` and `chemlinker-X.Y.Z-chrome.zip`
  attached.

The release workflow does not run the Python tests. Wait for CI to pass on
`main` before you push the tag.

The extension warns the user when its first two version numbers differ from
the program's. So raise the second number when extension and program must be
updated together, and use the third number for corrections of one side.

## Rules for compatible upgrades

Users keep one registry for years and write its IDs into notebooks. An update
must never break that. These rules are checked by tests where possible.

**Never change the meaning of an issued ID.** An ID that was issued names the
same compound, stereo variant and batch for ever. IDs are never deleted or
reused; a wrong one is retired. The ID format of a registry cannot be
changed. The database enforces much of this with triggers.

**Chemistry keys.** Whether two drawings are the same compound is decided by
`core_key` and `stereo_key` from `chem.py`. Anything that can change a key for
an existing structure can make a known compound look new: a new RDKit or InChI
version, or a change to `chem.py`. Therefore:

- RDKit stays pinned exactly. Raise the pin on purpose, not as routine.
- Raise `KEY_ALGORITHM` in `chem.py` when the recipe changes.
- The registry stores a fingerprint (RDKit version, InChI version,
  `KEY_ALGORITHM`). When it differs, registering is blocked with
  `chemistry_version_changed` until every stored structure was recomputed and
  gave the same keys. The update does this (`service.upgrade_registry`, the
  `Registry check:` line); by hand it is `compound-registry rekey-check
  --accept`. If a key differs, nothing is written and registering stays
  blocked. Do not release a change that makes this happen for real data
  without a migration plan.

**Database schema.** `SCHEMA_VERSION` in `sqlite_repo.py` is the version of
the permanent tables and their guards. It is 1.

- To change the schema, raise `SCHEMA_VERSION` and add a step to
  `_MIGRATIONS`: `_MIGRATIONS[n]` turns a schema `n - 1` database into schema
  `n`. Every version from 2 up to `SCHEMA_VERSION` needs a step.
- On open, an older file is first copied to the backup folder
  (`<name>-before-schema-<N>-<UTC>.sqlite`), then the steps run in one
  transaction, and a `migrate` event is written. If a step fails, nothing is
  changed.
- A file with a higher schema version is refused ("Update the program"). So a
  user cannot go back to an older program after a schema upgrade, except by
  restoring the copy. Say this in the changelog when you raise the schema.
- Annotation tables and columns (names, spectra, yields, reactions) that an
  older file lacks are added on open without a version step
  (`_ANNOTATION_COLUMNS`).
- Opening a registry that needs no change only reads, so its file is not
  modified. Keep it that way: do not add writes to every open.
- Test an upgrade with `tests/test_schema_upgrade.py` as the model.

**Registry identity.** Every registry has `meta.registry_id` (32 hex digits).
Every journal line and every backup carries it. A registry only counts and
deletes its own backups, and refuses to write (`journal_shared`) when its
journal holds lines of another registry. Never change or regenerate the
`registry_id` of an existing registry. A registry created before the value
existed gets one derived from its creation time and ID format, so that its
older backups and copies get the same identity. Its journal lines name no
registry: the first open appends a copy of each with the `registry_id`
(`meta.unstamped_journal` records it), and after that a line that names no
registry and has no such copy counts as another registry's, as it always does
for a registry created by this version.

**Journal.** `issued-ids.jsonl` is append-only. Add fields if you must; never
remove or rename one, and never change what a field means: the journal of an
old version must stay readable, and `recover` re-creates IDs from it.

**Protocol.** `PROTOCOL_VERSION` changes only when a host and an extension of
different protocol versions can no longer work together: an operation, a field
or a meaning was removed or changed. Additions keep it: a new operation or a
new optional field does not change the number. An older host answers a new
operation with `unknown_op`, and the extension then tells the user to update.
Change `protocol.py` and `protocol.ts` together.

**Version handshakes.** Users update the program and the browser extension
in two steps, and may forget one.

- `ping` returns `host_version`. The extension compares the first two version
  numbers with its own and shows a warning when they differ.
- The MestReNova add-on sends its version with every call
  (`--addon-version`). The bridge answers `addon_outdated`, with the command
  that repairs it, when it differs from the program version. `setup`
  refreshes an installed add-on.
- The page bridge's event names contain the extension version.

**Installer.** `update` runs `setup --upgrade`, which asks nothing and keeps
the data folder, the browsers, the ELN address and the add-on. Keep
`--upgrade` free of questions, and keep `setup` from ever creating a registry
or switching to another one without an explicit confirmation or option.

**Configuration.** Unknown keys in the known sections of `config.toml` are
refused, so an old program stops on a new key. Add keys with a default and
mention them in the changelog.
