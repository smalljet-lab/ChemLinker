/**
 * The "Find experiments by ID" search. Read-only (`find`), independent of the
 * product cards: it never changes an analysis and never registers anything.
 * Only the answer to the latest search is used.
 */

import type { BackgroundReply, FindResult, HostError } from "../shared/protocol";
import type { HostClient } from "./host-client";

export interface SearchState {
  /** The search running now. */
  pending: { seq: number; query: string } | null;
  latestSeq: number;
  /** The query whose answer is shown. */
  query: string | null;
  result: FindResult | null;
  error: HostError | null;
}

export interface SearchHooks {
  onChange(): void;
  announce?(message: string): void;
}

export class SearchController {
  private s: SearchState = { pending: null, latestSeq: 0, query: null, result: null, error: null };

  constructor(
    private readonly client: HostClient,
    private readonly hooks: SearchHooks,
  ) {}

  get state(): Readonly<SearchState> {
    return this.s;
  }

  private update(patch: Partial<SearchState>): void {
    this.s = { ...this.s, ...patch };
    this.hooks.onChange();
  }

  /** Sends `find` with the trimmed text. A newer search replaces an older one's answer. */
  async search(text: string): Promise<void> {
    const query = text.trim();
    if (query === "") return;
    const seq = this.s.latestSeq + 1;
    this.update({ latestSeq: seq, pending: { seq, query } });
    this.hooks.announce?.(`Searching for ${query}…`);
    let reply: BackgroundReply<"find">;
    try {
      reply = await this.client.request("find", { id: query });
    } catch (error) {
      reply = { ok: false, error: { code: "internal_error", message: String(error) } };
    }
    if (seq !== this.s.latestSeq) return; // an older search: discard
    if (reply.ok) {
      this.update({ pending: null, query, result: reply.result, error: null });
      const n = reply.result.experiments.length;
      this.hooks.announce?.(`${n} ${n === 1 ? "experiment" : "experiments"} found for ${query}.`);
    } else {
      this.update({ pending: null, query, result: null, error: reply.error });
      this.hooks.announce?.(`Search failed: ${reply.error.message}`);
    }
  }

  /** Removes the shown answer (and drops the answer of a search still running). */
  clear(): void {
    this.update({ latestSeq: this.s.latestSeq + 1, pending: null, query: null, result: null, error: null });
  }
}
