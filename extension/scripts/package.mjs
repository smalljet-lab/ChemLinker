// Rebuilds the extension and zips it into artifacts/, with manifest.json at
// the archive root:
//
//   npm run package          Firefox: dist/ -> chemlinker-<version>-firefox.xpi
//   npm run package:chrome   Chrome:  ../chrome-extension/ -> chemlinker-<version>-chrome.zip
//                            (unzip it and choose the folder in chrome://extensions > Load unpacked;
//                            the "key" in its manifest keeps the extension ID the same)
//
// Options, after `--`:
//   --force               replace an existing package of the same version
//   --eln-origin <origin> the ELN address to build for (see scripts/eln-origin.mjs)
//
// It always rebuilds first, so a package never holds an older build under a
// newer version. An existing package of the same version is never replaced
// without --force.
import { spawnSync } from "node:child_process";
import { lstat, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { createZip } from "./zip.mjs";

const here = fileURLToPath(import.meta.url);
const defaultRoot = dirname(dirname(here));

/** @param {string} dir @returns {Promise<string[]>} */
async function walk(dir) {
  const out = [];
  for (const item of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, item.name);
    if (item.isDirectory()) out.push(...(await walk(full)));
    else if (item.isFile()) out.push(full);
    else throw new Error(`not a regular file: ${full}`);
  }
  return out;
}

async function exists(path) {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

/** What differs between the two packages. `dist` is where build.mjs writes that build. */
const TARGETS = {
  firefox: {
    label: "Firefox",
    suffix: "firefox.xpi",
    buildArgs: [],
    dist: (root) => join(root, "dist"),
    /** Why `manifest` is not this target's, or null. */
    wrongBuild: (manifest) =>
      manifest.browser_specific_settings?.gecko?.id ? null : "not a Firefox build (no browser_specific_settings.gecko.id)",
  },
  chrome: {
    label: "Chrome",
    suffix: "chrome.zip",
    buildArgs: ["--chrome"],
    dist: (root) => resolve(root, "..", "chrome-extension"),
    wrongBuild: (manifest) =>
      manifest.browser_specific_settings !== undefined || !manifest.background?.service_worker || typeof manifest.key !== "string"
        ? 'not a Chrome build (it needs "key" and background.service_worker, and no browser_specific_settings)'
        : null,
  },
};

/** Runs build.mjs for the target. */
function runBuild(root, target, elnOrigin) {
  const args = [join(root, "build.mjs"), ...TARGETS[target].buildArgs];
  if (elnOrigin !== undefined) args.push("--eln-origin", elnOrigin);
  const result = spawnSync(process.execPath, args, { cwd: root, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`the build failed (exit code ${result.status}); nothing was packaged`);
}

/** Every file the manifest and the compounds page load. */
function requiredFiles(manifest) {
  const icon = manifest.action?.default_icon;
  return new Set([
    ...(manifest.background?.scripts ?? []),
    ...(manifest.background?.service_worker ? [manifest.background.service_worker] : []),
    ...(manifest.content_scripts ?? []).flatMap((c) => c.js ?? []),
    ...Object.values(manifest.icons ?? {}),
    ...(typeof icon === "string" ? [icon] : Object.values(icon ?? {})),
    "compounds.html",
    "compounds.css",
    "compounds.js",
  ]);
}

/**
 * @param {{
 *   root?: string,
 *   target?: "firefox" | "chrome",
 *   dist?: string,
 *   artifacts?: string,
 *   force?: boolean,
 *   elnOrigin?: string,
 *   build?: () => void | Promise<void>,
 *   log?: (line: string) => void,
 * }} [options] `build` must (re)write `dist`; the default runs build.mjs for
 *   the target (with `elnOrigin` as --eln-origin when given).
 * @returns {Promise<string>} the path of the written package
 */
export async function packageExtension(options = {}) {
  const root = options.root ?? defaultRoot;
  const target = options.target ?? "firefox";
  if (!Object.hasOwn(TARGETS, target)) throw new Error(`unknown package target ${JSON.stringify(target)}`);
  const kind = TARGETS[target];
  const dist = options.dist ?? kind.dist(root);
  const artifacts = options.artifacts ?? join(root, "artifacts");
  const force = options.force ?? false;
  const build = options.build ?? (() => runBuild(root, target, options.elnOrigin));
  const log = options.log ?? ((line) => console.log(line));

  const version = JSON.parse(await readFile(join(root, "package.json"), "utf8")).version;
  const out = join(artifacts, `chemlinker-${version}-${kind.suffix}`);
  if (!force && (await exists(out))) {
    throw new Error(`${out} already exists; bump the version, or pass --force to replace it`);
  }

  await build();

  const manifest = JSON.parse(await readFile(join(dist, "manifest.json"), "utf8"));
  if (manifest.version !== version) {
    throw new Error(`${dist} holds version ${manifest.version}, package.json says ${version}; nothing was packaged`);
  }
  const wrong = kind.wrongBuild(manifest);
  if (wrong !== null) throw new Error(`${dist} is ${wrong}; nothing was packaged`);

  const files = (await walk(dist))
    .map((full) => ({ full, name: relative(dist, full).split(sep).join("/") }))
    .sort((a, b) => (a.name === "manifest.json" ? -1 : b.name === "manifest.json" ? 1 : a.name < b.name ? -1 : 1));

  const names = new Set(files.map((f) => f.name));
  const missing = [...requiredFiles(manifest)].filter((name) => !names.has(name));
  if (missing.length > 0) throw new Error(`${dist} lacks ${missing.join(", ")}; nothing was packaged`);

  const entries = [];
  for (const file of files) entries.push({ name: file.name, data: await readFile(file.full) });

  const zip = createZip(entries);
  await mkdir(artifacts, { recursive: true });
  try {
    // "wx": fails if the file appeared meanwhile, so nothing is ever replaced without --force.
    await writeFile(out, zip, { flag: force ? "w" : "wx" });
  } catch (error) {
    if (error.code === "EEXIST") throw new Error(`${out} already exists; bump the version, or pass --force to replace it`);
    throw error;
  }
  log(`Wrote ${out} (${kind.label}, ${zip.length} bytes, ${entries.length} files)`);
  for (const entry of entries) log(`  ${entry.name}  ${entry.data.length} bytes`);
  return out;
}

if (process.argv[1] && resolve(process.argv[1]).toLowerCase() === resolve(here).toLowerCase()) {
  const { values } = parseArgs({
    options: {
      force: { type: "boolean", default: false },
      chrome: { type: "boolean", default: false },
      "eln-origin": { type: "string" },
    },
    strict: true,
  });
  try {
    await packageExtension({
      target: values.chrome ? "chrome" : "firefox",
      force: values.force,
      elnOrigin: values["eln-origin"],
    });
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  }
}
