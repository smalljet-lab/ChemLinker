// The Chrome manifest, derived from the Firefox one (static/manifest.json) and
// the fixed key (static/chrome-key.json). Pure: no file access. build.mjs
// writes the result into chrome-extension/manifest.json.
//
// Kept unchanged: name, version, description, permissions, host_permissions,
// content_scripts. Changed: no browser_specific_settings; the background is a
// service worker; the toolbar button has PNG icons and no default_area.
// Added: "key" (fixes the extension ID the native host accepts), "icons" and
// "minimum_chrome_version". Nothing else: no new permission, no
// web_accessible_resources, no externally_connectable.
import { createHash } from "node:crypto";
import { ICON_SIZES, iconPath } from "./make-icons.mjs";

export const MINIMUM_CHROME_VERSION = "120";

/** The keys of static/manifest.json this transform knows. Any other key is an error, never silently copied or dropped. */
const KNOWN_KEYS = [
  "manifest_version",
  "name",
  "version",
  "description",
  "browser_specific_settings",
  "permissions",
  "host_permissions",
  "action",
  "background",
  "content_scripts",
];

const KNOWN_ACTION_KEYS = ["default_title", "default_icon", "default_area"];

/** {"16": "icons/icon-16.png", ...} */
export function iconMap() {
  return Object.fromEntries(ICON_SIZES.map((size) => [String(size), iconPath(size)]));
}

/**
 * The extension ID Chrome derives from a manifest "key": the first 128 bits of
 * the SHA-256 of the DER public key, written with the letters a-p.
 * @param {string} key base64 DER SubjectPublicKeyInfo
 */
export function chromeExtensionIdFromKey(key) {
  const digest = createHash("sha256").update(Buffer.from(key, "base64")).digest("hex").slice(0, 32);
  return [...digest].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join("");
}

/**
 * @param {Record<string, any>} firefox the parsed static/manifest.json
 * @param {{ key: string, id: string }} chromeKey the parsed static/chrome-key.json
 * @returns {Record<string, any>}
 */
export function chromeManifest(firefox, chromeKey) {
  const unknown = Object.keys(firefox).filter((k) => !KNOWN_KEYS.includes(k));
  if (unknown.length > 0) {
    throw new Error(`static/manifest.json has keys the Chrome build does not know: ${unknown.join(", ")}`);
  }
  if (firefox.manifest_version !== 3) throw new Error("static/manifest.json must be manifest_version 3");
  const scripts = firefox.background?.scripts;
  if (!Array.isArray(scripts) || scripts.length !== 1 || Object.keys(firefox.background).length !== 1) {
    throw new Error('static/manifest.json background must be {"scripts": [<one file>]}');
  }
  const action = firefox.action ?? {};
  const unknownAction = Object.keys(action).filter((k) => !KNOWN_ACTION_KEYS.includes(k));
  if (unknownAction.length > 0) {
    throw new Error(`static/manifest.json action has keys the Chrome build does not know: ${unknownAction.join(", ")}`);
  }
  if (typeof chromeKey?.key !== "string" || typeof chromeKey?.id !== "string") {
    throw new Error("static/chrome-key.json must hold {key, id}");
  }
  const derived = chromeExtensionIdFromKey(chromeKey.key);
  if (derived !== chromeKey.id) {
    throw new Error(`static/chrome-key.json: the key gives the ID ${derived}, not ${chromeKey.id}`);
  }

  const icons = iconMap();
  /** @type {Record<string, any>} */
  const chromeAction = {};
  if (action.default_title !== undefined) chromeAction.default_title = action.default_title;
  chromeAction.default_icon = icons;

  return {
    manifest_version: 3,
    name: firefox.name,
    version: firefox.version,
    description: firefox.description,
    minimum_chrome_version: MINIMUM_CHROME_VERSION,
    key: chromeKey.key,
    icons,
    permissions: structuredClone(firefox.permissions),
    host_permissions: structuredClone(firefox.host_permissions),
    action: chromeAction,
    background: { service_worker: scripts[0] },
    content_scripts: structuredClone(firefox.content_scripts),
  };
}
