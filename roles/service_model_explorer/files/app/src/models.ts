// Merging the two sources into ONE list of models, each labelled with how it is reachable.
//
// This file is the app's actual subject. The gateway and OpenRouter disagree about what exists,
// and the disagreement is the thing worth showing:
//
//   ENUMERATED   GET /v1/models returns it, so Open WebUI CAN offer it in its picker: every
//                id the provider wildcards expand (anthropic, openai, elevenlabs, ...) plus
//                the openrouter ids the gateway declares EXPLICITLY.
//
//   ROUTABLE     the `openrouter/*` wildcard routes it, but it does NOT enumerate, so it is
//                unpickable however well it works: every OpenRouter catalogue model that is
//                not declared falls in this bucket.
//
// The cause: OpenRouter has no provider model-info class on LiteLLM v1.104.2
// (get_provider_model_info() -> None, no cost-map entries), so the wildcard contributes nothing
// to /v1/models, while `anthropic/*` and `openai/*` DO expand.

import type { OpenRouterModel, Snapshot } from "./schemas";

// The prefix LiteLLM routes OpenRouter models under. The declared ids carry it
// (`openrouter/vendor/model`) while OpenRouter's catalogue is keyed on the bare upstream id
// (`vendor/model`). Comparing the two forms directly reports every model as simultaneously
// missing from both sides — a report that is 100% wrong in both directions while looking
// populated. The drift play documents the same trap.
export const OPENROUTER_PREFIX = "openrouter/";

// A DOUBLED prefix is CORRECT for six models, and it looks exactly like a bug.
//
// OpenRouter's catalogue contains six models whose literal VENDOR segment is `openrouter`:
// openrouter/auto, auto-beta, fusion, free, pareto-code, bodybuilder (its own router models).
// Prefixing those for LiteLLM yields `openrouter/openrouter/auto`, which reads as a
// double-prefix mistake and is in fact the right id — provider prefix plus upstream id, exactly
// as `openrouter/x-ai/grok-4.7` is.
//
// Stripping the "duplicate" to tidy it would produce `openrouter/auto`, which LiteLLM would
// route to a model named `auto` under provider `openrouter` — i.e. a 400 for a model that works.
// Left alone deliberately, and recorded here so the next reader does not "fix" it.

export type Reachability = "enumerated" | "routable";

export interface Model {
  /** The id a client passes as `model`, including any routing prefix. */
  id: string;
  /** Human label where the catalogue gives one, else the id. */
  name: string;
  /** The vendor segment — `anthropic`, `openai`, `google`, `x-ai`, … */
  vendor: string;
  /** Which provider LiteLLM routes this through. */
  provider: string;
  /** Can Open WebUI put it in the picker at all? */
  reachability: Reachability;
  /** Is it declared on the gateway, i.e. deliberately surfaced in the picker today? */
  inPicker: boolean;
  contextLength: number | null;
  /** Per-token USD, or null when the catalogue gave no parseable figure. */
  promptPrice: number | null;
  completionPrice: number | null;
  inputModalities: string[];
  outputModalities: string[];
  supportedParameters: string[];
  /** Unix seconds, or null. */
  created: number | null;
  expirationDate: string | null;
  /** True when OpenRouter's catalogue backs this entry with filterable metadata. */
  hasCatalogueData: boolean;
}

/**
 * Parse a pricing string into a number, or null.
 *
 * Returns null rather than 0 for anything unparseable, deliberately: a FREE model and a model
 * whose price we failed to read are different facts, and collapsing them would make the price
 * filter quietly lie. The UI renders null as "unknown".
 *
 * A NEGATIVE price is a SENTINEL, not a price. OpenRouter publishes "-1" for its router models,
 * which pick a downstream model per request and so have no fixed rate (openrouter/auto, for
 * example). Taken literally it renders as "$-1000000.000 per million tokens" — visibly absurd,
 * but the same value would also sort those models ABOVE every genuinely free model under
 * "cheapest first" and pass any "max price" filter. Mapped to null (unknown) instead, which is
 * what it means.
 */
export function parsePrice(raw: string | undefined): number | null {
  if (raw === undefined || raw === null || raw === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  if (n < 0) return null;
  return n;
}

/** Split `vendor/model` (or `openrouter/vendor/model`) into its vendor segment. */
export function vendorOf(bareId: string): string {
  const i = bareId.indexOf("/");
  return i === -1 ? bareId : bareId.slice(0, i);
}

/**
 * Which LiteLLM provider serves an ENUMERATED id.
 *
 * The first segment of an id from /v1/models IS the provider, because every entry comes from a
 * provider wildcard (`anthropic/*`, `openai/*`, `elevenlabs/*`) or from an explicitly declared
 * `openrouter/...` entry.
 */
function providerOfEnumerated(id: string): string {
  return vendorOf(id);
}

/**
 * Build the merged, de-duplicated model list.
 *
 * De-duplication is by the id a CLIENT would pass, which is why the OpenRouter catalogue ids are
 * compared in their prefixed form: `openrouter/x-ai/grok-4.7` from /v1/models and `x-ai/grok-4.7`
 * from the catalogue are ONE model, and listing both would double-count every declared one and
 * show two rows that do the same thing.
 */
/**
 * Ids the gateway returned MORE THAN ONCE from /v1/models.
 *
 * /v1/models can list the same id twice (a model reachable through two wildcard expansions).
 * De-duplicating is obviously right (two rows for one model is noise), but doing it SILENTLY
 * would leave the app showing fewer models than the export recorded, i.e. two figures that
 * disagree with no explanation — exactly the kind of unexplained gap this app exists to
 * eliminate. So the duplicates are surfaced and the UI accounts for the difference.
 */
export function duplicateEnumeratedIds(snapshot: Snapshot): string[] {
  const seen = new Set<string>();
  const dupes = new Set<string>();
  for (const id of snapshot.enumerated) {
    if (seen.has(id)) dupes.add(id);
    seen.add(id);
  }
  return [...dupes].sort();
}

export function buildModels(snapshot: Snapshot): Model[] {
  const enumeratedSet = new Set(snapshot.enumerated);
  const pickerSet = new Set(snapshot.declared_picker);

  // Catalogue entries keyed by the PREFIXED id, so the enumerated side can look them up and
  // inherit the rich metadata (context, pricing, modalities) that /v1/models does not carry.
  const catalogueByPrefixedId = new Map<string, OpenRouterModel>();
  for (const m of snapshot.openrouter) {
    catalogueByPrefixedId.set(OPENROUTER_PREFIX + m.id, m);
  }

  const models: Model[] = [];
  const seen = new Set<string>();

  const fromCatalogue = (id: string, cat: OpenRouterModel | undefined, reach: Reachability): Model => {
    const bare = id.startsWith(OPENROUTER_PREFIX) ? id.slice(OPENROUTER_PREFIX.length) : id;
    return {
      id,
      name: cat?.name ?? id,
      vendor: vendorOf(bare),
      provider: id.startsWith(OPENROUTER_PREFIX) ? "openrouter" : providerOfEnumerated(id),
      reachability: reach,
      inPicker: pickerSet.has(id),
      contextLength: cat?.context_length ?? null,
      promptPrice: parsePrice(cat?.pricing?.prompt),
      completionPrice: parsePrice(cat?.pricing?.completion),
      inputModalities: cat?.architecture?.input_modalities ?? [],
      outputModalities: cat?.architecture?.output_modalities ?? [],
      supportedParameters: cat?.supported_parameters ?? [],
      created: cat?.created ?? null,
      expirationDate: cat?.expiration_date ?? null,
      hasCatalogueData: cat !== undefined,
    };
  };

  // 1. Everything the gateway enumerates. These come first because they are the models that can
  //    actually be picked, and an enumerated model that ALSO has catalogue data gets the richer
  //    metadata merged in.
  for (const id of snapshot.enumerated) {
    if (seen.has(id)) continue;
    seen.add(id);
    models.push(fromCatalogue(id, catalogueByPrefixedId.get(id), "enumerated"));
  }

  // 2. The rest of OpenRouter's catalogue: routable via the wildcard, invisible to the picker.
  for (const m of snapshot.openrouter) {
    const id = OPENROUTER_PREFIX + m.id;
    if (seen.has(id)) continue;
    seen.add(id);
    // `enumeratedSet` is consulted rather than assumed: if a future build DOES start enumerating
    // OpenRouter models, this labels them correctly with no code change.
    models.push(fromCatalogue(id, m, enumeratedSet.has(id) ? "enumerated" : "routable"));
  }

  return models;
}

// ---- filtering -------------------------------------------------------------------------

export interface Filters {
  search: string;
  vendors: Set<string>;
  providers: Set<string>;
  inputModalities: Set<string>;
  outputModalities: Set<string>;
  reachability: Set<Reachability>;
  /** "any" | "yes" | "no" — whether the model is in Open WebUI's picker today. */
  picker: "any" | "yes" | "no";
  minContext: number | null;
  /** Max per-token prompt price in USD. Models with an unknown price are kept — see below. */
  maxPromptPrice: number | null;
  /** "any" | "free" — free means a parsed prompt AND completion price of exactly 0. */
  pricing: "any" | "free";
  /** "any" | "yes" | "no" — has a published expiration_date. */
  expiring: "any" | "yes" | "no";
  /** Only models created on or after this unix-seconds instant. */
  createdAfter: number | null;
}

export const emptyFilters = (): Filters => ({
  search: "",
  vendors: new Set(),
  providers: new Set(),
  inputModalities: new Set(),
  outputModalities: new Set(),
  reachability: new Set(),
  picker: "any",
  minContext: null,
  maxPromptPrice: null,
  pricing: "any",
  expiring: "any",
  createdAfter: null,
});

export function applyFilters(models: Model[], f: Filters): Model[] {
  const needle = f.search.trim().toLowerCase();
  return models.filter((m) => {
    if (needle && !m.id.toLowerCase().includes(needle) && !m.name.toLowerCase().includes(needle)) {
      return false;
    }
    if (f.vendors.size > 0 && !f.vendors.has(m.vendor)) return false;
    if (f.providers.size > 0 && !f.providers.has(m.provider)) return false;
    if (f.reachability.size > 0 && !f.reachability.has(m.reachability)) return false;

    if (f.picker === "yes" && !m.inPicker) return false;
    if (f.picker === "no" && m.inPicker) return false;

    // Modality filters require the model to declare EVERY selected modality, so picking
    // "image" under outputs means "can emit images" rather than "mentions image anywhere".
    for (const want of f.inputModalities) {
      if (!m.inputModalities.includes(want)) return false;
    }
    for (const want of f.outputModalities) {
      if (!m.outputModalities.includes(want)) return false;
    }

    // A null context/price/date is UNKNOWN, not zero, so a model with no catalogue data is
    // excluded by a numeric filter rather than silently passing it as 0. Every enumerated
    // anthropic/openai id is in that position — /v1/models carries no metadata at all — so
    // this is the common case, not an edge one, and the UI says so next to the filter.
    if (f.minContext !== null) {
      if (m.contextLength === null || m.contextLength < f.minContext) return false;
    }
    if (f.maxPromptPrice !== null) {
      if (m.promptPrice === null || m.promptPrice > f.maxPromptPrice) return false;
    }
    if (f.pricing === "free") {
      if (m.promptPrice !== 0 || m.completionPrice !== 0) return false;
    }
    if (f.expiring === "yes" && !m.expirationDate) return false;
    if (f.expiring === "no" && m.expirationDate) return false;
    if (f.createdAfter !== null) {
      if (m.created === null || m.created < f.createdAfter) return false;
    }
    return true;
  });
}

/** Distinct values of a field, for building the filter controls from the data itself. */
export function distinct(models: Model[], pick: (m: Model) => string[]): string[] {
  const s = new Set<string>();
  for (const m of models) for (const v of pick(m)) if (v) s.add(v);
  return [...s].sort();
}
