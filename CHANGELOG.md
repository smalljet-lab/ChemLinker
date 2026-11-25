# Changelog

All notable changes to this project are written down in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The registry
program and the browser extension are released together under one version
number.

## [0.12.0] - 2026-11-24

### Added

- **Capture reaction: read a drawn reaction scheme from the screen.** Click
  the ChemLinker button in the browser's toolbar and choose **Capture
  reaction** (or press Alt+Shift+R). The extension takes a picture of the tab
  you are looking at (a paper, a PDF, a slide) and shows it on a page of its
  own. Drag a box around the scheme and press **Recognise**. You get every
  molecule as SMILES and as a structure you can edit, which molecules are
  reactants, products or drawn over the arrow, the reaction SMILES, the text
  at the arrow, **Copy all**, and **Open in ChemDraw**, which draws the
  reaction as you corrected it. A switch reads a single molecule instead of
  a reaction. Everything runs on your PC with open-source programs
  (RxnScribe, MolScribe, EasyOCR); no picture, structure or text is sent
  anywhere, the picture is kept in memory only, and no registry is needed.
- **The result is a draft to check.** About 9 of 10 molecules are read
  correctly from a clean drawing and about 7 of 10 from a figure in a paper;
  a wrong answer often looks plausible, and the text at the arrow is often
  misread. The page shows the picture with numbered boxes next to the redrawn
  structures and marks what deserves a second look (stereo and double-bond
  geometry, salts, isotope labels, unusual elements, placeholders, a low
  score of the program, two readings that disagree). You can correct every
  structure in the built-in editor, change roles and steps, add or remove
  molecules and edit the text.
- **Each structure is redrawn the way the picture shows it,** so you can
  compare the two at a glance: the same orientation, the same atoms left,
  right, up and down, evened out to regular rings and equal bonds. A group
  the picture abbreviates (Boc, OTf, Ph) is drawn out from where its label
  stands. Every stereocentre is drawn with a wedge that says what the SMILES
  on the same card says, on the bond the picture has wedged where that is
  possible; the structure editor and **Open in ChemDraw** get the same
  drawing. Only a structure whose drawing cannot be used gets a fresh layout.
- **`compound-registry install-recognition`** installs the part this needs.
  It is optional and large (about 2.8 GB in
  `%LOCALAPPDATA%\CompoundRegistry\recognition`, about 2.5 GB of memory while
  it runs, no graphics card needed). The installer asks
  `Install ChemRecogniser (about 2.8 GB)?` (default no; `--recognition yes|no`
  answers in advance, `--yes` never installs it, an update refreshes an
  existing one). The
  internet is needed for this command only; the model files are checked
  against known checksums. The command can be interrupted and repeated,
  `--models-from <folder>` uses model files you already have, `--uninstall`
  removes the part (only what the command itself created), and `doctor`
  shows whether it is installed. Run it again after an update of ChemLinker
  that changes the recognition programs, or after moving the ChemLinker
  folder; the capture page says when that is needed. The registry program's
  own environment is not changed.
- **Spectra of undesired side products in the MestReNova add-on.** Under the
  **Crude spectrum** box, the dialog that links a spectrum to a compound ID
  has a box **Undesired side product**. With the box ticked, the document is
  saved into the compound's folder as `<ID>_<sample>_side.mnova`
  (`..._crude_side.mnova` with both boxes ticked) and, as for a crude
  spectrum, only the place of that file is stored with the compound: no PDF,
  no shift report and no parameters. The catalogue lists it under
  **Undesired side product** with its path and nothing else.

### Changed

- **The toolbar button opens a small menu** with **Capture reaction** and
  **Open compound catalogue**. Before, a click opened the catalogue directly.
- **The extension asks for one more permission, `activeTab`.** It lets the
  extension take a picture of the tab you are looking at, and only at the
  moment you choose **Capture reaction** or press its shortcut. It gives no
  access to other tabs or to the content of web pages, and the extension
  still runs on the ELN address only.
- **ChemDraw routes:** a double bond whose geometry is not known is drawn as
  a wavy double bond instead of a definite E or Z, and a long line at an
  arrow without commas is wrapped at spaces.

### Updating from 0.11.0

Run `update.cmd` (or `install.cmd`), install the 0.12.0 extension in the
browser and restart the browser, so that it starts the new program. The
registry itself does not change. To use **Capture reaction**, run
`.venv\Scripts\compound-registry.exe install-recognition` once in the
project folder; everything else works without it. In Chrome, **Capture
reaction** does not work for a tab in an Incognito window.

## [0.11.0] - 2026-11-23

### Added

- **Crude spectra in the MestReNova add-on.** The dialog that links a
  spectrum to a compound ID has a box **Crude spectrum**. With the box
  ticked, the document is saved into the compound's folder as
  `<ID>_<sample>_crude.mnova` and only the place of that file is stored with
  the compound: no PDF, no shift report and no parameters. The catalogue
  lists it under **Crude spectrum** with its path and nothing else. To turn a
  crude spectrum into a full one, link the document again with the box
  unticked and use **Save Report and PDF to Compound**.

### Changed

- **The project is now called ChemLinker.** The browser extension, the
  panel in the ELN, the MestReNova menu (**Scripts > ChemLinker**) and the
  messages use the new name, and the extension packages are named
  `chemlinker-<version>-firefox.xpi` and `chemlinker-<version>-chrome.zip`.
  What you type and what is stored keep their names: the command is still
  `compound-registry`, and the data folder, the IDs and the registration with
  the browsers do not change.

### Updating from 0.10.0

Run `update.cmd` (or `install.cmd`), which also brings the MestReNova add-on
up to date, restart MestReNova and install the 0.11.0 extension in the
browser. The registry itself does not change.

## [0.10.0] - 2026-11-19

First public release.

### Added

- **Permanent compound IDs.** Every compound gets an ID `CORE_STEREO_BATCH`,
  starting with three capital letters chosen when the registry is created (for example
  `ABC000042_01_003`). Stereoisomers share the `CORE` number; salts, solvates
  and isotopically labelled compounds are compounds of their own. An ID is
  never reused; a wrong ID is retired, not deleted.
- **Panel in the Sciformation ELN** (Firefox 140+ and Chrome 120+). It
  detects the products of the open experiment, looks them up and creates IDs
  with one click. It warns about stereo that an ID would not distinguish and
  asks before it creates anything. A manual input (paste or drop a structure)
  is there for cases the page cannot be read.
- **Compound catalogue** in the extension's own page: search by ID, name,
  formula, ELN number, SMILES, InChI or a drawn structure; details with
  batches, experiments, stereo variants, spectra and files.
- **Names from ChemDraw** (Windows, ChemDraw desktop): the name of a
  compound on request, or typed by hand.
- **Yields**: the yield of each product is stored with its ID; a table of all
  yields with CSV download.
- **Synthesis routes**: the reactions that led to a compound are stored from
  the ELN and drawn as a ChemDraw file, with the choice of the starting
  material per step.
- **MestReNova add-on** (Windows): link a spectrum to a compound ID, save the
  MestReNova document, a PDF and the multiplet report into the compound's
  folder, paste the structure onto the spectrum, browse the compound library.
- **Bulk import** from the experiment list exported by the ELN (CSV), with a
  check-only run and a report.
- **Compound folders**: one folder per compound and stereo variant, with the
  structure as a `.mol` file.
- **Safety of the data**: automatic, verified backups with retention; a
  journal of every issued ID outside the database, which blocks new IDs after
  an old backup was restored until `compound-registry recover` has run; a
  check of all stored structures when the chemistry library changes.
- **Guided installation** (`install.cmd` / `install.ps1` / `install.sh`,
  `compound-registry setup`) and **update** (`update.cmd` / `update.ps1` /
  `update.sh`) for the current user, without administrator rights. A
  `--dry-run` option prints what would be done.
- **ELN address as a setting**: the extension can be built for any
  Sciformation served over https at the root of its host. The default is
  `https://sciformation.chemie.unibas.ch`.
- **Command-line tool** `compound-registry`: `doctor`, `host-status`,
  `backup`, `recover`, `retire`, `export` (CSV, SDF), `compounds`, `lookup`
  and more.
- Ready-made extension packages for the default ELN address on the GitHub
  Releases page.

### Upgrading from an earlier private build

If you used this program before it was published, your registry is kept as it
is. Do this once:

1. Close Firefox, Chrome and MestReNova.
2. Put the new project folder in place and run `install.cmd` (not
   `update.cmd`). Give the data folder of your existing registry; the
   installer shows it as the default when the browser was set up for it. Do
   not create a new registry. If the new project folder is not the old one,
   the installer asks `Let this installation take over?`; answer yes.
3. Install the new extension in the browser and reload the ELN tabs.

What changes:

- **Version numbers.** Program and extension now share one version, 0.10.0.
  Earlier builds had separate numbers. The panel warns when the two come from
  different releases.
- **Package file names.** The Firefox package is now
  `extension/artifacts/compound-registry-<version>-firefox.xpi` (it was
  `compound-registry-<version>.xpi`). The Chrome package
  `compound-registry-<version>-chrome.zip` is new.
- **The registry gets an identity.** The first time the new program writes to
  your registry, it stores a `registry_id` in the database and appends a copy
  of each existing line of the issued-ID journal (`issued-ids.jsonl`, in the
  data folder and in the backup folder) that names this `registry_id`. Nothing
  else changes, and no step by hand is needed. Older backups and journal
  lines of the same registry are recognised as its own.
- **One backup folder per registry.** If two registries were pointed at the
  same backup folder, creating IDs is now refused with a message that says so
  (`journal_shared`). Give each registry its own `[backup] folder` in
  `config.toml`.
- **MestReNova add-on.** An add-on installed by an earlier build is refused
  with a message that it is outdated. The installer brings it up to date when
  you answer yes to its MestReNova question (or run
  `python -m compound_registry.mnova install`). Restart MestReNova afterwards.
- **Installer options.** `install.ps1` no longer has `-DefaultHome`; the
  installer always records the data folder you choose. `-RegistryHome`,
  `-CreateRegistry`, `-Browser` and `-Force` still work. Without `-Browser`
  the installer now asks, instead of setting up Firefox only.
- **ID format of new registries.** A new registry accepts only letters, `_`,
  `-` and `.` around the numbers of its ID format, because IDs are also
  folder names. Existing registries keep their format.
- `chrome-extension.md` was replaced by `docs/browsers.md`.

[0.12.0]: https://github.com/smalljet-lab/ChemLinker/releases/tag/v0.12.0
[0.11.0]: https://github.com/smalljet-lab/ChemLinker/releases/tag/v0.11.0
[0.10.0]: https://github.com/smalljet-lab/ChemLinker/releases/tag/v0.10.0
