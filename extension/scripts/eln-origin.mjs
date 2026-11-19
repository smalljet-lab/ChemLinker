// The address of the Sciformation ELN the extension is built for.
//
// It is a build-time setting. build.mjs takes it from, in this order:
//   1. the command line:  --eln-origin https://eln.example.org
//   2. the environment:   COMPOUND_REGISTRY_ELN_ORIGIN
//   3. the file extension/local.config.json: {"elnOrigin": "https://eln.example.org"}
//      (not in the repository; the installer writes it)
//   4. DEFAULT_ELN_ORIGIN
//
// Only an https origin is accepted: scheme, host and an optional port. The
// extension assumes that Sciformation is served at the root of that host
// (links are built as <origin>/startUseCase?..., structures are downloaded
// from <origin>/zkau/...), so an address with a path is refused.
//
// Pure except for readLocalConfig(), which reads one file.
import { readFile } from "node:fs/promises";

export const DEFAULT_ELN_ORIGIN = "https://sciformation.chemie.unibas.ch";
export const ELN_ORIGIN_ENV = "COMPOUND_REGISTRY_ELN_ORIGIN";
export const LOCAL_CONFIG_FILE = "local.config.json";

// Labels separated by single dots; each starts and ends with a letter or digit.
const HOST_NAME = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/;
const IPV6_HOST = /^\[[0-9a-f:.]+\]$/;

/**
 * @param {unknown} value what the user gave
 * @param {string} from where it came from, for the error text
 * @returns {{ origin: string, matchPattern: string }} `origin` as the browser
 *   writes it (lower case, no default port); `matchPattern` for the manifest
 *   (match patterns carry no port)
 */
export function parseElnOrigin(value, from = "the ELN address") {
  const fail = (why) => {
    throw new Error(
      `${from}: ${JSON.stringify(value)} is not usable as the ELN address (${why}). ` +
        "Give the origin only, for example https://eln.example.org or https://eln.example.org:8443",
    );
  };
  if (typeof value !== "string" || value.trim() === "") fail("it is empty");
  const text = value.trim();
  if (/\s/.test(text)) fail("it contains white space");
  let url;
  try {
    url = new URL(text);
  } catch {
    fail("it is not a URL");
  }
  if (url.protocol !== "https:") fail("it must start with https://");
  if (url.username !== "" || url.password !== "" || text.includes("@")) fail("it must not contain a user name or password");
  if (text.includes("?") || text.includes("#")) fail("it must not contain a query or a fragment");
  if (url.pathname !== "/" && url.pathname !== "") {
    fail("it must not contain a path: Sciformation has to be served at the root of the host");
  }
  if (!HOST_NAME.test(url.hostname) && !IPV6_HOST.test(url.hostname)) fail("the host name is not valid");
  if (url.port === "0") fail("the port is not valid");
  return { origin: url.origin, matchPattern: `https://${url.hostname}/*` };
}

/**
 * The `elnOrigin` of extension/local.config.json, or undefined when the file
 * does not exist.
 * @param {string} path
 */
export async function readLocalConfig(path) {
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
  let config;
  try {
    config = JSON.parse(text.replace(/^\uFEFF/, ""));
  } catch {
    throw new Error(`${path} is not valid JSON; it must hold {"elnOrigin": "https://..."}`);
  }
  if (typeof config !== "object" || config === null || typeof config.elnOrigin !== "string") {
    throw new Error(`${path} must hold {"elnOrigin": "https://..."}`);
  }
  return config.elnOrigin;
}

/**
 * @param {{ arg?: string | undefined, env?: Record<string, string | undefined>, configPath?: string | undefined }} sources
 * @returns {Promise<{ origin: string, matchPattern: string, source: string }>}
 */
export async function resolveElnOrigin({ arg, env = {}, configPath } = {}) {
  if (arg !== undefined) return { ...parseElnOrigin(arg, "--eln-origin"), source: "--eln-origin" };
  const fromEnv = env[ELN_ORIGIN_ENV];
  if (fromEnv !== undefined && fromEnv !== "") return { ...parseElnOrigin(fromEnv, ELN_ORIGIN_ENV), source: ELN_ORIGIN_ENV };
  const fromFile = configPath === undefined ? undefined : await readLocalConfig(configPath);
  if (fromFile !== undefined) return { ...parseElnOrigin(fromFile, configPath), source: LOCAL_CONFIG_FILE };
  return { ...parseElnOrigin(DEFAULT_ELN_ORIGIN), source: "default" };
}

/**
 * The manifest for another ELN address: host_permissions and the matches of
 * every content script become `matchPattern`. The template must name exactly
 * the default address there, so that nothing else is ever rewritten.
 * @param {Record<string, any>} template the parsed static/manifest.json
 * @param {string} matchPattern
 * @returns {Record<string, any>}
 */
export function manifestForEln(template, matchPattern) {
  const expected = [parseElnOrigin(DEFAULT_ELN_ORIGIN).matchPattern];
  const same = (list) => Array.isArray(list) && list.length === 1 && list[0] === expected[0];
  const scripts = template.content_scripts;
  if (!same(template.host_permissions) || !Array.isArray(scripts) || scripts.length === 0 || !scripts.every((c) => same(c.matches))) {
    throw new Error(
      `static/manifest.json must name exactly ${expected[0]} in host_permissions and in the matches of every content script`,
    );
  }
  const out = structuredClone(template);
  out.host_permissions = [matchPattern];
  for (const script of out.content_scripts) script.matches = [matchPattern];
  return out;
}
