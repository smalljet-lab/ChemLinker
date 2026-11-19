/** Fakes for the import of the compounds page: a registry with `yields`, the browser's tabs, and answers of the content script. */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { HostClient } from "../src/content/host-client";
import type { ImportTabs, ImportTimers } from "../src/compounds/import";
import type { ImportApplyReply, ImportApplyRequest, ImportOutcome, ImportPageProduct, ImportRequest, ImportStatusReply } from "../src/shared/import";
import type { BackgroundReply, Op, ParamsByOp, YieldRow, YieldsParams } from "../src/shared/protocol";

export const FIXTURE = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "eln-experiments.csv"));

/** pk -> ELN number of the fixture. */
export const FIXTURE_REFS: Record<string, string> = {
  "4110": "ABC-001",
  "4120": "ABC-002",
  "4130": "ABC-003",
  "4131": "ABC-004",
  "4150": "ABC-005",
  "4160": "ABC-006",
  "4170": "ABC-007",
  "4180": "ABC-008",
  "4190": "ABC-009",
};

export function yieldRow(ref: string, index: number, id: string, over: Partial<YieldRow> = {}): YieldRow {
  return {
    experiment_ref: ref,
    experiment_code: FIXTURE_REFS[ref] ?? null,
    experiment_url: null,
    product_index: index,
    full_id: id,
    core_stereo_id: id.slice(0, 9),
    name: null,
    yield: null,
    created_at: "2026-11-01T10:00:00+00:00",
    retired: false,
    ...over,
  };
}

/** A host that answers `yields` at once from `rows`, page by page; every other op is refused. */
export class RegistryClient implements HostClient {
  calls: { op: Op; params: unknown }[] = [];
  constructor(public rows: YieldRow[] = []) {}
  async request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    this.calls.push({ op, params });
    if (op !== "yields") return { ok: false, error: { code: "not_allowed", message: `no ${op} here` } } as BackgroundReply<K>;
    const p = params as YieldsParams;
    const offset = p.offset ?? 0;
    const limit = p.limit ?? 200;
    const items = this.rows.slice(offset, offset + limit);
    return {
      ok: true,
      result: { total: this.rows.length, registry_total: this.rows.length, offset, limit, items, warnings: [] },
    } as unknown as BackgroundReply<K>;
  }
}

export function pageProduct(index: number, isolated: number | null, over: Partial<ImportPageProduct> = {}): ImportPageProduct {
  return { index, structure: true, problem: null, yields: { isolated, analytical: null }, ...over };
}

export function ready(ref: string, products: ImportPageProduct[], code: string | null = FIXTURE_REFS[ref] ?? null): ImportStatusReply {
  return { state: "ready", experiment: { ref, code }, products };
}

export function outcome(index: number, over: Partial<ImportOutcome> = {}): ImportOutcome {
  return {
    index,
    kind: "would_create",
    id: null,
    core_stereo_id: null,
    status: "new",
    compound_key: `key-${index}`,
    inchikey: null,
    formula: null,
    stereo_mode: "abs",
    core_created: false,
    stereo_created: false,
    message: null,
    notes: [],
    yields: null,
    yield_result: "none",
    name: null,
    ...over,
  };
}

/** What the content script of one experiment answers. */
export interface FakePage {
  /** The n-th status answer (0-based) since the tab was sent to this experiment; undefined = no answer. */
  status?: (n: number) => unknown;
  apply?: (request: ImportApplyRequest) => unknown;
}

/** Fixture listing: the products of each experiment with their isolated yields (for default answers). */
export const FIXTURE_PRODUCTS: Record<string, (number | null)[]> = {
  "4110": [52.38],
  "4120": [17.25, 58.62],
  "4130": [37.84],
  "4131": [64.55],
  "4150": [0],
  "4160": [76.18, 76.18],
  "4170": [null],
  "4180": [69.97],
  "4190": [55.12],
};

/** Shows the experiment as the list has it (yields rounded as the ELN shows them), ready at once. */
export function defaultPage(ref: string): FakePage {
  const products = (FIXTURE_PRODUCTS[ref] ?? []).map((y, i) => pageProduct(i + 1, y === null ? null : Math.round(y * 10) / 10));
  return {
    status: () => ready(ref, products),
    apply: (request): ImportApplyReply => ({
      ok: true,
      experiment: { ref, code: FIXTURE_REFS[ref] ?? null },
      products: request.products.map((index) =>
        outcome(index, {
          kind: request.dry_run ? "would_create" : "created",
          id: request.dry_run ? null : `${ref}_${index}`,
          compound_key: `${ref}-${index}`,
          yields: products[index - 1]?.yields ?? null,
        }),
      ),
      reaction: request.dry_run ? "none" : "stored",
      reaction_message: null,
    }),
  };
}

export type TabEvent =
  | { op: "create"; url: string; active: boolean; id: number }
  | { op: "update"; id: number; url: string }
  | { op: "remove"; id: number };

/** The browser's tabs: one tab per create; messages are answered by the page of the experiment the tab shows. */
export class FakeTabs implements ImportTabs {
  events: TabEvent[] = [];
  sent: { tabId: number; message: ImportRequest }[] = [];
  open = new Set<number>();
  pages = new Map<string, FakePage>();
  private urls = new Map<number, string>();
  private asked = new Map<number, number>();
  private listeners = new Set<(tabId: number) => void>();
  private nextId = 41;

  page(ref: string, page: FakePage): this {
    this.pages.set(ref, page);
    return this;
  }

  async create(props: { url: string; active: boolean }): Promise<{ id?: number }> {
    const id = this.nextId++;
    this.open.add(id);
    this.urls.set(id, props.url);
    this.asked.set(id, 0);
    this.events.push({ op: "create", url: props.url, active: props.active, id });
    return { id };
  }

  async update(tabId: number, props: { url: string }): Promise<unknown> {
    if (!this.open.has(tabId)) throw new Error(`no tab ${tabId}`);
    this.urls.set(tabId, props.url);
    this.asked.set(tabId, 0);
    this.events.push({ op: "update", id: tabId, url: props.url });
    return {};
  }

  async sendMessage(tabId: number, message: ImportRequest): Promise<unknown> {
    this.sent.push({ tabId, message });
    if (!this.open.has(tabId)) throw new Error("Could not establish connection. Receiving end does not exist.");
    const ref = new URL(this.urls.get(tabId)!).searchParams.get("pk")!;
    const page = this.pages.get(ref) ?? defaultPage(ref);
    const fallback = defaultPage(ref);
    if (message.type === "import-status") {
      const n = this.asked.get(tabId)!;
      this.asked.set(tabId, n + 1);
      return (page.status ?? fallback.status!)(n);
    }
    return (page.apply ?? fallback.apply!)(message);
  }

  async remove(tabId: number): Promise<void> {
    this.events.push({ op: "remove", id: tabId });
    this.open.delete(tabId);
  }

  onRemoved(listener: (tabId: number) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The user closes a tab. */
  close(tabId: number): void {
    this.open.delete(tabId);
    for (const l of [...this.listeners]) l(tabId);
  }

  listenerCount(): number {
    return this.listeners.size;
  }

  statusRequests(ref?: string): number {
    return this.sent.filter((s) => s.message.type === "import-status" && (ref === undefined || s.message.ref === ref)).length;
  }

  applies(): ImportApplyRequest[] {
    return this.sent.map((s) => s.message).filter((m): m is ImportApplyRequest => m.type === "import-apply");
  }
}

/** The page's timers on vitest's fake clock. */
export const fakeTimers: ImportTimers = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function refOfUrl(url: string): string {
  return new URL(url).searchParams.get("pk")!;
}
