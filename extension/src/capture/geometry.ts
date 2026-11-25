/**
 * The arithmetic of the selection box and of the picture that is sent. Pure:
 * no DOM, no canvas.
 *
 * Two kinds of units: "display" (CSS pixels of the screenshot as the page
 * shows it, scaled to fit) and "picture" (the screenshot's own pixels:
 * the background's screenshot is in device pixels, so on a screen scaled to
 * 150 % a 1280 px wide tab gives a 1920 px wide picture). The crop is always
 * taken in picture pixels, so nothing is lost to the display scale.
 */

import { MAX_IMAGE_BASE64_CHARS, MAX_IMAGE_SIDE, MIN_IMAGE_SIDE } from "../shared/protocol";

export interface Size {
  w: number;
  h: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type Corner = "nw" | "ne" | "sw" | "se";
export const CORNERS: readonly Corner[] = ["nw", "ne", "sw", "se"];

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** The rectangle between two points (in any order), kept inside `bounds`. */
export function rectFromPoints(a: Point, b: Point, bounds: Size): Rect {
  const x0 = clamp(Math.min(a.x, b.x), 0, bounds.w);
  const x1 = clamp(Math.max(a.x, b.x), 0, bounds.w);
  const y0 = clamp(Math.min(a.y, b.y), 0, bounds.h);
  const y1 = clamp(Math.max(a.y, b.y), 0, bounds.h);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** The point of `rect` at `corner`. */
export function cornerPoint(rect: Rect, corner: Corner): Point {
  return {
    x: corner === "nw" || corner === "sw" ? rect.x : rect.x + rect.w,
    y: corner === "nw" || corner === "ne" ? rect.y : rect.y + rect.h,
  };
}

const OPPOSITE: Readonly<Record<Corner, Corner>> = { nw: "se", ne: "sw", sw: "ne", se: "nw" };

/** `rect` with one corner moved to `point`; the opposite corner stays. */
export function moveCorner(rect: Rect, corner: Corner, point: Point, bounds: Size): Rect {
  return rectFromPoints(cornerPoint(rect, OPPOSITE[corner]), point, bounds);
}

/** `rect` with one corner moved by (dx, dy): the keyboard way to change the box. */
export function nudgeCorner(rect: Rect, corner: Corner, dx: number, dy: number, bounds: Size): Rect {
  const from = cornerPoint(rect, corner);
  return moveCorner(rect, corner, { x: from.x + dx, y: from.y + dy }, bounds);
}

/** The whole picture as a selection. */
export function wholeRect(bounds: Size): Rect {
  return { x: 0, y: 0, w: bounds.w, h: bounds.h };
}

/**
 * A selection in display units as whole picture pixels: scaled by
 * picture / display (which is where the device pixel ratio comes in), rounded
 * outwards and kept inside the picture. Null when either size is unusable.
 */
export function toPictureRect(selection: Rect, display: Size, picture: Size): Rect | null {
  if (!(display.w > 0 && display.h > 0 && picture.w >= 1 && picture.h >= 1)) return null;
  const sx = picture.w / display.w;
  const sy = picture.h / display.h;
  const x0 = clamp(Math.floor(selection.x * sx), 0, picture.w);
  const y0 = clamp(Math.floor(selection.y * sy), 0, picture.h);
  const x1 = clamp(Math.ceil((selection.x + selection.w) * sx), 0, picture.w);
  const y1 = clamp(Math.ceil((selection.y + selection.h) * sy), 0, picture.h);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** Why a crop cannot be sent, in plain words, or null. */
export function cropProblem(crop: Rect | null): string | null {
  if (crop === null) return "The picture is not ready yet.";
  if (crop.w < MIN_IMAGE_SIDE || crop.h < MIN_IMAGE_SIDE) {
    return `The selected area is too small (it must be at least ${MIN_IMAGE_SIDE} by ${MIN_IMAGE_SIDE} pixels). Drag a larger box.`;
  }
  return null;
}

/** `size` scaled by `scale` (at most 1: never larger than the crop), in whole pixels of at least 1. */
export function scaledSize(size: Size, scale: number): Size {
  const s = Math.min(1, scale);
  return { w: Math.max(1, Math.round(size.w * s)), h: Math.max(1, Math.round(size.h * s)) };
}

/** The largest scale (at most 1) at which no side is longer than `maxSide`. */
export function startScale(size: Size, maxSide: number = MAX_IMAGE_SIDE): number {
  return Math.min(1, maxSide / Math.max(size.w, size.h));
}

/**
 * The next, smaller scale to try after a picture of `length` characters did not
 * fit into `limit`. The length grows roughly with the area, so the scale goes
 * down with the square root of the excess, a little further to be safe, and
 * always by at least 10 %.
 */
export function nextScale(scale: number, length: number, limit: number): number {
  const estimate = scale * Math.sqrt(limit / length) * 0.95;
  return Math.min(estimate, scale * 0.9);
}

export interface EncodedPicture {
  /** Base64 of the PNG, no "data:" prefix. */
  base64: string;
  width: number;
  height: number;
  /** 1 when the crop was sent as it is; below 1 when it had to be made smaller. */
  scale: number;
}

/**
 * Encodes the crop as PNG, first at its own size, then stepwise smaller until
 * the base64 text fits into `limit` characters (the request must stay under
 * the host's frame limit). Never larger than the crop. Null when even the
 * smallest allowed picture does not fit, or the crop is too small to send.
 *
 * @param encode draws the crop at the given size and returns the base64 PNG
 */
export async function encodeWithinLimit(
  crop: Size,
  encode: (width: number, height: number) => string | Promise<string>,
  limit: number = MAX_IMAGE_BASE64_CHARS,
): Promise<EncodedPicture | null> {
  if (crop.w < MIN_IMAGE_SIDE || crop.h < MIN_IMAGE_SIDE) return null;
  let scale = startScale(crop);
  for (let attempt = 0; attempt < 40; attempt++) {
    const size = scaledSize(crop, scale);
    if (size.w < MIN_IMAGE_SIDE || size.h < MIN_IMAGE_SIDE) return null;
    const base64 = await encode(size.w, size.h);
    if (base64.length > 0 && base64.length <= limit) return { base64, width: size.w, height: size.h, scale };
    if (base64.length === 0) return null;
    scale = nextScale(scale, base64.length, limit);
  }
  return null;
}

/** A box of the result as percentages of the picture it was found in, for drawing it over the picture at any size. */
export function boxPercent(box: readonly [number, number, number, number], width: number, height: number): { left: number; top: number; width: number; height: number } | null {
  if (!(width > 0 && height > 0)) return null;
  const x0 = clamp(box[0], 0, width);
  const y0 = clamp(box[1], 0, height);
  const x1 = clamp(box[2], 0, width);
  const y1 = clamp(box[3], 0, height);
  if (!(x1 > x0 && y1 > y0)) return null;
  const pct = (value: number, total: number) => (value / total) * 100;
  return { left: pct(x0, width), top: pct(y0, height), width: pct(x1 - x0, width), height: pct(y1 - y0, height) };
}

/**
 * How to show only one part of a picture: a frame of the part's shape
 * (`ratio` = width / height) with the whole picture inside it, `width` x
 * `height` percent of the frame large and moved by `left` / `top` percent of
 * the frame, so that exactly the part is seen. `pixels` is the part's width in
 * picture pixels.
 *
 * `box` null means the whole picture. `margin` (a fraction of the box's larger
 * side) is added around a box, as far as the picture reaches. Null when the
 * box is empty or outside the picture.
 */
export function cutOutView(
  box: readonly [number, number, number, number] | null,
  width: number,
  height: number,
  margin = 0.15,
): { left: number; top: number; width: number; height: number; ratio: number; pixels: number } | null {
  if (!(width > 0 && height > 0)) return null;
  let x0 = 0;
  let y0 = 0;
  let x1 = width;
  let y1 = height;
  if (box !== null) {
    x0 = clamp(box[0], 0, width);
    y0 = clamp(box[1], 0, height);
    x1 = clamp(box[2], 0, width);
    y1 = clamp(box[3], 0, height);
    if (!(x1 > x0 && y1 > y0)) return null;
    const extra = Math.max(x1 - x0, y1 - y0) * Math.max(0, margin);
    x0 = clamp(x0 - extra, 0, width);
    y0 = clamp(y0 - extra, 0, height);
    x1 = clamp(x1 + extra, 0, width);
    y1 = clamp(y1 + extra, 0, height);
  }
  const w = x1 - x0;
  const h = y1 - y0;
  return { left: (-x0 / w) * 100, top: (-y0 / h) * 100, width: (width / w) * 100, height: (height / h) * 100, ratio: w / h, pixels: w };
}
