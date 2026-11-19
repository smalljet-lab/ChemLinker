/**
 * Build-time settings. build.mjs replaces the two identifiers below in every
 * bundle (esbuild `define`); where nothing replaces them (the tests) the
 * defaults apply.
 *
 * The ELN address is an https origin: scheme, host and optional port. Two
 * things follow from that and hold for every build:
 *   - Sciformation is served at the root of the host: experiment links are
 *     `<origin>/startUseCase?...` (shared/import.ts) and structures are
 *     downloaded from `<origin>/zkau/...` (page/bridge.ts);
 *   - links to experiments are only ever https links (content/safe-values.ts).
 * The manifest's host_permissions and content script matches are written from
 * the same setting by build.mjs.
 */

declare const __ELN_ORIGIN__: string | undefined;
declare const __EXTENSION_VERSION__: string | undefined;

/** The address used when the build names no other; also the one in static/manifest.json. */
export const DEFAULT_SITE_ORIGIN = "https://sciformation.chemie.unibas.ch";

/** The only site the extension runs on. */
export const SITE_ORIGIN: string = typeof __ELN_ORIGIN__ === "string" ? __ELN_ORIGIN__ : DEFAULT_SITE_ORIGIN;

/** The version in package.json and the manifest; null where the code was not built by build.mjs. */
export const EXTENSION_VERSION: string | null =
  typeof __EXTENSION_VERSION__ === "string" ? __EXTENSION_VERSION__ : null;
