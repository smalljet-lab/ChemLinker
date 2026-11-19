/**
 * Page bridge: runs in the PAGE's own world (manifest `"world": "MAIN"`),
 * where the ELN's ZK objects (`zk`, `zAu`) live. Implements
 * `shared/bridge.ts`: answers each request event exactly once.
 *
 * `molfile` asks the ZK widget of a product structure for its molfile the way
 * the ELN itself does: it fires `onGetMolfile` to the server, which answers
 * with a `download` command; a temporary hook on `zAu.cmd0.download` catches
 * that URL instead of letting the browser download a file, and the molfile is
 * then fetched from the same origin.
 *
 * `reaction` reads the experiment's reaction grids and fields (reactants,
 * reagents, solvents, temperature, reference amount, units) through the
 * ELN's own component ids (`zk.Widget.$('$id')`). Read-only: it only reads
 * texts and input values, sends nothing to the server.
 *
 * Safety rules:
 * - one molfile request at a time (the download hook is global);
 * - the hook only takes a same-origin URL under /zkau/ ending in
 *   /molfile.mol; any other download goes to the original function untouched;
 * - the original function is restored in every path (success, timeout, error);
 * - one time limit covers the whole read (the server's answer and the
 *   download), so a download that never ends cannot hold up the next request;
 * - never throws into the page, touches nothing else, and does nothing at all
 *   until a request arrives.
 *
 * This is the ONLY file under src/ allowed to call fetch (same origin only).
 */

import {
  BRIDGE_INSTALLED_FLAG,
  BRIDGE_MOLFILE_TIMEOUT_MS,
  BRIDGE_REQUEST_EVENT,
  BRIDGE_RESPONSE_EVENT,
  type BridgeErrorCode,
  type BridgeResponse,
  type PageAmounts,
  type PageComponent,
  type PageReaction,
} from "../shared/bridge";
import { MAX_STRUCTURE_CHARS } from "../shared/protocol";

/* eslint-disable @typescript-eslint/no-explicit-any */
type AnyFn = (...args: any[]) => any;

export interface BridgeWindow {
  document: Document;
  location: { href: string; origin: string };
  CustomEvent: typeof CustomEvent;
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
  setTimeout: (fn: () => void, ms: number) => any;
  clearTimeout: (timer: any) => void;
  /** Used to give up a download that takes too long; without it the download is only no longer waited for. */
  AbortController?: typeof AbortController;
  zk?: any;
  zAu?: any;
}

export interface BridgeOptions {
  timeoutMs?: number;
}

const INSTALLED = BRIDGE_INSTALLED_FLAG;
const MAX_REQUEST_DETAIL = 10_000;

function errorMessage(error: unknown): string {
  try {
    if (error && typeof error === "object" && "message" in error && typeof (error as any).message === "string") {
      return (error as any).message.slice(0, 300);
    }
  } catch {
    // ignore
  }
  return "unexpected error";
}

/** The molfile download URL, or null when `url` is anything else. */
export function molfileDownloadUrl(url: unknown, location: { href: string; origin: string }): string | null {
  if (typeof url !== "string" || url === "") return null;
  let parsed: URL;
  try {
    parsed = new URL(url, location.href);
  } catch {
    return null;
  }
  if (parsed.origin !== location.origin) return null;
  if (!parsed.pathname.startsWith("/zkau/") || !parsed.pathname.endsWith("/molfile.mol")) return null;
  return parsed.href;
}

function hasZk(win: BridgeWindow): boolean {
  try {
    return (
      !!win.zk &&
      !!win.zk.Widget &&
      typeof win.zk.Widget.$ === "function" &&
      !!win.zAu &&
      !!win.zAu.cmd0 &&
      typeof win.zAu.cmd0.download === "function"
    );
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------ reaction

/** At most this many rows are read from one grid, and this many characters from one text. */
export const MAX_PAGE_ROWS = 200;
export const MAX_PAGE_TEXT = 2000;

/** Whitespace collapsed (also no-break spaces), trimmed, clipped; empty -> null. */
function cleanText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.replace(/\s+/g, " ").trim().slice(0, MAX_PAGE_TEXT);
  return text === "" ? null : text;
}

function isElement(value: unknown): value is Element {
  return (
    !!value &&
    typeof value === "object" &&
    (value as { nodeType?: unknown }).nodeType === 1 &&
    typeof (value as Element).querySelectorAll === "function"
  );
}

function hasWidgetLookup(win: BridgeWindow): boolean {
  try {
    return !!win.zk && !!win.zk.Widget && typeof win.zk.Widget.$ === "function";
  } catch {
    return false;
  }
}

/**
 * Reads what the experiment in this frame shows about its reaction, through
 * the ELN's own component ids (see `PageReaction`). Never throws; a widget
 * that is missing gives an empty value. Null when the frame shows no
 * reactants grid (not an experiment page).
 */
export function readPageReaction(win: BridgeWindow): PageReaction | null {
  /** The DOM node of the ZK component with this id, or null. */
  const node = (zkId: string): Element | null => {
    try {
      const widget = win.zk.Widget.$(`$${zkId}`);
      const n = widget && typeof widget.$n === "function" ? widget.$n() : null;
      return isElement(n) ? n : null;
    } catch {
      return null;
    }
  };
  /** Every input read; `readOnly` is true when all of them are read-only. */
  const inputsRead: HTMLInputElement[] = [];
  const inputValue = (input: Element | null | undefined): string | null => {
    if (!input) return null;
    const field = input as HTMLInputElement;
    inputsRead.push(field);
    try {
      return cleanText(field.value);
    } catch {
      return null;
    }
  };
  const selectedText = (select: Element | null | undefined): string | null => {
    if (!select) return null;
    try {
      const s = select as HTMLSelectElement;
      const option = s.selectedIndex >= 0 ? s.options[s.selectedIndex] : select.querySelector("option[selected]");
      return option ? cleanText(option.textContent) : null;
    } catch {
      return null;
    }
  };
  const unitOf = (zkId: string): string | null => {
    const n = node(zkId);
    if (!n) return null;
    return selectedText(n.matches("select") ? n : n.querySelector("select"));
  };
  const rowsOf = (grid: Element | null): Element[] => {
    if (!grid) return [];
    try {
      return Array.from(grid.querySelectorAll(":scope > .z-grid-body tbody.z-rows > tr.z-row")).slice(0, MAX_PAGE_ROWS);
    } catch {
      return [];
    }
  };
  /** Actual (first input) and Plan (second input) of one amount cell. */
  const pair = (cell: Element | undefined): [string | null, string | null] => {
    if (!cell) return [null, null];
    const inputs = cell.querySelectorAll("input");
    return [inputValue(inputs[0]), inputValue(inputs[1])];
  };
  const labelsOf = (cell: Element): string[] => {
    const out: string[] = [];
    for (const span of Array.from(cell.querySelectorAll("span.z-label"))) {
      const text = cleanText(span.textContent);
      if (text !== null) out.push(text);
    }
    return out;
  };

  const component = (tr: Element, index: number): PageComponent => {
    const cells = Array.from(tr.children);
    const macro = cells[2]?.querySelector("div.z-macro[smiles]") ?? null;
    let name: string | null = null;
    let sourceRef: string | null = null;
    let sourceCode: string | null = null;
    const nameCell = cells[3];
    if (nameCell) {
      const first = labelsOf(nameCell)[0];
      name = first === undefined ? null : cleanText(first.replace(/,\s*$/, ""));
      const link = nameCell.querySelector('a[href*="table=ElnReaction"]');
      if (link) {
        const m = /[?&]pk=(\d+)/.exec(link.getAttribute("href") ?? "");
        sourceRef = m ? m[1]! : null;
        sourceCode = cleanText(link.textContent);
      }
    }
    const formulaInputs = cells[4]?.querySelectorAll("input");
    const [eqA, eqP] = pair(cells[1]);
    const [nA, nP] = pair(cells[6]);
    const [mA, mP] = pair(cells[8]);
    const [vA, vP] = pair(cells[10]);
    const actual: PageAmounts = { eq: eqA, n: nA, m: mA, v: vA };
    const planned: PageAmounts = { eq: eqP, n: nP, m: mP, v: vP };
    const handle = macro && macro.id ? macro.id.slice(0, 200) : null;
    return {
      row: index + 1,
      handle,
      smiles: macro ? cleanText(macro.getAttribute("smiles")) : null,
      name,
      formula: formulaInputs ? cleanText((formulaInputs[0] as HTMLInputElement | undefined)?.value) : null,
      cas: formulaInputs ? cleanText((formulaInputs[1] as HTMLInputElement | undefined)?.value) : null,
      sourceRef,
      sourceCode,
      actual,
      planned,
    };
  };
  const grid = (zkId: string): PageComponent[] => {
    const out: PageComponent[] = [];
    rowsOf(node(zkId)).forEach((tr, i) => {
      try {
        out.push(component(tr, i));
      } catch {
        // an unreadable row is left out
      }
    });
    return out;
  };

  const reactantsGrid = node("elnReactionReactants");
  if (!reactantsGrid) return null;
  const units = { n: unitOf("reactant_amount_unit"), m: unitOf("reactant_mass_unit"), v: unitOf("reactant_volume_unit") };
  const reactants = grid("elnReactionReactants");
  const reagents = grid("elnReactionReagents");

  const solventUnit = unitOf("solvent_volume_unit");
  const solvents: PageReaction["solvents"] = [];
  for (const tr of rowsOf(node("elnReactionSolvents"))) {
    const cells = Array.from(tr.children);
    const label = cells[0]?.querySelector("span.z-label");
    const name = label ? cleanText(label.textContent) : null;
    const volume = inputValue(cells[1]?.querySelector("input"));
    if (name !== null) solvents.push({ name, volume, unit: solventUnit });
  }
  const fieldInput = (zkId: string): Element | null => {
    const n = node(zkId);
    if (!n) return null;
    return n.matches("input") ? n : n.querySelector("input");
  };
  const solventName = inputValue(fieldInput("solvent"));
  const solventAmount = inputValue(fieldInput("solventAmount"));
  if (solvents.length === 0 && solventName !== null) {
    solvents.push({ name: solventName, volume: solventAmount, unit: solventUnit });
  }
  const temperature = inputValue(fieldInput("temperature"));
  let referenceAmount: PageReaction["referenceAmount"] = null;
  const refNode = node("refAmount");
  if (refNode) {
    const value = inputValue(refNode.matches("input") ? refNode : refNode.querySelector("input"));
    if (value !== null) referenceAmount = { value, unit: selectedText(refNode.querySelector("select")) };
  }

  return {
    readOnly: inputsRead.every((input) => {
      try {
        return input.readOnly === true || input.disabled === true;
      } catch {
        return false;
      }
    }),
    amountsShown: amountsShown(node),
    units,
    reactants,
    reagents,
    solvents,
    temperature,
    referenceAmount,
  };
}

/**
 * Which amounts radio is selected: `$displayActual` / `$displayTarget`, else
 * the radio group `$displayTargetValues` (its radios labelled "Actual
 * amounts" and "Planned amounts", in that order). Null when it cannot be told.
 */
function amountsShown(node: (zkId: string) => Element | null): PageReaction["amountsShown"] {
  const checked = (zkId: string): boolean | null => {
    const n = node(zkId);
    if (!n) return null;
    const input = (n.matches("input") ? n : n.querySelector('input[type="radio"]')) as HTMLInputElement | null;
    return input ? input.checked === true : null;
  };
  try {
    if (checked("displayActual") === true) return "actual";
    if (checked("displayTarget") === true) return "planned";
    const group = node("displayTargetValues");
    if (!group) return null;
    const radios = Array.from(group.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
    const index = radios.findIndex((r) => r.checked === true);
    if (index < 0) return null;
    const radio = radios[index]!;
    const label = (radio.parentElement?.textContent ?? "").toLowerCase();
    if (label.includes("actual")) return "actual";
    if (label.includes("plan")) return "planned";
    return index === 0 ? "actual" : index === 1 ? "planned" : null;
  } catch {
    return null;
  }
}

/** Installs the bridge listener on `win.document`. Returns an uninstall function (for tests). */
export function installBridge(win: BridgeWindow, options: BridgeOptions = {}): () => void {
  const doc = win.document;
  const timeoutMs = options.timeoutMs ?? BRIDGE_MOLFILE_TIMEOUT_MS;
  try {
    if ((win as any)[INSTALLED]) return () => {};
    Object.defineProperty(win, INSTALLED, { value: true, configurable: true });
  } catch {
    return () => {};
  }

  let chain: Promise<void> = Promise.resolve();

  function respond(response: BridgeResponse): void {
    try {
      doc.dispatchEvent(new win.CustomEvent(BRIDGE_RESPONSE_EVENT, { detail: JSON.stringify(response) }));
    } catch {
      // nothing else to do: never throw into the page
    }
  }

  function fail(id: string, code: BridgeErrorCode, message: string): BridgeResponse {
    return { id, ok: false, code, message };
  }

  async function readMolfile(id: string, domId: string): Promise<BridgeResponse> {
    if (!hasZk(win)) return fail(id, "no_zk", "This frame has no ELN page scripts.");
    const element = doc.getElementById(domId);
    if (!element) return fail(id, "no_widget", "The structure is no longer on the page.");
    let widget: any;
    try {
      widget = win.zk.Widget.$(element);
    } catch {
      widget = null;
    }
    if (!widget || typeof widget.fire !== "function") return fail(id, "no_widget", "The structure is no longer on the page.");

    const cmd0 = win.zAu.cmd0;
    const original: AnyFn = cmd0.download;
    let captured = false;
    let resolveUrl: (url: string | null) => void = () => {};
    const got = new Promise<string | null>((resolve) => {
      resolveUrl = resolve;
    });
    const hook = function (this: unknown, ...args: unknown[]): unknown {
      const url = captured ? null : molfileDownloadUrl(args[0], win.location);
      if (url !== null) {
        captured = true;
        restore();
        resolveUrl(url);
        return undefined;
      }
      // Not ours (e.g. a download the user started): pass it on untouched.
      return original.apply(this, args);
    };
    const restore = (): void => {
      try {
        if (cmd0.download === hook) cmd0.download = original;
      } catch {
        // ignore
      }
    };
    // One time limit for the whole read: the wait for the server's download
    // command and the download itself.
    const timedOut = (): BridgeResponse => fail(id, "timeout", "The ELN server did not send the structure in time.");
    let expired = false;
    let expire: () => void = () => {};
    const expiry = new Promise<null>((resolve) => {
      expire = () => resolve(null);
    });
    let controller: AbortController | null = null;
    try {
      controller = typeof win.AbortController === "function" ? new win.AbortController() : null;
    } catch {
      controller = null;
    }
    const timer = win.setTimeout(() => {
      expired = true;
      resolveUrl(null);
      expire();
      try {
        controller?.abort();
      } catch {
        // ignore
      }
    }, timeoutMs);

    try {
      let url: string | null;
      try {
        cmd0.download = hook;
        widget.fire("onGetMolfile", [null], { toServer: true });
        url = await got;
      } catch (error) {
        return fail(id, "unexpected", errorMessage(error));
      } finally {
        restore();
      }
      if (url === null) return timedOut();

      const download = async (downloadUrl: string): Promise<BridgeResponse> => {
        let text: string;
        try {
          const response = await win.fetch(downloadUrl, {
            credentials: "same-origin",
            mode: "same-origin",
            ...(controller ? { signal: controller.signal } : {}),
          });
          if (!response.ok) return fail(id, "fetch_failed", `The structure download failed (HTTP ${response.status}).`);
          const length = Number(response.headers?.get?.("content-length") ?? NaN);
          if (Number.isFinite(length) && length > MAX_STRUCTURE_CHARS * 4) {
            return fail(id, "too_large", "The structure is too large.");
          }
          text = await response.text();
        } catch (error) {
          if (expired) return timedOut();
          return fail(id, "fetch_failed", `The structure download failed: ${errorMessage(error)}`);
        }
        if (typeof text !== "string" || text.length > MAX_STRUCTURE_CHARS) {
          return fail(id, "too_large", "The structure is too large.");
        }
        return { id, ok: true, kind: "molfile", molfile: text };
      };
      return (await Promise.race([download(url), expiry])) ?? timedOut();
    } finally {
      win.clearTimeout(timer);
    }
  }

  function onRequest(event: Event): void {
    let id = "";
    try {
      const detail = (event as CustomEvent).detail;
      if (typeof detail !== "string" || detail.length > MAX_REQUEST_DETAIL) {
        respond(fail(id, "bad_request", "Unreadable request."));
        return;
      }
      const request = JSON.parse(detail) as unknown;
      if (!request || typeof request !== "object") {
        respond(fail(id, "bad_request", "Unreadable request."));
        return;
      }
      const r = request as { id?: unknown; kind?: unknown; domId?: unknown };
      if (typeof r.id !== "string" || r.id === "" || r.id.length > 200) {
        respond(fail(id, "bad_request", "Unreadable request."));
        return;
      }
      id = r.id;
      if (r.kind === "hello") {
        respond({ id, ok: true, kind: "hello", zk: hasZk(win) });
        return;
      }
      if (r.kind === "molfile" && typeof r.domId === "string" && r.domId !== "" && r.domId.length <= 200) {
        const domId = r.domId;
        const requestId = id;
        // Strictly one at a time.
        chain = chain.then(async () => {
          let response: BridgeResponse;
          try {
            response = await readMolfile(requestId, domId);
          } catch (error) {
            response = fail(requestId, "unexpected", errorMessage(error));
          }
          respond(response);
        });
        return;
      }
      if (r.kind === "reaction") {
        // Read-only and synchronous: it never touches the download hook, so it does not wait for the molfile chain.
        if (!hasWidgetLookup(win)) {
          respond(fail(id, "no_zk", "This frame has no ELN page scripts."));
          return;
        }
        let reaction: PageReaction | null;
        try {
          reaction = readPageReaction(win);
        } catch (error) {
          respond(fail(id, "unexpected", errorMessage(error)));
          return;
        }
        respond(
          reaction === null
            ? fail(id, "no_widget", "No experiment with a reaction is shown in this frame.")
            : { id, ok: true, kind: "reaction", reaction },
        );
        return;
      }
      respond(fail(id, "bad_request", "Unknown request."));
    } catch (error) {
      respond(fail(id, "bad_request", errorMessage(error)));
    }
  }

  doc.addEventListener(BRIDGE_REQUEST_EVENT, onRequest);
  return () => {
    doc.removeEventListener(BRIDGE_REQUEST_EVENT, onRequest);
    try {
      delete (win as any)[INSTALLED];
    } catch {
      // ignore
    }
  };
}
