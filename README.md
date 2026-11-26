# ChemLinker

A local registry that gives every compound you make a permanent ID of the form
`CORE_STEREO_BATCH`, for example `ABC000042_01_003`: compound 42, its first
stereo variant, the third batch. A browser extension adds a panel to the
Sciformation ELN. The panel detects the products of the experiment you have
open and looks up or creates their IDs with one click.

The same program keeps a searchable catalogue of all your compounds: text and
structure search, names from ChemDraw, yields, the synthesis route of a
compound as a ChemDraw file, NMR files saved from MestReNova into the
compound's folder, and a bulk import from the ELN's exported experiment list
(CSV).

Everything stays on your PC. There is no server and no account. The extension
talks only to the ELN page you have open and to the registry program on the
same PC. The registry is one folder that you choose and back up.

## Requirements

| What | Needed |
| --- | --- |
| Windows 10 or 11 | Fully supported. |
| macOS, Linux | Not supported yet. The registry, the extension and the command-line tool are written to work there, but this has not been tried and the automatic tests run on Windows only. The ChemDraw and MestReNova integration are Windows only. |
| Browser | Firefox 140 or newer, or Chrome 120 or newer. Read [which Firefox](#3-load-the-extension-in-the-browser) before you start. |
| ELN | Sciformation, served over https at the root of its host. The default address is `https://sciformation.chemie.unibas.ch`; any other address is a setting, see [Using another Sciformation address](#using-another-sciformation-address). |
| uv | The Python package manager the project uses. The installer offers to install it. You do not need to install Python yourself. |
| Node.js | Only to build the extension: version 20.19 or newer of the 20 series, or 22.12 or newer (the current LTS version from <https://nodejs.org> is fine). Not needed if you use the [ready-made packages](#ready-made-extension-packages). |
| Internet | During installation and updates only (Python packages, build tools). |
| ChemDraw (optional) | Desktop ChemDraw on Windows, for compound names and for opening synthesis routes. Tried with ChemDraw 26. |
| MestReNova (optional) | On Windows, for the NMR add-on. Tried with MestReNova 17. |
| Reading reactions from the screen (optional) | An extra part of about 2.8 GB on disk that uses about 2.5 GB of memory while it runs. No graphics card is needed. See [Capture a reaction from the screen](#capture-a-reaction-from-the-screen-optional). |

No administrator rights are needed. Everything is installed for the current
Windows user only.

## Install on Windows

### 1. Get the project folder

Either download the ZIP from
<https://github.com/smalljet-lab/ChemLinker> (green **Code** button,
**Download ZIP**) and unpack it, or clone it:

```
git clone https://github.com/smalljet-lab/ChemLinker.git
```

Put the folder where it can stay, for example `C:\Users\<you>\compound-registry`.
The program runs from this folder. If you move or delete it later, the browser
no longer finds the program and you have to run the installer again.

The project folder holds the program. It is not the place for your data; you
choose the data folder in the next step.

### 2. Run the installer

Double-click `install.cmd`. A window opens and asks a few questions. Every
question shows its default in brackets; press Enter to accept it.

If uv is not on the PC yet, the installer first asks `Install uv now? [y/N]`.
Answer `y`.

Then the questions come in this order:

1. **Data folder.** The folder for the registry database, its backups and the
   compound files. The default is `CompoundRegistry` in your user folder.
   - Use a folder on a local disk. Do not use a OneDrive, Dropbox, Google
     Drive or iCloud folder, or a network drive: sync programs can damage a
     database that is in use. The installer warns and asks
     `Use this folder anyway?` if it sees such a folder.
   - One registry per person is the normal case. Share one registry between
     people only if you really want one common numbering, and then never let
     two PCs write to it through a synced folder.
   - Every registry needs a backup folder of its own. Do not point two
     registries at the same backup folder.
   - If no registry exists in the folder, the installer asks
     `Create a new, empty registry?`. Say yes only for a first installation.
     If you already have a registry somewhere else, give that folder instead.
   - For a new registry it asks for the **first three letters of the compound
     IDs**: three capital letters from A to Z, for example your initials
     (`PET` gives `PET000001_01_001`). Small letters are turned into capitals.
     The letters can never be changed afterwards.
2. **Browser.** `firefox`, `chrome` or `both`.
3. **ELN address.** Press Enter for the default, or type the address of your
   own Sciformation.
4. **MestReNova add-on.** Asked only when MestReNova was found on the PC.
5. **ChemResolver.** The optional part behind "Capture reaction": it reads a
   drawn reaction scheme from a picture of the screen. The installer asks
   `Install ChemResolver (about 2.8 GB)?`; the default is no. It needs about
   2.8 GB on disk, most of it downloaded during the installation, and can be
   added later with `compound-registry install-recognition`. See
   [Capture a reaction from the screen](#capture-a-reaction-from-the-screen-optional).
6. **Continue?** The installer shows what it will do. Nothing has been
   changed before this point.

The installer then creates the registry (if you said so), registers the
registry program with the browser, builds the extension and ends with a list
headed `What to do now:`. That list contains the exact file and folder names
for the next step.

If Node.js is missing or too old, everything except the extension is
installed and the installer says so. Install Node.js and run `install.cmd`
again, or use the [ready-made packages](#ready-made-extension-packages).

To see what the installer would do without changing anything, run in a
PowerShell window in the project folder:

```
powershell -ExecutionPolicy Bypass -File install.ps1 --dry-run
```

### 3. Load the extension in the browser

**Firefox.** The extension file is
`extension\artifacts\chemlinker-<version>-firefox.xpi`. It is not signed
by Mozilla, and this decides what works:

- **Firefox ESR, Developer Edition and Nightly** can install it for good. Open
  `about:config`, set `xpinstall.signatures.required` to `false` (once). Then
  open `about:addons`, click the gear menu, choose **Install Add-on From
  File...** and pick the `.xpi` file.
- **Ordinary Firefox (release and beta)** refuses an unsigned file. There the
  extension can only be loaded until Firefox is closed: open
  `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on...**
  and choose `extension\dist\manifest.json`. You have to repeat this after
  every start of Firefox. If you want to stay with ordinary Firefox, your
  group can have the file signed with its own Mozilla account; see
  [docs/browsers.md](docs/browsers.md).

**Chrome.** Open `chrome://extensions`, switch on **Developer mode**, click
**Load unpacked** and choose the folder `chrome-extension` in the project
folder.

Finally, reload the ELN tabs that are open.

## Install on macOS or Linux

In a terminal, in the project folder:

```
sh install.sh
```

The questions and the steps are the same as on Windows. Differences:

- The MestReNova add-on and the ChemDraw functions are not available.
- "Chrome" means Google Chrome. Chromium, Edge and Brave are not set up.
- A Firefox installed as a snap or flatpak package runs in a sandbox and may
  not be able to start the registry program. If the panel cannot reach the
  program, use a Firefox installed the ordinary way.

## Ready-made extension packages

If you do not want to install Node.js, download the two extension packages
from the Releases page,
<https://github.com/smalljet-lab/ChemLinker/releases>:

- `chemlinker-<version>-firefox.xpi` for Firefox. Install it as
  described above.
- `chemlinker-<version>-chrome.zip` for Chrome. Unpack it into a folder
  that can stay, and choose that folder with **Load unpacked**.

These packages work only with the default ELN address
(`https://sciformation.chemie.unibas.ch`). You still have to run the installer:
it installs the registry program and registers it with the browser. Take the
packages of the same version as the program.

## Using another Sciformation address

The extension runs on exactly one ELN address, and the address is built into
it. Answer the installer's **ELN address** question with your address, or pass
it as an option:

```
powershell -ExecutionPolicy Bypass -File install.ps1 --eln-origin https://eln.example.org
```

Rules: the address must start with `https://`, it may have a port
(`https://eln.example.org:8443`), and it must not have a path, because
Sciformation has to be at the root of the host. The installer saves the address
in `extension\local.config.json` and updates keep it. For another address you
need Node.js, because the extension has to be built on your PC.

## Update

1. Close Firefox, Chrome and MestReNova. The program file cannot be replaced
   while a browser has it open. On Windows the update tells you and waits.
2. Get the new version:
   - if you cloned with git, the update script fetches it itself;
   - if you downloaded the ZIP, download the new ZIP and unpack it over the
     project folder, replacing the files.
3. Double-click `update.cmd` (or run `update.ps1`; on macOS/Linux
   `sh update.sh`).

The update asks nothing. It keeps your registry, your choice of browsers, the
ELN address and, if it is installed, the MestReNova add-on (which it brings up
to date). The registry itself (database, journal, backups, compound files) is
never replaced.

What you still have to do by hand is listed at the end of the update:

- **Firefox:** install the new `.xpi` file, the same way as the first time.
- **Chrome:** open `chrome://extensions` and click the reload arrow of
  "ChemLinker".
- Reload the open ELN tabs.
- Restart MestReNova if you use the add-on.

The update prints a line that starts with `Registry check:`. The program
recognises a compound by keys computed with a chemistry library. When an
update brings a new version of that library, the update recomputes the keys of
every stored structure:

- `The chemistry version did not change; nothing to check.` Nothing to do.
- `... structure(s) checked, keys unchanged, new chemistry version accepted`
  Nothing to do.
- `... no longer give the same keys ...` Creating IDs stays blocked, because a
  known compound could get a second ID. Nothing in the registry was changed.
  Install the previous program version again and report the problem with the
  output of `compound-registry rekey-check`.

If the panel later shows "Extension and registry program are from different
releases", the extension in the browser is older or newer than the program:
run the update and reload the extension.

## First use

1. Open an experiment in the ELN. A small button labelled **ID** appears on
   the page. Click it to open the panel.
2. The panel shows one card per product of the experiment and looks each one
   up. A card says whether the product is a new compound, a new stereo variant
   of a known compound, or a new batch of a known one.
3. Click **Create ID**, check the summary, then click **Create**. The ID is
   permanent from this moment. Copy it into the ELN and your notebook.
4. Click **All compounds** in the panel (or the extension's toolbar button,
   then **Open compound catalogue**) to open the catalogue. It has three views: **Compounds** (search by ID, name,
   formula, ELN number, SMILES, InChI or a drawn structure), **Yields** and
   **Import**.
5. In the details of a compound you find its batches and experiments, its
   name (**Get name from ChemDraw** if ChemDraw is installed), its spectra and
   files, and **Generate ChemDraw tree**, which draws the synthesis route.
6. In MestReNova, the add-on is in the menu **Scripts > ChemLinker**.
   **Link Spectrum to Compound ID...** assigns the open spectrum to an ID and
   **Save Report and PDF to Compound** saves it into the compound's folder.
   For the spectrum of a crude product, tick **Crude spectrum** in the dialog
   that asks for the compound: the document is saved as
   `<ID>_<sample>_crude.mnova`, without PDF and report, and the catalogue
   shows only where that file is. For the spectrum of an undesired side
   product, tick **Undesired side product** below it: the document is saved
   as `<ID>_<sample>_side.mnova`, also without PDF and report, and the
   catalogue lists its path under **Undesired side product**.
7. To register many old experiments at once, export the experiment list from
   the ELN as a CSV file and use the **Import** view. **Check only (creates
   nothing)** shows what would happen first.

An ID cannot be deleted or reused. A wrongly created ID is retired in a
terminal, see below.

## Capture a reaction from the screen (optional)

**Capture reaction** in the extension's toolbar button reads a drawn reaction
scheme from the browser tab you have open (a paper, a PDF, a slide): you drag
a box around the scheme and get every molecule as SMILES and as a structure
you can edit, the reaction SMILES, the text at the arrow, and a button that
opens the reaction in ChemDraw. It needs no registry and creates no IDs.

How to use it:

1. Show the reaction scheme in a browser tab (a web page or a PDF opened in
   the browser) and scroll so that the whole scheme is visible.
2. Click the ChemLinker button in the toolbar and choose **Capture
   reaction**, or press **Alt+Shift+R**. (The shortcut can be changed in the
   browser's settings for add-on shortcuts. On some pages only the button
   works.) A new tab opens with a picture of what the tab showed.
3. Drag a box around the scheme (**Whole picture** takes everything), choose
   **Reaction** or **Single molecule**, and press **Recognise**. The first
   time after the browser started this takes longer, because the models are
   loaded.
4. Check the result against the picture: every molecule has a numbered box
   in the picture and a card with the redrawn structure, its SMILES and
   warnings. **Edit** corrects a structure in the built-in editor; role and
   step of each molecule and the text at the arrow can be changed.
5. **Copy** a SMILES, **Copy all**, or **Open in ChemDraw**.

To take the picture the extension has the permission `activeTab`: it may
photograph the tab you are looking at, only at the moment you choose
**Capture reaction** or press the shortcut. It cannot read other tabs or the
content of web pages. In Chrome, a tab in an Incognito window cannot be
captured.

This needs an extra part that the installer does not install, because it is
large. Install it once, in a terminal in the project folder:

```
.venv\Scripts\compound-registry.exe install-recognition
```

- **What it is.** Three open-source programs that run on your PC: RxnScribe
  finds the reactions in the picture, MolScribe reads each molecule, EasyOCR
  reads the text. They get a Python environment of their own; the registry
  program itself is not changed.
- **Size.** About 2.8 GB on disk (1.2 GB programs, 1.6 GB model files), in
  `%LOCALAPPDATA%\CompoundRegistry\recognition`. While it runs it uses about
  2.5 GB of memory. No graphics card is needed.
- **Offline.** The internet is needed for the installation only (the command
  downloads from PyPI, pytorch.org, GitHub and huggingface.co and checks the
  model files against known checksums). Afterwards nothing is downloaded and
  nothing leaves your PC: no picture, no structure, no text. The screenshot
  is kept in memory only.
- **Speed.** The first capture after starting the browser loads the models:
  20 to 35 seconds, a few minutes the very first time. After that a scheme
  takes about 10 to 20 seconds. The program stops by itself after 10 minutes
  without use and when the browser closes.
- **The result is a draft.** About 9 of 10 molecules are read correctly from
  a clean drawing, about 7 of 10 from a figure in a paper, and a wrong answer
  often looks plausible. The page marks what to look at (stereo and
  double-bond geometry, salts, isotope labels, unusual elements,
  placeholders, a low score of the program), and text at the arrow is often
  misread. A reagent that is written as text over the arrow is not a
  molecule and is missing from the reaction SMILES. Check every structure
  before you use it.

The command can be interrupted and run again; it continues where it stopped.
If you already have the model files, `--models-from <folder>` copies them
instead of downloading. `doctor` shows whether the part is installed.
`install-recognition --uninstall` removes it again.

Run the command again in two cases: after an update of ChemLinker, when the
capture page says the part was installed for another version; and after you
moved or renamed the ChemLinker folder (the part uses the Python inside that
folder; the command makes its environment again and keeps the model files).

## What counts as the same compound

The ID has three parts. `CORE` is the compound without its stereochemistry,
`STEREO` is the stereo variant, and `BATCH` counts every time it was made.

- Same compound means exactly the same compound. Salts and solvates are
  different compounds: free base, HCl salt and TFA salt each get their own
  `CORE`. Isotopically labelled compounds too.
- Stereoisomers share a `CORE` and get different `STEREO` numbers. "Absolute
  as drawn", "racemic", "relative" and "undefined" are different variants,
  even when the drawing looks the same. You choose the stereo mode in the
  panel.
- Different ways of drawing the same compound give the same ID (aromatic or
  Kekule, explicit or implicit hydrogens, a salt drawn as ion pair or as acid
  plus base).
- If a wedge is drawn that the ID cannot distinguish, the panel says so and
  asks you to accept it before it creates an ID.

Known limits:

- Keto/enol and ring-chain tautomers are different compounds. Only
  heteroatom mobile-hydrogen tautomers are merged.
- Bonds to metals are disconnected, and stereo at metal centres, planar and
  helical chirality are not described.
- Allene and spiro axial chirality and stereogenic nitrogen are not
  distinguished.
- An atropisomer is recognised only when the axis is drawn with a wedge on a
  ring bond that starts at an axis atom. Some axes are refused.
- Abbreviations (such as "Ph" or "Boc" as a label), R-groups and polymer or
  mixture brackets are refused. Draw the full structure.

## Where your data is

The data folder you chose holds everything:

| File or folder | What it is |
| --- | --- |
| `config.toml` | Settings: ID format, database path, backup folder, compound folder. The comments in the file explain each one. |
| `registry.sqlite` | The database. |
| `issued-ids.jsonl` | The journal: one line for every ID ever issued or retired, kept outside the database. |
| `backups\` | Backups named `registry-YYYYMMDD-HHMMSS.sqlite` (UTC) and a second copy of the journal. |
| `compounds\` | One folder per compound and stereo variant (`CORE_STEREO`), with its structure as a `.mol` file, the files saved from MestReNova and the route drawings. |
| `logs\host.log` | Log of the registry program. It contains no structures. |

**Backups** are made automatically. After an ID is created or other data is
stored, the program writes a new backup if the last one is older than 24
hours. It keeps the newest 30. Both numbers are in `config.toml`
(`interval_hours`, `keep`). Every backup is checked before it appears under its
final name. The backup folder may be a synced folder (for example OneDrive),
and that is a good way to get a copy off the PC: set `[backup] folder` in
`config.toml`. The compound folder may be a synced folder too. Only the
database must stay on a local disk.

**The journal** protects you after a restore. If the database is ever replaced
by an older backup, it has forgotten the IDs issued since then and could hand
them out a second time. The program notices this from the journal and blocks
new IDs until you run `compound-registry recover`, which re-creates the
missing IDs exactly as they were.

**To restore a backup:** close the browsers and MestReNova, copy the backup
file to the place of `registry.sqlite`, then run `compound-registry recover`.

### The command-line tool

The installer creates the tool inside the project folder:
`.venv\Scripts\compound-registry.exe` on Windows, `.venv/bin/compound-registry`
on macOS/Linux. Open a terminal in the project folder to use it. If your data
folder is not the default (`CompoundRegistry` in your user folder), put
`--home <data folder>` before the command.

```
.venv\Scripts\compound-registry.exe host-status
.venv\Scripts\compound-registry.exe --home D:\CompoundRegistry doctor
.venv\Scripts\compound-registry.exe --home D:\CompoundRegistry backup
.venv\Scripts\compound-registry.exe --home D:\CompoundRegistry backup --list
```

- `host-status` shows how the program is registered with Firefox and Chrome
  and which registry the extension uses. It works even when the registry is
  missing. Start here when the panel cannot reach the program.
- `doctor` shows the configuration and the health of the registry: paths,
  number of records, integrity, backups, journal, versions, warnings.
- `backup` writes a backup now. `backup --list` lists the backups and
  `backup --verify FILE` checks one.
- `retire <ID> --reason "<why>"` retires a wrongly created ID. The ID stays
  recorded and is never given to another compound.
- `export --format csv --output FILE` (or `--format sdf`) exports all batches.

`compound-registry --help` lists every command.

## Troubleshooting

The panel always shows the program's own message and an error code. The most
common ones:

| Message in the panel | What to do |
| --- | --- |
| **Registry program not reachable** | The program is not registered with this browser, or the project folder was moved. Run `compound-registry host-status`, then run the installer again and press Retry. |
| **The registry does not exist** | The data folder or the database is not where the program expects it (for example a drive that is not connected). The program never creates a registry by itself. Reconnect the drive, or run the installer and give the right folder. Do not create a new registry unless you really have none. |
| **Extension and registry program do not match** / **... are from different releases** / **Operation not supported by the registry program** | Extension and program come from different versions. Run the update, then reload or reinstall the extension. |
| **The registry program refuses this extension** | The extension does not have the expected ID. Use the packages built by this project; in Chrome, load the `chrome-extension` folder made by the build. |
| **The database is older than the issued-ID journal** | The database was restored from an older backup. Run `compound-registry recover`. |
| **The chemistry software changed** | Creating IDs is blocked until the stored structures have been checked. Run the installer again, or `compound-registry rekey-check --accept`. |
| **No answer from the registry program** | It may still be starting. Try again in a moment. |
| **Product slot already taken** | This product position of the experiment already has an ID for another compound. Retire that ID first (`compound-registry retire`). |
| **Internal error in the registry program** | Look at `logs\host.log` in the data folder. |

Other situations:

- **The panel does not appear in the ELN.** Reload the ELN tab after loading
  or reloading the extension. Check that the extension was built for the
  address your ELN has.
- **The extension is gone after restarting Firefox.** It was loaded as a
  temporary add-on. See [which Firefox](#3-load-the-extension-in-the-browser).
- **A message contains `journal_shared` or "Give each registry its own backup
  folder".** Two registries use the same backup folder (or the same data
  folder). Set `[backup] folder` in `config.toml` to a folder that only this
  registry uses. Do not run `recover` for this.
- **`uv sync failed` and "access is denied" or "file is in use" during
  installation or update.** Close Firefox, Chrome and MestReNova and run it
  again.
- **`update.cmd` stops with "Program files in this folder were changed
  locally".** The project folder is a git checkout with changed files. Undo
  the changes, or run `update.ps1 -NoPull` to update with the files as they
  are.
- **The MestReNova add-on says it is outdated.** Run the update (or the
  installer), then restart MestReNova.
- **`doctor` says "the extension uses a DIFFERENT registry than this
  command".** You forgot `--home <data folder>`.

## Uninstall

In a terminal in the project folder:

```
.venv\Scripts\compound-registry.exe uninstall-host --browser both
.venv\Scripts\python.exe -m compound_registry.mnova uninstall
```

The first command removes the registration with the browsers. The second
removes the MestReNova add-on (Windows only). If you installed the optional
part that reads reactions from the screen, remove it with
`.venv\Scripts\compound-registry.exe install-recognition --uninstall` (it is
not inside the project folder). Then remove the extension in the
browser (`about:addons` or `chrome://extensions`) and delete the project
folder.

Your data folder is never deleted by any of this. Keep it, or delete it
yourself when you are sure that you no longer need the IDs.

## For developers

The project has a Python package (`src/compound_registry`), a TypeScript
browser extension (`extension/`) and a MestReNova script. See:

- [docs/development.md](docs/development.md): repository layout, development
  setup, tests, how the parts talk to each other, releasing, and the rules for
  compatible upgrades.
- [docs/browsers.md](docs/browsers.md): the Firefox and Chrome builds,
  packaging, the fixed Chrome extension ID, signing for ordinary Firefox.
- [CHANGELOG.md](CHANGELOG.md): what changed in each version.

## Licence

MIT. See [LICENSE](LICENSE).
