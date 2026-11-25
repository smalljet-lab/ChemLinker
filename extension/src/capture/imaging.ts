/**
 * Decoding the screenshot and cutting the selected area out of it, behind a
 * small interface so the page logic can be tested without a canvas. The real
 * one uses an <img> and a <canvas> of this page; nothing is loaded from
 * anywhere but the `data:` URL the background handed over.
 */

import type { Rect } from "./geometry";

export interface Picture {
  /** The `data:image/png;base64,...` URL, for showing it. */
  url: string;
  /** The picture's own pixels (device pixels of the captured tab). */
  width: number;
  height: number;
}

export interface Imaging {
  /** Decodes the picture. Rejects when it cannot be read. */
  load(url: string): Promise<Picture>;
  /** The area `rect` (picture pixels) drawn `width` x `height` pixels large, as base64 of a PNG ("" when it fails). */
  crop(picture: Picture, rect: Rect, width: number, height: number): string;
}

const PNG_PREFIX = "data:image/png;base64,";

export function createCanvasImaging(doc: Document): Imaging {
  const sources = new WeakMap<Picture, HTMLImageElement>();
  return {
    load(url: string): Promise<Picture> {
      return new Promise((resolve, reject) => {
        const img = doc.createElement("img");
        img.addEventListener("load", () => {
          if (!(img.naturalWidth > 0 && img.naturalHeight > 0)) {
            reject(new Error("the picture is empty"));
            return;
          }
          const picture: Picture = { url, width: img.naturalWidth, height: img.naturalHeight };
          sources.set(picture, img);
          resolve(picture);
        });
        img.addEventListener("error", () => reject(new Error("the picture could not be decoded")));
        img.src = url;
      });
    },
    crop(picture: Picture, rect: Rect, width: number, height: number): string {
      const source = sources.get(picture);
      if (!source) return "";
      const canvas = doc.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (!context) return "";
      // A white ground: a transparent area would otherwise reach the model as black.
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, width, height);
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.drawImage(source, rect.x, rect.y, rect.w, rect.h, 0, 0, width, height);
      const url = canvas.toDataURL("image/png");
      return url.startsWith(PNG_PREFIX) ? url.slice(PNG_PREFIX.length) : "";
    },
  };
}
