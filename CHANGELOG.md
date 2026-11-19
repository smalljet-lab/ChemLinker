# Changelog

All notable changes to this project are written down in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The registry
program and the browser extension are released together under one version
number.

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

[0.10.0]: https://github.com/smalljet-lab/compound-registry/releases/tag/v0.10.0
