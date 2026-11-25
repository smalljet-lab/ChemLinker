// Bundles the background script, the content script, the page bridge, the
// compounds page, the capture page and the toolbar popup and copies the
// manifest and the pages' static files.
//
//   npm run build          Firefox: dist/ (target firefox140), packaged as the .xpi
//   npm run build:chrome   Chrome:  ../chrome-extension/ (target chrome120), a
//                          complete unpacked extension for chrome://extensions >
//                          Load unpacked, with the manifest from
//                          scripts/chrome-manifest.mjs and PNG icons
//
// `--eln-origin <origin>` sets the address of the Sciformation ELN the
// extension works on (see scripts/eln-origin.mjs for the other ways to set it
// and for what is accepted). The build writes it into the manifest
// (host_permissions and the content scripts' matches) and into the bundles
// (src/shared/site.ts), so both always agree.
//
// `--out <dir>` builds into another folder (tests). The output folder is
// deleted and recreated, so it only ever holds generated files; the build
// refuses to delete a folder that holds anything it does not generate.
import { build } from "esbuild";
import { copyFile, mkdir, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { chromeManifest } from "./scripts/chrome-manifest.mjs";
import { LOCAL_CONFIG_FILE, manifestForEln, resolveElnOrigin } from "./scripts/eln-origin.mjs";
import { ICON_SIZES, iconPath } from "./scripts/make-icons.mjs";

const root = dirname(fileURLToPath(import.meta.url));

const { values: args } = parseArgs({
  options: {
    chrome: { type: "boolean", default: false },
    out: { type: "string" },
    "eln-origin": { type: "string" },
  },
  strict: true,
});
const chrome = args.chrome;
const dist = args.out ? resolve(args.out) : chrome ? resolve(root, "..", "chrome-extension") : join(root, "dist");

/** Copied unchanged from static/ next to the bundles. */
const STATIC_FILES = [
  "compounds.html", "compounds.css", "compounds-icon.svg",
  "capture.html", "capture.css",
  "popup.html", "popup.css",
];

/**
 * In the Chrome bundles that talk to extension APIs (background, content script,
 * compounds page, capture page, popup) esbuild replaces every free `browser` identifier with `chrome`:
 * Chrome MV3's `chrome` namespace returns promises like Firefox's `browser`.
 * A compile-time replacement, not a runtime `globalThis.browser ??= chrome`:
 * Chrome has no `browser` global, so in the content script an ELN element with
 * id or name "browser" would show through as window.browser (DOM clobbering)
 * and a conditional shim would then keep that element. Never in page-bridge.js:
 * it runs in the page's own world and must not touch extension APIs.
 */
const CHROME_BROWSER_DEFINE = { browser: "chrome" };

const BUNDLES = ["background.js", "content.js", "page-bridge.js", "compounds.js", "capture.js", "popup.js"];
const ICONS = ICON_SIZES.map((size) => iconPath(size));

const pkg = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
const manifest = JSON.parse(await readFile(join(root, "static", "manifest.json"), "utf8"));
if (manifest.version !== pkg.version) {
  throw new Error(
    `static/manifest.json version ${manifest.version} differs from package.json version ${pkg.version}`,
  );
}

let eln;
try {
  eln = await resolveElnOrigin({
    arg: args["eln-origin"],
    env: process.env,
    configPath: join(root, LOCAL_CONFIG_FILE),
  });
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
}
const elnManifest = manifestForEln(manifest, eln.matchPattern);

const chromeKey = chrome ? JSON.parse(await readFile(join(root, "static", "chrome-key.json"), "utf8")) : null;
const outManifest = chrome ? chromeManifest(elnManifest, chromeKey) : elnManifest;

/**
 * Build-time settings of every bundle (read in src/shared/site.ts): the ELN
 * address, and the extension version, which also names the page bridge's
 * events so that each release talks to its own bridge.
 */
const SETTINGS_DEFINE = {
  __ELN_ORIGIN__: JSON.stringify(eln.origin),
  __EXTENSION_VERSION__: JSON.stringify(pkg.version),
};

/**
 * Refuses to delete `dir` unless it is missing, empty, or holds only files
 * this build writes (a previous, possibly unfinished, build).
 */
async function checkDeletable(dir) {
  const fromDir = relative(dir, root);
  if (fromDir === "" || (!fromDir.startsWith("..") && !isAbsolute(fromDir))) {
    throw new Error(`refusing to build into ${dir}: it contains the extension sources`);
  }
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  const generated = new Set([...BUNDLES, ...STATIC_FILES, "manifest.json", ...ICONS]);
  const foreign = [];
  for (const entry of entries) {
    if (entry.isFile() && generated.has(entry.name)) continue;
    if (entry.isDirectory() && entry.name === "icons") {
      for (const icon of await readdir(join(dir, "icons"), { withFileTypes: true })) {
        if (!(icon.isFile() && generated.has(`icons/${icon.name}`))) foreign.push(`icons/${icon.name}`);
      }
      continue;
    }
    foreign.push(entry.name);
  }
  if (foreign.length > 0) {
    throw new Error(`refusing to delete ${dir}: it holds files this build does not generate (${foreign.join(", ")})`);
  }
}

await checkDeletable(dist);
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });

const common = {
  bundle: true,
  format: "iife",
  platform: "browser",
  target: [chrome ? "chrome120" : "firefox140"],
  minify: false,
  sourcemap: false,
  charset: "utf8",
  legalComments: "none",
  logLevel: "info",
  metafile: true,
};

/** Options of the bundles that use extension APIs. */
const extensionApi = { define: chrome ? { ...SETTINGS_DEFINE, ...CHROME_BROWSER_DEFINE } : SETTINGS_DEFINE };
/** Options of the page bridge: the settings only. */
const pageWorld = { define: SETTINGS_DEFINE };

/** OpenChemLib (the structure editor) belongs in compounds.js and capture.js and nowhere else. */
const OCL = /node_modules[\\/]openchemlib[\\/]/;
function checkOcl(result, outfile, wanted) {
  const has = Object.keys(result.metafile.inputs).some((input) => OCL.test(input));
  if (has !== wanted) {
    throw new Error(`${outfile} ${wanted ? "lacks" : "must not contain"} openchemlib`);
  }
}

async function bundle(entry, outfile, options = {}, oclWanted = false) {
  const result = await build({ ...common, ...options, entryPoints: [join(root, ...entry)], outfile: join(dist, outfile) });
  checkOcl(result, outfile, oclWanted);
}

await bundle(["src", "background", "index.ts"], "background.js", extensionApi);
await bundle(["src", "content", "index.ts"], "content.js", extensionApi);
await bundle(["src", "page", "index.ts"], "page-bridge.js", pageWorld);
await bundle(
  ["src", "compounds", "index.ts"],
  "compounds.js",
  {
    ...extensionApi,
    // openchemlib reads `import.meta` only in `Resources.registerFromUrl` /
    // `registerFromNodejs` (3D and toxicity data), which this page never calls;
    // in an IIFE bundle `import.meta` is empty, which is what we want.
    logOverride: { "empty-import-meta": "silent" },
  },
  true,
);
await bundle(
  ["src", "capture", "index.ts"],
  "capture.js",
  // The same openchemlib as in compounds.js: see there.
  { ...extensionApi, logOverride: { "empty-import-meta": "silent" } },
  true,
);
await bundle(["src", "popup", "index.ts"], "popup.js", extensionApi);

for (const name of STATIC_FILES) await copyFile(join(root, "static", name), join(dist, name));
if (chrome) {
  await mkdir(join(dist, "icons"), { recursive: true });
  for (const icon of ICONS) await copyFile(join(root, "static", ...icon.split("/")), join(dist, ...icon.split("/")));
}
await writeFile(join(dist, "manifest.json"), `${JSON.stringify(outManifest, null, 2)}\n`);

// Everything the manifest and the extension's pages load must be there.
const required = new Set([
  ...(outManifest.background.scripts ?? []),
  ...(outManifest.background.service_worker ? [outManifest.background.service_worker] : []),
  ...outManifest.content_scripts.flatMap((c) => c.js ?? []),
  ...Object.values(outManifest.icons ?? {}),
  ...(typeof outManifest.action?.default_icon === "string"
    ? [outManifest.action.default_icon]
    : Object.values(outManifest.action?.default_icon ?? {})),
  ...(outManifest.action?.default_popup ? [outManifest.action.default_popup] : []),
  "compounds.html",
  "compounds.css",
  "compounds.js",
  "capture.html",
  "capture.css",
  "capture.js",
  "popup.html",
  "popup.css",
  "popup.js",
]);
for (const name of required) {
  const info = await stat(join(dist, ...name.split("/"))).catch(() => null);
  if (!info?.isFile()) throw new Error(`the build lacks ${name}`);
}

console.log(
  `Built ${manifest.name} ${manifest.version} for ${chrome ? "Chrome" : "Firefox"} into ${dist}\n` +
    `ELN address: ${eln.origin} (${eln.source === "default" ? "the default" : `from ${eln.source}`})`,
);
