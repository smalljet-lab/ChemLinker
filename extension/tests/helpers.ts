import type { ExperimentContext, ProductInput, ProductSource, SourceState } from "../src/shared/adapter";
import type {
  AnalyzeResult,
  BackgroundReply,
  Batch,
  CompoundItem,
  CompoundsResult,
  CompoundVariant,
  FindResult,
  FoundVariant,
  GenerateNameResult,
  NameInfo,
  Op,
  ParamsByOp,
  PingResult,
  Properties,
  RegisterResult,
  SetNameResult,
  SpectrumInfo,
  StructureInfo,
} from "../src/shared/protocol";
import type { HostClient } from "../src/content/host-client";

export interface FakeCall {
  op: Op;
  params: unknown;
  resolve: (reply: BackgroundReply) => void;
}

/** A host client whose requests stay pending until the test resolves them. */
export class FakeClient implements HostClient {
  calls: FakeCall[] = [];
  request<K extends Op>(op: K, params: ParamsByOp[K]): Promise<BackgroundReply<K>> {
    return new Promise((resolve) => {
      this.calls.push({ op, params, resolve: resolve as (reply: BackgroundReply) => void });
    });
  }
  callsOf(op: Op): FakeCall[] {
    return this.calls.filter((c) => c.op === op);
  }
  last(op?: Op): FakeCall {
    const list = op ? this.callsOf(op) : this.calls;
    const call = list[list.length - 1];
    if (!call) throw new Error(`no ${op ?? ""} call`);
    return call;
  }
}

/** Waits for pending promise callbacks. */
export async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

export function structureInfo(over: Partial<StructureInfo> = {}): StructureInfo {
  return {
    inchikey: "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    key_digest: "d1".repeat(32),
    core_inchikey: "LFQSCWFLJHTTHZ-UHFFFAOYSA",
    formula: "C2H6O",
    smiles: "CCO",
    inchi: "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
    stereo_mode: "abs",
    detected_mode: "abs",
    mode_overridden: false,
    warnings: [],
    ignored_stereo: [],
    svg: "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><rect width='10' height='10'/></svg>",
    ...over,
  };
}

export function batch(over: Partial<Batch> = {}): Batch {
  return {
    full_id: "000042_01_001",
    core_no: 42,
    stereo_no: 1,
    batch_no: 1,
    core_stereo_id: "000042_01",
    experiment_ref: "4125",
    experiment_code: "ABC-001",
    experiment_url: null,
    product_index: 1,
    smiles: "CCO",
    inchikey: "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    stereo_mode: "abs",
    source: "extension",
    created_at: "2026-11-01T10:00:00+00:00",
    rdkit_version: "2026.09.2",
    inchi_version: "1.07",
    retired_at: null,
    retired_reason: null,
    retired: false,
    ...over,
  };
}

export function analyzeResult(
  over: Partial<Omit<AnalyzeResult, "structure">> & { structure?: Partial<StructureInfo> } = {},
): AnalyzeResult {
  const { structure, ...rest } = over;
  return {
    structure: structureInfo(structure),
    status: "new",
    id: null,
    core_stereo_id: null,
    core_no: null,
    batches: [],
    other_stereos: [],
    slot_conflict: null,
    mode_options: ["abs", "rac", "rel", "undefined"],
    warnings: [],
    ...rest,
  };
}

export function registerResult(over: Partial<RegisterResult> = {}): RegisterResult {
  return {
    id: "000042_01_002",
    created: true,
    core_created: false,
    stereo_created: false,
    batch: batch({ full_id: "000042_01_002", batch_no: 2 }),
    structure: structureInfo({ svg: null }),
    accepted_ignored_stereo: [],
    warnings: [],
    backup: { performed: false, path: null, warning: null, removed: [] },
    ...over,
  };
}

export function pingResult(over: Partial<PingResult> = {}): PingResult {
  return {
    protocol_version: 1,
    host_version: "0.1.0",
    home_source: "default",
    home: "C:\\Users\\x\\CompoundRegistry",
    db_path: "C:\\Users\\x\\CompoundRegistry\\registry.sqlite",
    db_exists: true,
    id_format: "{core:06d}_{stereo:02d}_{batch:03d}",
    default_stereo_mode: "abs",
    mode_options: ["abs", "rac", "rel", "undefined"],
    stats: {},
    integrity: "ok",
    rdkit_version: "2026.09.2",
    inchi_version: "1.07",
    chemistry_fingerprint: { stored: null, current: "x", changed: false },
    python: "3.12.0",
    warnings: [],
    ...over,
  };
}

export const MOLFILE = "\n  Mrv2014 11172610002D\n\n  3  2  0  0  0  0            999 V2000\n    0.0000    0.0000    0.0000 C   0  0\n    0.8250    0.0000    0.0000 C   0  0\n    1.6500    0.0000    0.0000 O   0  0\n  1  2  1  0\n  2  3  1  0\nM  END\n";

export function product(over: Partial<ProductInput> = {}): ProductInput {
  return {
    productIndex: 1,
    structure: { molfile: MOLFILE },
    label: "Pasted molfile",
    changeToken: "t1",
    ...over,
  };
}

export function experiment(over: Partial<ExperimentContext> = {}): ExperimentContext {
  return { ref: "4180", code: "ABC-008", url: null, ...over };
}

/** A manual timer queue (debounces in tests). */
export function manualTimers() {
  const queue: { fn: () => void; cancelled: boolean }[] = [];
  return {
    schedule: (fn: () => void) => {
      const t = { fn, cancelled: false };
      queue.push(t);
      return () => {
        t.cancelled = true;
      };
    },
    run(): void {
      for (const t of queue.splice(0)) if (!t.cancelled) t.fn();
    },
    pending: () => queue.filter((t) => !t.cancelled).length,
  };
}

/** A source whose state the test sets directly. */
export class StaticSource implements ProductSource {
  readonly name = "Test source";
  readonly autoLookup: boolean;
  readonly confirmBeforeCreate: boolean;
  private listeners = new Set<() => void>();
  constructor(
    private state: SourceState,
    options: { autoLookup?: boolean; confirmBeforeCreate?: boolean } = {},
  ) {
    this.autoLookup = options.autoLookup ?? false;
    this.confirmBeforeCreate = options.confirmBeforeCreate ?? true;
  }
  getState(): SourceState {
    return this.state;
  }
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  set(state: SourceState): void {
    this.state = state;
    for (const l of [...this.listeners]) l();
  }
}

export function properties(over: Partial<Properties> = {}): Properties {
  return {
    sum_formula: "C2H6O",
    mol_weight: 46.069,
    exact_mass: 46.04186,
    composition: [
      { element: "C", percent: 52.14 },
      { element: "H", percent: 13.13 },
      { element: "O", percent: 34.73 },
    ],
    ...over,
  };
}

export function nameInfo(text: string, updated_at = "2026-11-17T10:22:31+00:00"): NameInfo {
  return { text, updated_at };
}

export function compoundVariant(over: Partial<CompoundVariant> = {}): CompoundVariant {
  return {
    core_stereo_id: "000042_01",
    stereo_no: 1,
    stereo_mode: "abs",
    smiles: "C[C@H](N)O",
    inchi: "InChI=1S/C2H7NO/c1-2(3)4/h2,4H,3H2,1H3/t2-/m0/s1",
    inchikey: "KEY-ONE-N",
    name: null,
    batch_count: 1,
    matched: true,
    ...over,
  };
}

export function compoundItem(over: Partial<CompoundItem> = {}): CompoundItem {
  return {
    core_no: 42,
    core_id: "000042",
    formula: "C2H7NO",
    properties: properties({ sum_formula: "C2H7NO", mol_weight: 61.084, exact_mass: 61.05276 }),
    svg: "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>",
    variants: [compoundVariant()],
    batch_count: 1,
    experiment_count: 1,
    experiments: [{ experiment_ref: "4125", experiment_code: "ABC-001", experiment_url: null }],
    first_created_at: "2026-11-01T10:00:00+00:00",
    last_created_at: "2026-11-01T10:00:00+00:00",
    matched_by: [],
    ...over,
  };
}

export function compoundsResult(over: Partial<CompoundsResult> = {}): CompoundsResult {
  const items = over.items ?? [compoundItem()];
  return { total: items.length, registry_total: items.length, offset: 0, limit: 24, items, notes: [], warnings: [], ...over };
}

export function foundVariant(over: Partial<FoundVariant> = {}): FoundVariant {
  return {
    core_stereo_id: "000042_01",
    stereo_no: 1,
    stereo_mode: "abs",
    smiles: "C[C@H](N)O",
    inchikey: "KEY-ONE-N",
    batch_count: 1,
    name: null,
    inchi: "InChI=1S/C2H7NO/c1-2(3)4/h2,4H,3H2,1H3/t2-/m0/s1",
    properties: properties({ sum_formula: "C2H7NO", mol_weight: 61.084, exact_mass: 61.05276 }),
    svg: "<svg xmlns='http://www.w3.org/2000/svg'/>",
    batches: [batch()],
    ...over,
  };
}

export function coreFindResult(over: Partial<FindResult> = {}): FindResult {
  return {
    query: "000042",
    level: "core",
    core_no: 42,
    matched_id: null,
    variants: [foundVariant()],
    other_variants: [],
    experiments: [],
    warnings: [],
    ...over,
  };
}

export function setNameResult(over: Partial<SetNameResult> = {}): SetNameResult {
  return { core_stereo_id: "000042_01", name: nameInfo("ethanol"), changed: true, warnings: [], ...over };
}

export function generatedName(text: string, updated_at = "2026-11-18T09:00:00+00:00"): NameInfo {
  return { text, updated_at, source: "chemdraw" };
}

export function generateNameResult(over: Partial<GenerateNameResult> = {}): GenerateNameResult {
  return {
    core_stereo_id: "000042_01",
    name: generatedName("(2S)-2-aminopropan-1-ol"),
    generated: true,
    changed: true,
    message: null,
    warnings: [],
    ...over,
  };
}

/** A ping answer of a host that found ChemDraw on its PC. */
export function pingWithGenerator(label: string | null = "ChemDraw"): PingResult {
  return pingResult({ name_generator: { available: true, label } });
}

/** One spectrum recorded by the MestReNova add-on (compound_files). */
export function spectrum(over: Partial<SpectrumInfo> = {}): SpectrumInfo {
  return {
    full_id: "000042_01_001",
    spectrum_key: "ABC-001 1H",
    updated_at: "2026-11-17T14:05:00+00:00",
    experiment_code: "ABC-001",
    title: "ABC-001 FC2",
    comment: null,
    sample: null,
    nucleus: "1H",
    solvent: "CDCl3",
    experiment: null,
    pulse_sequence: null,
    frequency_mhz: 500.13,
    dimensions: 1,
    report: null,
    parameters: [],
    mnova_file: null,
    pdf_file: null,
    pdf_available: false,
    ...over,
  };
}
