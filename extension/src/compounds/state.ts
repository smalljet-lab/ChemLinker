/**
 * State of the compounds page: which registry is in use (`ping`), the list of
 * compounds for the current search, sort order and page (`compounds`), and the
 * stereo variants of expanded entries (`find` on the CORE). Read-only, except
 * for names (`set_name` / `generate_name`, through the shared NameEditor). No DOM.
 *
 * Only the answer to the latest list request is used; a list reload drops the
 * expanded details. A `registry_changed` answer to ANY request means the host
 * now uses another registry: ping again and reload the list (once; if the
 * reload is refused the same way, the error is shown with Retry).
 */

import {
  DEFAULT_PAGE_SIZE,
  DEFAULT_YIELD_PAGE_SIZE,
  MAX_SEARCH_TEXT,
  MAX_YIELD_PAGE_SIZE,
  REGISTRY_CHANGED,
  type BackgroundReply,
  type CompoundsParams,
  type CompoundsResult,
  type FindResult,
  type HostError,
  type MatchMode,
  type Op,
  type ParamsByOp,
  type SetNameResult,
  type YieldRow,
  type YieldsParams,
} from "../shared/protocol";
import type { HostClient } from "../content/host-client";
import { pingStateFromReply, type PingState } from "../content/ping-state";
import { formatPercent } from "../content/yields";

export interface SearchQuery {
  /** Trimmed search text, or null. */
  text: string | null;
  /** The drawn query structure, or null. */
  molfile: string | null;
  match: MatchMode;
}

export const NO_SEARCH: SearchQuery = { text: null, molfile: null, match: "substructure" };

export function isSearch(query: SearchQuery): boolean {
  return query.text !== null || query.molfile !== null;
}

export type Order = "asc" | "desc";

export type Expansion =
  | { status: "loading" }
  | { status: "ok"; result: FindResult }
  | { status: "error"; error: HostError };

export interface ListRequest {
  query: SearchQuery;
  order: Order;
  offset: number;
  limit: number;
}

export interface CompoundsState {
  ping: PingState;
  /** What is being shown or loaded now. */
  request: ListRequest;
  loading: boolean;
  /** The answer to `shown` (kept while the next page loads). */
  result: CompoundsResult | null;
  shown: ListRequest | null;
  error: HostError | null;
  /** By `core_id`. */
  expanded: ReadonlyMap<string, Expansion>;
  /** Set after the list was reloaded because the registry configuration changed. */
  registryChanged: boolean;
}

export interface CompoundsHooks {
  onChange(): void;
  announce?(message: string): void;
}

export function listParams(request: ListRequest): CompoundsParams {
  const params: CompoundsParams = { order: request.order, offset: request.offset, limit: request.limit };
  if (request.query.text !== null) params.text = request.query.text;
  if (request.query.molfile !== null) {
    params.molfile = request.query.molfile;
    params.match = request.query.match;
  }
  return params;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "12 of 340 compounds match the search." / "340 compounds." / the empty and no-match cases. */
export function resultLine(result: CompoundsResult, searched: boolean): string {
  if (result.registry_total === 0) return "The registry has no compounds yet.";
  if (searched && result.total === 0) return "No compound matches this search.";
  const head = searched
    ? `${result.total} of ${plural(result.registry_total, "compound", "compounds")} ${result.total === 1 ? "matches" : "match"} the search.`
    : `${plural(result.total, "compound", "compounds")}.`;
  if (result.items.length === 0 || (result.total <= result.items.length && result.offset === 0)) return head;
  return `${head} Showing ${result.offset + 1}–${result.offset + result.items.length}.`;
}

/** "Page 2 of 15". */
export function pageInfo(result: CompoundsResult): { page: number; pages: number } {
  const limit = Math.max(1, result.limit);
  return { page: Math.floor(result.offset / limit) + 1, pages: Math.max(1, Math.ceil(result.total / limit)) };
}

export class CompoundsController {
  private s: CompoundsState = {
    ping: { kind: "idle" },
    request: { query: NO_SEARCH, order: "asc", offset: 0, limit: DEFAULT_PAGE_SIZE },
    loading: false,
    result: null,
    shown: null,
    error: null,
    expanded: new Map(),
    registryChanged: false,
  };
  private pingSeq = 0;
  private listSeq = 0;
  /** Bumped by every list (re)load: details requested before it are dropped. */
  private generation = 0;
  private detailSeq = new Map<string, number>();
  /** True from an automatic reload (registry changed) until a list answer arrives. */
  private autoReload = false;
  /** Every request goes through here (see `registry_changed`). */
  readonly client: HostClient;

  constructor(
    client: HostClient,
    private readonly hooks: CompoundsHooks,
    /** This extension's version, compared with the registry program's; default: the version of this build. */
    private readonly extensionVersion: string | null | undefined = undefined,
  ) {
    this.client = {
      request: async <K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> => {
        let reply: BackgroundReply<K>;
        try {
          reply = await client.request(op, params);
        } catch (error) {
          reply = { ok: false, error: { code: "internal_error", message: String(error) } };
        }
        if (!reply.ok && reply.error.code === REGISTRY_CHANGED) this.onRegistryChanged();
        return reply;
      },
    };
  }

  get state(): Readonly<CompoundsState> {
    return this.s;
  }

  private update(patch: Partial<CompoundsState>): void {
    this.s = { ...this.s, ...patch };
    this.hooks.onChange();
  }

  /** First load: ping and the first page. */
  start(): void {
    void this.runPing();
    void this.load(this.s.request);
  }

  /** Retry after an error: ping again and reload the list. */
  retry(): void {
    this.autoReload = false;
    void this.runPing();
    void this.load(this.s.request);
  }

  /** A new search (or none): first page, details collapsed. */
  search(query: SearchQuery): void {
    void this.load({ ...this.s.request, query, offset: 0 });
  }

  setOrder(order: Order): void {
    if (order === this.s.request.order) return;
    void this.load({ ...this.s.request, order, offset: 0 });
  }

  goToOffset(offset: number): void {
    void this.load({ ...this.s.request, offset: Math.max(0, offset) });
  }

  private onRegistryChanged(): void {
    if (this.autoReload) return; // one automatic reload; a second refusal is shown with Retry
    this.autoReload = true;
    this.hooks.announce?.("The registry configuration changed. Checking which registry is now in use and reloading the list.");
    this.update({ registryChanged: true });
    void this.runPing();
    void this.load(this.s.request);
  }

  private async runPing(): Promise<void> {
    const seq = ++this.pingSeq;
    this.update({ ping: { kind: "pending" } });
    const reply = await this.client.request("ping", {});
    if (seq !== this.pingSeq) return;
    const ping = pingStateFromReply(reply, undefined, this.extensionVersion);
    this.update({ ping });
    if (ping.kind === "error") this.hooks.announce?.(`Registry program not reachable: ${ping.error.message}`);
    else if (ping.kind === "mismatch") this.hooks.announce?.("Extension and registry program do not match.");
  }

  private async load(request: ListRequest): Promise<void> {
    const seq = ++this.listSeq;
    this.generation += 1;
    this.detailSeq.clear();
    this.update({ request, loading: true, error: null, expanded: new Map() });
    const reply = await this.client.request("compounds", listParams(request));
    if (seq !== this.listSeq) return; // an older request: discard
    // The notice stays for the list loaded after the change; a refusal of that
    // reload (wrapper above saw autoReload set) ends the automatic cycle.
    const afterChange = this.autoReload;
    this.autoReload = false;
    if (reply.ok) {
      this.update({ loading: false, result: reply.result, shown: request, error: null, registryChanged: afterChange });
      this.hooks.announce?.(resultLine(reply.result, isSearch(request.query)));
    } else {
      this.update({ loading: false, result: null, shown: request, error: reply.error, registryChanged: false });
      this.hooks.announce?.(`The compounds could not be loaded: ${reply.error.message}`);
    }
  }

  /** Expands or collapses one CORE; expanding loads its stereo variants (`find`). */
  toggle(coreId: string): void {
    if (this.s.expanded.has(coreId)) {
      const expanded = new Map(this.s.expanded);
      expanded.delete(coreId);
      this.detailSeq.delete(coreId);
      this.update({ expanded });
      return;
    }
    void this.loadDetails(coreId);
  }

  isExpanded(coreId: string): boolean {
    return this.s.expanded.has(coreId);
  }

  /** Loads (or reloads after an error) the stereo variants of one CORE. */
  async loadDetails(coreId: string): Promise<void> {
    const generation = this.generation;
    const seq = (this.detailSeq.get(coreId) ?? 0) + 1;
    this.detailSeq.set(coreId, seq);
    this.setExpansion(coreId, { status: "loading" });
    const reply = await this.client.request("find", { id: coreId });
    if (generation !== this.generation || this.detailSeq.get(coreId) !== seq) return;
    this.setExpansion(coreId, reply.ok ? { status: "ok", result: reply.result } : { status: "error", error: reply.error });
    if (!reply.ok) this.hooks.announce?.(`The stereo variants of ${coreId} could not be loaded: ${reply.error.message}`);
  }

  private setExpansion(coreId: string, expansion: Expansion): void {
    const expanded = new Map(this.s.expanded);
    expanded.set(coreId, expansion);
    this.update({ expanded });
  }

  /** A name was stored (`set_name` or `generate_name`): show it in the list entry and in the expanded details. */
  nameStored(result: Pick<SetNameResult, "core_stereo_id" | "name">): void {
    const id = result.core_stereo_id;
    let changed = false;
    let list = this.s.result;
    if (list && list.items.some((item) => item.variants.some((v) => v.core_stereo_id === id))) {
      list = {
        ...list,
        items: list.items.map((item) =>
          item.variants.some((v) => v.core_stereo_id === id)
            ? { ...item, variants: item.variants.map((v) => (v.core_stereo_id === id ? { ...v, name: result.name } : v)) }
            : item,
        ),
      };
      changed = true;
    }
    const expanded = new Map(this.s.expanded);
    for (const [coreId, expansion] of this.s.expanded) {
      if (expansion.status !== "ok" || !expansion.result.variants.some((v) => v.core_stereo_id === id)) continue;
      expanded.set(coreId, {
        status: "ok",
        result: {
          ...expansion.result,
          variants: expansion.result.variants.map((v) => (v.core_stereo_id === id ? { ...v, name: result.name } : v)),
        },
      });
      changed = true;
    }
    if (changed) this.update({ result: list, expanded });
  }
}

// ------------------------------------------------------------------ yields

/** The filter of the yields table. */
export interface YieldsFilter {
  /** Trimmed text (ELN number, ID or name), or null. */
  text: string | null;
  /** Only rows that have a yield. */
  onlyRecorded: boolean;
}

export const NO_YIELDS_FILTER: YieldsFilter = { text: null, onlyRecorded: false };

export interface YieldsState {
  /** The filter of the rows shown (or being loaded). */
  filter: YieldsFilter;
  /** "list": the first page for `filter`; "more": further rows. */
  loading: "list" | "more" | null;
  /** Rows loaded so far for `filter`, in the host's order. */
  rows: readonly YieldRow[];
  /** Rows matching `filter` (null before the first answer). */
  total: number | null;
  /** Batches in the registry. */
  registryTotal: number | null;
  warnings: string[];
  error: HostError | null;
}

export function yieldsParams(filter: YieldsFilter, offset: number, limit: number): YieldsParams {
  const params: YieldsParams = { offset, limit };
  if (filter.text !== null) params.text = filter.text;
  if (filter.onlyRecorded) params.only_recorded = true;
  return params;
}

export function isYieldsFiltered(filter: YieldsFilter): boolean {
  return filter.text !== null || filter.onlyRecorded;
}

/** "12 of 40 products" / "40 products", plus how many are shown when not all are loaded. */
export function yieldsCountLine(state: Pick<YieldsState, "filter" | "rows" | "total" | "registryTotal">): string {
  if (state.total === null || state.registryTotal === null) return "";
  const products = (n: number) => `${n} ${n === 1 ? "product" : "products"}`;
  const head = isYieldsFiltered(state.filter)
    ? `${state.total} of ${products(state.registryTotal)}`
    : products(state.total);
  return state.rows.length < state.total ? `${head} (showing ${state.rows.length})` : head;
}

/** The ELN number, else the internal key in brackets. */
export function yieldExperimentText(row: Pick<YieldRow, "experiment_ref" | "experiment_code">): string {
  const code = row.experiment_code?.trim() ?? "";
  return code !== "" ? code : `[${row.experiment_ref}]`;
}

export const YIELD_EXPORT_HEADER = [
  "Experiment",
  "Product",
  "Compound ID",
  "Name",
  "Isolated yield (%)",
  "Analytical yield (%)",
  "Retired",
] as const;

/**
 * A text cell that a spreadsheet would read as a formula (=, +, -, @) gets a
 * leading apostrophe, so that opening the file never runs anything.
 */
export function guardText(text: string): string {
  return /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
}

/** Rows of text cells as tab-separated text (pastes straight into a spreadsheet). */
export function tsvText(rows: readonly (readonly string[])[]): string {
  const cell = (text: string): string => {
    const flat = text.replace(/[\t\r\n]+/g, " ");
    return flat.includes('"') ? `"${flat.replace(/"/g, '""')}"` : flat;
  };
  return rows.map((r) => r.map(cell).join("\t")).join("\n") + "\n";
}

/** Put before a CSV text so that spreadsheet programs read it as UTF-8. */
export const BOM = "\uFEFF";

/** Rows of text cells as comma-separated text (RFC 4180 quoting, CRLF); without the byte order mark. */
export function csvText(rows: readonly (readonly string[])[]): string {
  const cell = (text: string): string =>
    /[",\r\n]/.test(text) || text !== text.trim() ? `"${text.replace(/"/g, '""')}"` : text;
  return rows.map((r) => r.map(cell).join(",")).join("\r\n") + "\r\n";
}

/** One row of the export, as text cells (numbers without trailing zeros, empty when none). */
export function yieldExportCells(row: YieldRow): string[] {
  const isolated = row.yield?.isolated ?? null;
  const analytical = row.yield?.analytical ?? null;
  return [
    guardText(yieldExperimentText(row)),
    String(row.product_index),
    guardText(row.full_id),
    guardText(row.name ?? ""),
    isolated === null ? "" : formatPercent(isolated),
    analytical === null ? "" : formatPercent(analytical),
    row.retired ? "yes" : "",
  ];
}

/** Tab-separated, with a header line: pastes straight into a spreadsheet. */
export function yieldsTsv(rows: readonly YieldRow[]): string {
  return tsvText([YIELD_EXPORT_HEADER, ...rows.map(yieldExportCells)]);
}

/** Comma-separated (RFC 4180 quoting), with a header line; without the byte order mark. */
export function yieldsCsv(rows: readonly YieldRow[]): string {
  return csvText([YIELD_EXPORT_HEADER, ...rows.map(yieldExportCells)]);
}

export type FetchAllOutcome = { ok: true; rows: readonly YieldRow[] } | { ok: false; error: HostError } | null;

/**
 * The yields table (`yields`): the first page for a filter, "Show more", and
 * every remaining page for an export. Read-only. Only answers for the current
 * filter are used.
 */
export class YieldsController {
  private s: YieldsState = {
    filter: NO_YIELDS_FILTER,
    loading: null,
    rows: [],
    total: null,
    registryTotal: null,
    warnings: [],
    error: null,
  };
  /** Bumped by every new filter (and reload): answers for an older one are dropped. */
  private generation = 0;

  constructor(
    private readonly client: HostClient,
    private readonly hooks: CompoundsHooks,
    private readonly pageSize: number = DEFAULT_YIELD_PAGE_SIZE,
  ) {}

  get state(): Readonly<YieldsState> {
    return this.s;
  }

  private update(patch: Partial<YieldsState>): void {
    this.s = { ...this.s, ...patch };
    this.hooks.onChange();
  }

  /** Loads the first page for `filter` (default: the current one again). */
  load(filter: YieldsFilter = this.s.filter): void {
    const text = filter.text?.trim().slice(0, MAX_SEARCH_TEXT) ?? "";
    const clean: YieldsFilter = { text: text === "" ? null : text, onlyRecorded: filter.onlyRecorded };
    void this.loadFirst(clean);
  }

  private async loadFirst(filter: YieldsFilter): Promise<void> {
    const generation = ++this.generation;
    this.update({ filter, loading: "list", rows: [], total: null, registryTotal: null, warnings: [], error: null });
    const reply = await this.client.request("yields", yieldsParams(filter, 0, this.pageSize));
    if (generation !== this.generation) return;
    if (reply.ok) {
      this.update({
        loading: null,
        rows: reply.result.items,
        total: reply.result.total,
        registryTotal: reply.result.registry_total,
        warnings: reply.result.warnings,
      });
      this.hooks.announce?.(yieldsCountLine(this.s));
    } else {
      this.update({ loading: null, error: reply.error });
      this.hooks.announce?.(`The yields could not be loaded: ${reply.error.message}`);
    }
  }

  /** True when the host has more rows for the current filter than are loaded. */
  hasMore(): boolean {
    return this.s.total !== null && this.s.rows.length < this.s.total;
  }

  /** "Show more": the next page. */
  more(): void {
    if (this.s.loading !== null || !this.hasMore()) return;
    void this.nextPage(this.pageSize);
  }

  /** Loads one more page; false when it failed, was superseded, or brought nothing new. */
  private async nextPage(limit: number): Promise<boolean> {
    const generation = this.generation;
    this.update({ loading: "more", error: null });
    const reply = await this.client.request("yields", yieldsParams(this.s.filter, this.s.rows.length, limit));
    if (generation !== this.generation) return false;
    if (!reply.ok) {
      this.update({ loading: null, error: reply.error });
      this.hooks.announce?.(`More yields could not be loaded: ${reply.error.message}`);
      return false;
    }
    // Rows may have been added meanwhile: never list one batch twice.
    const known = new Set(this.s.rows.map((r) => r.full_id));
    const fresh = reply.result.items.filter((r) => !known.has(r.full_id));
    this.update({
      loading: null,
      rows: [...this.s.rows, ...fresh],
      total: reply.result.total,
      registryTotal: reply.result.registry_total,
      warnings: reply.result.warnings,
    });
    return fresh.length > 0;
  }

  /**
   * Every row matching the current filter, for an export: loads the pages not
   * loaded yet first. Null when the filter changed meanwhile.
   */
  async fetchAll(): Promise<FetchAllOutcome> {
    if (this.s.loading !== null) {
      return { ok: false, error: { code: "busy", message: "The table is still loading. Try again in a moment." } };
    }
    if (this.s.error && this.s.total === null) return { ok: false, error: this.s.error };
    const generation = this.generation;
    while (this.hasMore()) {
      const progressed = await this.nextPage(MAX_YIELD_PAGE_SIZE);
      if (generation !== this.generation) return null;
      if (this.s.error) return { ok: false, error: this.s.error };
      if (!progressed) break;
    }
    return { ok: true, rows: this.s.rows };
  }
}
