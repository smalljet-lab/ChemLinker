// Draws the toolbar hexagon of static/compounds-icon.svg as PNG files for the
// Chrome build (Chrome accepts no SVG for "icons" or "action.default_icon").
// Dependency-free: a small anti-aliased polygon rasterizer and a PNG writer on
// node:zlib. The output is deterministic, and tests check that the committed
// files in static/icons/ are exactly what this script draws.
// Usage: `node scripts/make-icons.mjs` (rewrites static/icons/icon-<size>.png).
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";

/** The sizes the Chrome manifest names. */
export const ICON_SIZES = [16, 32, 48, 128];

/** Path of one icon, relative to static/ and to the built extension folder. */
export function iconPath(size) {
  return `icons/icon-${size}.png`;
}

// static/compounds-icon.svg, viewBox 0 0 32 32, drawn in this order.
const OUTER = [[16, 2], [28.1, 9], [28.1, 23], [16, 30], [3.9, 23], [3.9, 9]];
const INNER = [[16, 8], [22.9, 12], [22.9, 20], [16, 24], [9.1, 20], [9.1, 12]];
const FILL = [0x0b, 0x5c, 0xad];
const OUTER_STROKE = { color: [0x0a, 0x4f, 0x94], halfWidth: 0.75 };
const INNER_STROKE = { color: [0xff, 0xff, 0xff], halfWidth: 1 };
const VIEWBOX = 32;
const SAMPLES = 8; // 8 x 8 samples per pixel

function inside(poly, x, y) {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

function edgeDistance(poly, x, y) {
  let best = Infinity;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [ax, ay] = poly[j];
    const [bx, by] = poly[i];
    const dx = bx - ax;
    const dy = by - ay;
    const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)));
    best = Math.min(best, Math.hypot(x - (ax + t * dx), y - (ay + t * dy)));
  }
  return best;
}

/** Colour of one point in viewBox units, or null when it is transparent. */
function colorAt(x, y) {
  if (edgeDistance(INNER, x, y) <= INNER_STROKE.halfWidth) return INNER_STROKE.color;
  if (edgeDistance(OUTER, x, y) <= OUTER_STROKE.halfWidth) return OUTER_STROKE.color;
  if (inside(OUTER, x, y)) return FILL;
  return null;
}

/** RGBA pixels, row by row, of the icon at `size` x `size`. */
export function renderIcon(size) {
  const pixels = Buffer.alloc(size * size * 4);
  const unit = VIEWBOX / size;
  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      let r = 0, g = 0, b = 0, covered = 0;
      for (let sy = 0; sy < SAMPLES; sy++) {
        for (let sx = 0; sx < SAMPLES; sx++) {
          const c = colorAt((px + (sx + 0.5) / SAMPLES) * unit, (py + (sy + 0.5) / SAMPLES) * unit);
          if (c === null) continue;
          r += c[0];
          g += c[1];
          b += c[2];
          covered++;
        }
      }
      const at = (py * size + px) * 4;
      if (covered > 0) {
        pixels[at] = Math.round(r / covered);
        pixels[at + 1] = Math.round(g / covered);
        pixels[at + 2] = Math.round(b / covered);
        pixels[at + 3] = Math.round((covered * 255) / (SAMPLES * SAMPLES));
      }
    }
  }
  return pixels;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "latin1");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])) >>> 0, 0);
  return Buffer.concat([head, data, crc]);
}

/** An 8-bit RGBA PNG (no filtering, no ancillary chunks). */
export function encodePng(width, height, rgba) {
  if (rgba.length !== width * height * 4) throw new Error("pixel buffer does not match the size");
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filter
  ihdr[12] = 0; // no interlace
  const rows = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y++) {
    rows[y * (width * 4 + 1)] = 0; // filter type None
    Buffer.from(rgba.buffer, rgba.byteOffset + y * width * 4, width * 4).copy(rows, y * (width * 4 + 1) + 1);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(rows, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** The PNG file of the icon at `size` x `size`. */
export function iconPng(size) {
  return encodePng(size, size, renderIcon(size));
}

const here = fileURLToPath(import.meta.url);
if (process.argv[1] && resolve(process.argv[1]).toLowerCase() === resolve(here).toLowerCase()) {
  const out = join(dirname(dirname(here)), "static");
  for (const size of ICON_SIZES) {
    const file = join(out, ...iconPath(size).split("/"));
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, iconPng(size));
    console.log(`Wrote ${file}`);
  }
}
