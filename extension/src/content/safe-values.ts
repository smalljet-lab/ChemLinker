/**
 * The two places where text from the host becomes something other than text:
 * a thumbnail URL and a link URL.
 */

import { SITE_ORIGIN } from "../shared/site";

/** Thumbnails larger than this (in UTF-16 code units, about the same as bytes for SVG) are not shown. */
export const MAX_SVG_CHARS = 300_000;

/**
 * `data:` URL for an SVG produced by the host, for use ONLY as `<img src>`
 * (an image document never runs scripts). Null when missing or too large.
 */
export function svgDataUrl(svg: unknown, maxChars: number = MAX_SVG_CHARS): string | null {
  if (typeof svg !== "string" || svg.trim() === "" || svg.length > maxChars) return null;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** The URL to use as a link, or null when it is not an https URL on the Sciformation site. */
export function experimentLink(url: unknown, origin: string = SITE_ORIGIN): string | null {
  if (typeof url !== "string" || url === "") return null;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.origin !== origin || parsed.protocol !== "https:") return null;
  return parsed.href;
}
