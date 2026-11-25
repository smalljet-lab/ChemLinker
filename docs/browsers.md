# Browsers: the Firefox and Chrome builds

The installer builds and packages the extension for you. This page explains
what it does, for people who build by hand, use another ELN address, or want
to sign the Firefox package.

## One source tree, two builds

The extension has one source tree, `extension/`. It is built twice:

| Browser | Build | Output | Package |
| --- | --- | --- | --- |
| Firefox 140+ | `npm run build` | `extension/dist/` | `npm run package` writes `extension/artifacts/chemlinker-<version>-firefox.xpi` |
| Chrome 120+ | `npm run build:chrome` | `chrome-extension/` in the project folder | `npm run package:chrome` writes `extension/artifacts/chemlinker-<version>-chrome.zip` |

The Firefox manifest is `extension/static/manifest.json`. The Chrome manifest
is derived from it at build time by `extension/scripts/chrome-manifest.mjs`
(service worker instead of background script, PNG icons, the fixed `key`).

`extension/dist/`, `extension/artifacts/` and `chrome-extension/` are build
outputs. They are deleted and recreated by every build and are not in git.
Never put your own files there.

## Build and package commands

Node.js is needed: version 20.19 or newer of the 20 series, or 22.12 or newer
(`engines` in `extension/package.json`).

```
cd extension
npm ci                     # once, and again after an update changed package-lock.json
npm run package            # Firefox: rebuilds dist/ and writes the .xpi
npm run package:chrome     # Chrome: rebuilds ../chrome-extension/ and writes the .zip
```

`npm run build` and `npm run build:chrome` build without packaging.

Options go after `--`:

| Option | For | Meaning |
| --- | --- | --- |
| `--eln-origin <origin>` | all four scripts | The ELN address to build for, for example `https://eln.example.org`. |
| `--force` | `package`, `package:chrome` | Replace an existing package of the same version. Without it, an existing package is never overwritten. |

Example:

```
npm run package -- --force --eln-origin https://eln.example.org
```

A package script always rebuilds first, so a package never contains an older
build.

## The ELN address

The extension runs on exactly one address. The build writes it into the
manifest (`host_permissions` and the content scripts' `matches`) and into the
scripts. The build takes it from, in this order:

1. the option `--eln-origin`;
2. the environment variable `COMPOUND_REGISTRY_ELN_ORIGIN`;
3. the file `extension/local.config.json`, which holds
   `{"elnOrigin": "https://eln.example.org"}`. The installer writes this file;
   it is not in git;
4. the default, `https://sciformation.chemie.unibas.ch`.

The build prints the address it used (`ELN address: ...`). Only an https
origin is accepted: `https://host` or `https://host:port`. An address with a
path is refused, because the extension expects Sciformation at the root of the
host. An invalid address stops the build with one line of explanation and
writes nothing.

The packages on the GitHub Releases page are built for the default address.

## Loading the extension

**Firefox ESR, Developer Edition, Nightly:** set
`xpinstall.signatures.required` to `false` in `about:config`, then install the
`.xpi` with `about:addons`, gear menu, **Install Add-on From File...**. The
extension stays installed.

**Ordinary Firefox (release, beta):** it ignores that setting and refuses an
unsigned file. Load `extension/dist/manifest.json` with
`about:debugging#/runtime/this-firefox`, **Load Temporary Add-on...**. The
extension is gone when Firefox closes. The alternative is a signed package,
see below.

**Chrome:** `chrome://extensions`, switch on **Developer mode**, **Load
unpacked**, choose the `chrome-extension` folder (the folder that holds
`manifest.json`). If you use the `.zip` from the Releases page, unpack it
first and choose the unpacked folder. After a rebuild, click the reload arrow
on the extension's card.

In both browsers, reload the open ELN tabs after loading or reloading the
extension.

## The toolbar button, the shortcut and the permissions

The toolbar button opens a small popup (`popup.html`) with two entries:
**Capture reaction** and **Open compound catalogue**. The manifest also
defines the keyboard shortcut Alt+Shift+R for **Capture reaction**
(`commands`). Change it in Firefox under `about:addons`, gear menu, **Manage
Extension Shortcuts**, and in Chrome under `chrome://extensions/shortcuts`.

The extension has exactly two permissions, in both browsers:

| Permission | Why |
| --- | --- |
| `nativeMessaging` | To talk to the registry program on this PC. |
| `activeTab` | For **Capture reaction**: `tabs.captureVisibleTab` takes a picture of the tab you are looking at. The permission is given by the browser only at the moment you click the button's entry or press the shortcut, and only for that tab. |

Besides these it has access to the one ELN address (`host_permissions`).
**Capture reaction** adds no content script, no access to other addresses and
no `scripting` permission: the picture is shown on the extension's own page
(`capture.html`), the web page itself is never touched. The picture is kept
in the memory of the background script until that page has fetched it (once,
at most 60 seconds) and is never stored.

Reading the picture needs the optional recognition part of the registry
program (about 2.8 GB, `compound-registry install-recognition`, see the
README). It runs on this PC only; its result is a draft that the user checks
on the capture page. Without it the capture page explains how to install it.

Limits: the browsers' own pages (settings, the new tab page, the add-on
store) cannot be captured. In Chrome a tab in an Incognito window is refused,
because the capture page could only open in a normal window.

## The fixed Chrome extension ID

Chrome gives an unpacked extension an ID. Normally the ID depends on the
folder path, so it would differ on every PC. The registry program must know
the ID, because it answers only this one extension. Therefore the Chrome
manifest contains a `key` (a public key, stored in
`extension/static/chrome-key.json`). Chrome derives the ID from the key, so
the ID is the same everywhere:

```
imkbdobpgmdgbpfeencchphiegjledoj
```

The same ID is written in `src/compound_registry/protocol.py`
(`CHROME_EXTENSION_ID`) and `extension/src/shared/protocol.ts`. The Chrome
registration made by the installer allows only
`chrome-extension://imkbdobpgmdgbpfeencchphiegjledoj/`. The build checks that
the key gives this ID. No private key is needed and none is in the
repository.

If the extension shows another ID in `chrome://extensions`, the folder was not
made by `npm run build:chrome`, and the registry program will refuse it.

The Firefox add-on ID is `compound-registry@registry.local`
(`browser_specific_settings.gecko.id` in `extension/static/manifest.json`,
`EXTENSION_ID` in the two protocol files).

## Both browsers use one registry

Each browser needs its own registration of the registry program (a "native
messaging host"). The installer writes it; the low-level command is
`compound-registry install-host --browser firefox|chrome|both`.

Where the registration is:

| System | Firefox | Chrome |
| --- | --- | --- |
| Windows | manifest in `%LOCALAPPDATA%\CompoundRegistry\native-host`, pointer in `HKCU\Software\Mozilla\NativeMessagingHosts\compound_registry` | manifest in the same folder, pointer in `HKCU\Software\Google\Chrome\NativeMessagingHosts\compound_registry` |
| macOS | `~/Library/Application Support/Mozilla/NativeMessagingHosts/compound_registry.json` | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/compound_registry.json` |
| Linux | `~/.mozilla/native-messaging-hosts/compound_registry.json` | `$XDG_CONFIG_HOME/google-chrome/NativeMessagingHosts/compound_registry.json` |

Both browsers share one host configuration file,
`compound_registry.config.json`, which names the data folder. So there is one
registry and one sequence of IDs: an ID created from Chrome is the ID you see
from Firefox. Consequences:

- Registering Chrome alone never changes which registry is used. To switch
  the registry, register both browsers (the installer does this for you).
- If the registry cannot be found, the program refuses to work
  ("The registry does not exist"). It never opens or creates another one.

`compound-registry host-status` shows the registration of both browsers and
what is wrong with it.

On macOS and Linux only Google Chrome is set up, not Chromium, Edge or Brave.

## Optional: a signed package for ordinary Firefox

Ordinary Firefox installs only add-ons that Mozilla has signed. A group that
wants a permanent installation in ordinary Firefox can have its own build
signed. Mozilla offers this for add-ons that are not listed in its public
catalogue ("unlisted", self-distributed): you submit the package with a
Mozilla account, through the developer pages of <https://addons.mozilla.org>
or with Mozilla's `web-ext` command-line tool (`web-ext sign`), and get back a
signed `.xpi` that you distribute yourself.

This project does not do the signing and has not been through that process.
What follows from the code:

- The add-on ID must be your group's own. Change
  `browser_specific_settings.gecko.id` in `extension/static/manifest.json` to
  an ID that belongs to your group. An add-on ID can be registered with
  Mozilla only once, so the ID in this repository cannot be shared by every
  group.
- The registry program answers only the add-on ID it knows. Change
  `EXTENSION_ID` in `src/compound_registry/protocol.py` and in
  `extension/src/shared/protocol.ts` to the same new ID, then run the
  installer again so that the browser registration names the new ID. The
  extension tests check that the three places agree.
- A signed package cannot be changed afterwards. Build it for the right ELN
  address before you submit it, and sign again for every new version.
- Mozilla's rules and steps change over time. Follow Mozilla's current
  documentation ("Extension Workshop") for the details.

Keeping your own ID means keeping a small fork: the three changed lines have
to be kept at every update.

## If something does not work

- **"Registry program not reachable"**: the program is not registered with
  this browser. Run the installer, or `compound-registry host-status` to see
  what is missing.
- **"The registry program refuses this extension"**: the extension does not
  have the expected ID (see above).
- **The panel does not appear in the ELN**: reload the ELN tab. Check the
  `ELN address:` line of the build.
- **"Extension and registry program are from different releases"**: rebuild
  and reload the extension after an update, or run the update script.
