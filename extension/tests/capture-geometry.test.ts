import { describe, expect, it, vi } from "vitest";
import {
  boxPercent,
  cornerPoint,
  cropProblem,
  cutOutView,
  encodeWithinLimit,
  moveCorner,
  nextScale,
  nudgeCorner,
  rectFromPoints,
  scaledSize,
  startScale,
  toPictureRect,
  wholeRect,
} from "../src/capture/geometry";
import { MAX_IMAGE_BASE64_CHARS, MAX_IMAGE_SIDE, MIN_IMAGE_SIDE } from "../src/shared/protocol";

const UNIT = { w: 1, h: 1 };

describe("selection box", () => {
  it("is the rectangle between two points in any drag direction", () => {
    const expected = { x: 10, y: 20, w: 30, h: 40 };
    const bounds = { w: 100, h: 100 };
    expect(rectFromPoints({ x: 10, y: 20 }, { x: 40, y: 60 }, bounds)).toEqual(expected);
    expect(rectFromPoints({ x: 40, y: 60 }, { x: 10, y: 20 }, bounds)).toEqual(expected);
    expect(rectFromPoints({ x: 40, y: 20 }, { x: 10, y: 60 }, bounds)).toEqual(expected);
  });

  it("stays inside the picture when the pointer leaves it", () => {
    expect(rectFromPoints({ x: 0.5, y: 0.5 }, { x: 1.7, y: -0.3 }, UNIT)).toEqual({ x: 0.5, y: 0, w: 0.5, h: 0.5 });
    expect(rectFromPoints({ x: -5, y: -5 }, { x: 5, y: 5 }, UNIT)).toEqual(wholeRect(UNIT));
  });

  it("moves one corner and keeps the opposite one, also across it", () => {
    const rect = { x: 10, y: 10, w: 20, h: 20 };
    const bounds = { w: 100, h: 100 };
    expect(moveCorner(rect, "se", { x: 50, y: 60 }, bounds)).toEqual({ x: 10, y: 10, w: 40, h: 50 });
    expect(moveCorner(rect, "nw", { x: 0, y: 5 }, bounds)).toEqual({ x: 0, y: 5, w: 30, h: 25 });
    expect(moveCorner(rect, "ne", { x: 40, y: 0 }, bounds)).toEqual({ x: 10, y: 0, w: 30, h: 30 });
    expect(moveCorner(rect, "sw", { x: 5, y: 90 }, bounds)).toEqual({ x: 5, y: 10, w: 25, h: 80 });
    // Dragged past the fixed corner: the box flips instead of getting a negative size.
    expect(moveCorner(rect, "se", { x: 0, y: 0 }, bounds)).toEqual({ x: 0, y: 0, w: 10, h: 10 });
    expect(cornerPoint(rect, "se")).toEqual({ x: 30, y: 30 });
    expect(cornerPoint(rect, "nw")).toEqual({ x: 10, y: 10 });
  });

  it("nudges a corner with the keyboard and stops at the edge", () => {
    const rect = { x: 0.2, y: 0.2, w: 0.2, h: 0.2 };
    const moved = nudgeCorner(rect, "se", 0.1, 0, UNIT);
    expect(moved.w).toBeCloseTo(0.3);
    expect(moved.h).toBeCloseTo(0.2);
    const edge = nudgeCorner(rect, "se", 5, 5, UNIT);
    expect(edge.x + edge.w).toBe(1);
    expect(edge.y + edge.h).toBe(1);
  });
});

describe("from the box on the page to the pixels of the screenshot", () => {
  it("scales by picture / display, so a screen at 150 % gives 1.5 picture pixels per page pixel", () => {
    // A 1280 x 720 tab captured at device pixel ratio 1.5, shown 1:1 in CSS pixels.
    const display = { w: 1280, h: 720 };
    const picture = { w: 1920, h: 1080 };
    expect(toPictureRect({ x: 100, y: 200, w: 400, h: 300 }, display, picture)).toEqual({ x: 150, y: 300, w: 600, h: 450 });
  });

  it("gives the full resolution of the screenshot when the page shows it smaller", () => {
    // Device pixel ratio 2, and the page shows the 2560 px picture 800 px wide.
    const display = { w: 800, h: 450 };
    const picture = { w: 2560, h: 1440 };
    expect(toPictureRect({ x: 200, y: 100, w: 400, h: 225 }, display, picture)).toEqual({ x: 640, y: 320, w: 1280, h: 720 });
  });

  it("works with the selection as fractions of the picture", () => {
    expect(toPictureRect({ x: 0.25, y: 0.5, w: 0.5, h: 0.25 }, UNIT, { w: 1352, h: 1248 })).toEqual({ x: 338, y: 624, w: 676, h: 312 });
    expect(toPictureRect(wholeRect(UNIT), UNIT, { w: 1352, h: 1248 })).toEqual({ x: 0, y: 0, w: 1352, h: 1248 });
  });

  it("rounds outwards to whole pixels and never leaves the picture", () => {
    const rect = toPictureRect({ x: 0.3334, y: 0.3334, w: 0.3333, h: 0.3333 }, UNIT, { w: 100, h: 100 })!;
    expect(rect).toEqual({ x: 33, y: 33, w: 34, h: 34 });
    for (const value of Object.values(rect)) expect(Number.isInteger(value)).toBe(true);
    expect(toPictureRect({ x: 0.9, y: 0.9, w: 0.5, h: 0.5 }, UNIT, { w: 100, h: 100 })).toEqual({ x: 90, y: 90, w: 10, h: 10 });
  });

  it("gives nothing for an unusable size", () => {
    expect(toPictureRect(wholeRect(UNIT), { w: 0, h: 10 }, { w: 100, h: 100 })).toBeNull();
    expect(toPictureRect(wholeRect(UNIT), UNIT, { w: 0, h: 0 })).toBeNull();
  });

  it("says when the crop is too small for the host", () => {
    expect(cropProblem(null)).not.toBeNull();
    expect(cropProblem({ x: 0, y: 0, w: MIN_IMAGE_SIDE - 1, h: 500 })).toContain("too small");
    expect(cropProblem({ x: 0, y: 0, w: 500, h: MIN_IMAGE_SIDE - 1 })).toContain("too small");
    expect(cropProblem({ x: 0, y: 0, w: MIN_IMAGE_SIDE, h: MIN_IMAGE_SIDE })).toBeNull();
  });
});

describe("the size of the picture that is sent", () => {
  it("never scales up and keeps whole pixels", () => {
    expect(scaledSize({ w: 300, h: 200 }, 1)).toEqual({ w: 300, h: 200 });
    expect(scaledSize({ w: 300, h: 200 }, 3)).toEqual({ w: 300, h: 200 });
    expect(scaledSize({ w: 301, h: 201 }, 0.5)).toEqual({ w: 151, h: 101 });
    expect(scaledSize({ w: 3, h: 2 }, 0.01)).toEqual({ w: 1, h: 1 });
  });

  it("starts at the crop's own size unless a side is longer than the host accepts", () => {
    expect(startScale({ w: 1920, h: 1080 })).toBe(1);
    expect(startScale({ w: MAX_IMAGE_SIDE * 2, h: 100 })).toBe(0.5);
    expect(startScale({ w: 100, h: MAX_IMAGE_SIDE * 4 })).toBe(0.25);
  });

  it("the next scale is always clearly smaller", () => {
    expect(nextScale(1, 1_000_001, 1_000_000)).toBeCloseTo(0.9);
    expect(nextScale(1, 4_000_000, 1_000_000)).toBeCloseTo(0.475);
    expect(nextScale(0.5, 4_000_000, 1_000_000)).toBeCloseTo(0.2375);
  });

  /** A PNG whose base64 length grows with the area. */
  const fakeEncode = (charsPerPixel: number) => vi.fn((w: number, h: number) => "A".repeat(Math.ceil(w * h * charsPerPixel)));

  it("sends the crop as it is when it fits", async () => {
    const encode = fakeEncode(0.5);
    const out = await encodeWithinLimit({ w: 1000, h: 800 }, encode);
    expect(out).toMatchObject({ width: 1000, height: 800, scale: 1 });
    expect(out!.base64.length).toBe(400_000);
    expect(encode).toHaveBeenCalledTimes(1);
  });

  it("makes the picture smaller step by step until the base64 fits, and never larger than the crop", async () => {
    const encode = fakeEncode(2);
    const crop = { w: 1920, h: 1080 }; // 4.1 million characters at full size
    const out = (await encodeWithinLimit(crop, encode))!;
    expect(out.base64.length).toBeLessThanOrEqual(MAX_IMAGE_BASE64_CHARS);
    expect(out.scale).toBeLessThan(1);
    expect(out.width).toBeLessThan(crop.w);
    expect(out.width / out.height).toBeCloseTo(crop.w / crop.h, 1);
    expect(encode.mock.calls.length).toBeGreaterThan(1);
    expect(encode.mock.calls.length).toBeLessThan(6);
    // Every try was no larger than the one before, starting at the crop's own size.
    const widths = encode.mock.calls.map((c) => c[0]);
    expect(widths[0]).toBe(1920);
    expect([...widths].sort((a, b) => b - a)).toEqual(widths);
    // Not smaller than needed by much: the result uses most of the limit.
    expect(out.base64.length).toBeGreaterThan(MAX_IMAGE_BASE64_CHARS * 0.6);
  });

  it("honours a limit given by the caller, exactly at the boundary", async () => {
    const encode = (w: number, h: number) => "A".repeat(w * h);
    expect(await encodeWithinLimit({ w: 100, h: 100 }, encode, 10_000)).toMatchObject({ scale: 1 });
    const out = (await encodeWithinLimit({ w: 100, h: 100 }, encode, 9_999))!;
    expect(out.scale).toBeLessThan(1);
    expect(out.base64.length).toBeLessThanOrEqual(9_999);
  });

  it("reduces a crop with a side longer than the host accepts before the first try", async () => {
    const encode = fakeEncode(0.001);
    const out = (await encodeWithinLimit({ w: MAX_IMAGE_SIDE * 2, h: 400 }, encode))!;
    expect(encode.mock.calls[0]).toEqual([MAX_IMAGE_SIDE, 200]);
    expect(out.width).toBe(MAX_IMAGE_SIDE);
  });

  it("gives up when the crop is too small, the picture cannot be made small enough, or encoding fails", async () => {
    const encode = fakeEncode(1);
    expect(await encodeWithinLimit({ w: MIN_IMAGE_SIDE - 1, h: 100 }, encode)).toBeNull();
    expect(encode).not.toHaveBeenCalled();
    expect(await encodeWithinLimit({ w: 200, h: 200 }, () => "A".repeat(50), 10)).toBeNull();
    expect(await encodeWithinLimit({ w: 200, h: 200 }, () => "")).toBeNull();
  });

  it("waits for an asynchronous encoder", async () => {
    const out = await encodeWithinLimit({ w: 50, h: 50 }, async () => "QUJD");
    expect(out).toEqual({ base64: "QUJD", width: 50, height: 50, scale: 1 });
  });
});

describe("boxes over the picture", () => {
  it("are placed in percent of the picture the host saw, whatever size it is shown at", () => {
    expect(boxPercent([338, 312, 676, 624], 1352, 1248)).toEqual({ left: 25, top: 25, width: 25, height: 25 });
    expect(boxPercent([0, 0, 1352, 1248], 1352, 1248)).toEqual({ left: 0, top: 0, width: 100, height: 100 });
  });

  it("are left out when they make no sense", () => {
    expect(boxPercent([10, 10, 10, 50], 100, 100)).toBeNull();
    expect(boxPercent([50, 10, 10, 50], 100, 100)).toBeNull();
    expect(boxPercent([0, 0, 10, 10], 0, 100)).toBeNull();
    expect(boxPercent([50, 50, 400, 400], 100, 100)).toEqual({ left: 50, top: 50, width: 50, height: 50 }); // cut at the edge
  });
});

describe("showing one part of the picture", () => {
  it("the whole picture fills the frame", () => {
    expect(cutOutView(null, 800, 400)).toEqual({ left: -0, top: -0, width: 100, height: 100, ratio: 2, pixels: 800 });
  });

  it("a box is shown with a little of its surroundings, as percentages of the frame", () => {
    // 100 x 50 box in the middle of 800 x 400, no margin: the picture is 8 x as large as the frame.
    expect(cutOutView([300, 150, 400, 200], 800, 400, 0)).toEqual({ left: -300, top: -300, width: 800, height: 800, ratio: 2, pixels: 100 });
    // Margin: 15 % of the larger side (15 pixels) on every side -> [285, 135, 415, 215].
    const view = cutOutView([300, 150, 400, 200], 800, 400)!;
    expect(view.pixels).toBe(130);
    expect(view.ratio).toBeCloseTo(130 / 80, 9);
    expect(view.left).toBeCloseTo((-285 / 130) * 100, 9);
    expect(view.top).toBeCloseTo((-135 / 80) * 100, 9);
    expect(view.width).toBeCloseTo((800 / 130) * 100, 9);
    expect(view.height).toBeCloseTo((400 / 80) * 100, 9);
  });

  it("the margin stops at the edge of the picture", () => {
    const view = cutOutView([0, 0, 100, 100], 800, 400)!;
    expect(view.left).toBeCloseTo(0, 9);
    expect(view.top).toBeCloseTo(0, 9);
    expect(view.pixels).toBe(115);
    const corner = cutOutView([700, 300, 900, 500], 800, 400, 0)!; // reaches outside: clamped
    expect(corner.pixels).toBe(100);
    expect(corner.ratio).toBe(1);
  });

  it("gives nothing for an empty box or picture", () => {
    expect(cutOutView([10, 10, 10, 50], 800, 400)).toBeNull();
    expect(cutOutView([900, 10, 950, 50], 800, 400)).toBeNull();
    expect(cutOutView(null, 0, 400)).toBeNull();
  });
});
