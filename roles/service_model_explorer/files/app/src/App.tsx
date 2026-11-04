import { useEffect, useMemo, useState } from "react";
import { snapshotSchema } from "./schemas";
import type { Snapshot } from "./schemas";
import {
  applyFilters,
  buildModels,
  distinct,
  duplicateEnumeratedIds,
  emptyFilters,
} from "./models";
import type { Filters, Model, Reachability } from "./models";
import { renderDesiredState, stageBlockedReason } from "./stage";

// ---- load state ------------------------------------------------------------------------
//
// THREE outcomes, never two. An unparseable read must never
// present as "nothing is there" — so "could not load" is its own state with its own message,
// and the table is never rendered empty as a stand-in for a failed fetch.
type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; snapshot: Snapshot }
  | { kind: "error"; detail: string };

const CONTEXT_PRESETS = [
  { label: "any", value: null },
  { label: "32K+", value: 32_000 },
  { label: "128K+", value: 128_000 },
  { label: "200K+", value: 200_000 },
  { label: "1M+", value: 1_000_000 },
];

const SORTS = {
  "context-desc": "context ↓",
  "context-asc": "context ↑",
  "created-desc": "newest first",
  "created-asc": "oldest first",
  "price-asc": "cheapest first",
  "price-desc": "dearest first",
  "id-asc": "id A→Z",
} as const;
type SortKey = keyof typeof SORTS;

function fmtContext(n: number | null): JSX.Element {
  if (n === null) return <span className="unknown">unknown</span>;
  if (n >= 1_000_000) return <>{(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M</>;
  if (n >= 1_000) return <>{Math.round(n / 1_000)}K</>;
  return <>{n}</>;
}

/**
 * Per-token USD rendered per MILLION tokens, which is how every provider quotes it and
 * therefore how the figure is actually compared. Free is shown as "free" rather than $0.00 so
 * it does not read as a rounding artefact; unknown stays visibly unknown.
 */
function fmtPrice(perToken: number | null): JSX.Element {
  if (perToken === null) return <span className="unknown">—</span>;
  if (perToken === 0) return <span style={{ color: "var(--picker)" }}>free</span>;
  const perM = perToken * 1_000_000;
  const digits = perM < 1 ? 3 : perM < 100 ? 2 : 0;
  return <>${perM.toFixed(digits)}</>;
}

/**
 * How stale is the snapshot, and should the page SAY so?
 *
 * The export play is run BY HAND (there is no cron and no timer — see the role README), so the
 * snapshot ages silently unless something draws attention to it. A bare ISO timestamp in the
 * corner is not that: it makes the reader do date arithmetic to discover that a model-discovery
 * page is showing a fortnight-old catalogue.
 *
 * Thresholds are deliberately generous because the underlying data moves slowly — OpenRouter
 * ships a handful of models a week, and the declared picker list changes rarely. The point is
 * to catch "nobody has run the export since the deploy", not to nag.
 */
function staleness(generatedAt: string): { days: number; level: "fresh" | "aging" | "stale" } {
  const then = Date.parse(generatedAt);
  // An unparseable timestamp is treated as STALE, not fresh. Failing towards "this might be old"
  // is the honest direction: the opposite would quietly vouch for data we cannot date.
  if (Number.isNaN(then)) return { days: -1, level: "stale" };
  const days = Math.floor((Date.now() - then) / 86_400_000);
  return { days, level: days >= 30 ? "stale" : days >= 7 ? "aging" : "fresh" };
}

function fmtDate(unixSeconds: number | null): JSX.Element {
  if (unixSeconds === null) return <span className="unknown">—</span>;
  const d = new Date(unixSeconds * 1000);
  if (Number.isNaN(d.getTime())) return <span className="unknown">—</span>;
  return <>{d.toISOString().slice(0, 10)}</>;
}

export default function App() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [sort, setSort] = useState<SortKey>("context-desc");
  const [staged, setStaged] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // `catalogue.json` sits beside index.html, written by the export play and shipped by the
    // role. A relative URL so the app works at any mount path.
    fetch("./catalogue.json", { cache: "no-store" })
      .then(async (r) => {
        if (!r.ok) {
          throw new Error(`the server answered HTTP ${r.status} ${r.statusText} for catalogue.json`);
        }
        // Read as TEXT then parse, so a non-JSON body (an nginx error page, say) reports as a
        // parse failure naming what arrived, rather than throwing an opaque SyntaxError.
        const body = await r.text();
        let raw: unknown;
        try {
          raw = JSON.parse(body);
        } catch {
          throw new Error(
            `catalogue.json is not valid JSON (${body.length} bytes, starts: ${JSON.stringify(
              body.slice(0, 80),
            )})`,
          );
        }
        const parsed = snapshotSchema.safeParse(raw);
        if (!parsed.success) {
          const issues = parsed.error.issues
            .slice(0, 6)
            .map((i) => `  ${i.path.join(".") || "(root)"}: ${i.message}`)
            .join("\n");
          throw new Error(`catalogue.json does not match the expected shape:\n${issues}`);
        }
        return parsed.data;
      })
      .then((snapshot) => {
        if (!cancelled) setState({ kind: "ready", snapshot });
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setState({ kind: "error", detail: e instanceof Error ? e.message : String(e) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const models = useMemo(
    () => (state.kind === "ready" ? buildModels(state.snapshot) : []),
    [state],
  );

  const filtered = useMemo(() => applyFilters(models, filters), [models, filters]);

  const sorted = useMemo(() => {
    const rows = [...filtered];
    // Nulls sort LAST in every ordering, because "unknown" is not "zero" and burying the
    // metadata-less rows at the bottom is what a reader scanning by context actually wants.
    const nullsLast = (a: number | null, b: number | null, dir: 1 | -1) => {
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      return (a - b) * dir;
    };
    switch (sort) {
      case "context-desc":
        rows.sort((a, b) => nullsLast(a.contextLength, b.contextLength, -1));
        break;
      case "context-asc":
        rows.sort((a, b) => nullsLast(a.contextLength, b.contextLength, 1));
        break;
      case "created-desc":
        rows.sort((a, b) => nullsLast(a.created, b.created, -1));
        break;
      case "created-asc":
        rows.sort((a, b) => nullsLast(a.created, b.created, 1));
        break;
      case "price-asc":
        rows.sort((a, b) => nullsLast(a.promptPrice, b.promptPrice, 1));
        break;
      case "price-desc":
        rows.sort((a, b) => nullsLast(a.promptPrice, b.promptPrice, -1));
        break;
      case "id-asc":
        rows.sort((a, b) => a.id.localeCompare(b.id));
        break;
    }
    return rows;
  }, [filtered, sort]);

  if (state.kind === "loading") {
    return (
      <div className="state loading">
        <h2>Loading the catalogue…</h2>
        <p>Reading <code>catalogue.json</code>.</p>
      </div>
    );
  }

  if (state.kind === "error") {
    // The honest failure state. It says the read failed and never implies the gateway has no
    // models — the reason this is not an empty table.
    return (
      <div className="state error">
        <h2>Could not load the catalogue</h2>
        <p>
          This is a problem reading the snapshot, <strong>not</strong> a statement that the
          gateway has no models. Nothing below should be read as "no models exist".
        </p>
        <pre>{state.detail}</pre>
        <p>
          Regenerate the snapshot (it reads the gateway's model list and OpenRouter's public
          catalogue) and redeploy it.
        </p>
      </div>
    );
  }

  const { snapshot } = state;
  const dupes = duplicateEnumeratedIds(snapshot);
  const stale = staleness(snapshot.generated_at);
  const vendors = distinct(models, (m) => [m.vendor]);
  const providers = distinct(models, (m) => [m.provider]);
  const inMods = distinct(models, (m) => m.inputModalities);
  const outMods = distinct(models, (m) => m.outputModalities);

  const countBy = (pick: (m: Model) => boolean) => models.filter(pick).length;

  const toggleSet = <T,>(set: Set<T>, v: T): Set<T> => {
    const next = new Set(set);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    return next;
  };

  const stagedIds = [...staged].sort();
  const desiredState = renderDesiredState(snapshot.declared_picker, stagedIds);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(desiredState);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard access can be denied (it needs a secure context, and this app is reached over
      // plain HTTP in some paths). Saying so beats a button that silently does nothing — the
      // block is on screen and selectable either way.
      setCopied(false);
      window.alert(
        "The browser denied clipboard access. Select the block below and copy it manually.",
      );
    }
  };

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>
            Model <span className="accent">Explorer</span>
          </h1>
          <p className="tagline">
            Every model the gateway can reach — and which of them Open WebUI can actually offer.
            The two are not the same, which is the point.
          </p>
        </div>

        <div className="tallies">
          <div className="tally is-enumerated">
            <span className="n">{countBy((m) => m.reachability === "enumerated")}</span>
            <span className="l">enumerated</span>
          </div>
          <div className="tally is-routable">
            <span className="n">{countBy((m) => m.reachability === "routable")}</span>
            <span className="l">routable only</span>
          </div>
          <div className="tally is-picker">
            <span className="n">{countBy((m) => m.inPicker)}</span>
            <span className="l">in picker</span>
          </div>
        </div>

        <div className="provenance">
          <div>
            gateway <b>{snapshot.gateway_host}</b>
          </div>
          <div>
            data as of <b>{snapshot.generated_at}</b>
          </div>
          {/* Staleness in WORDS, not just a timestamp. The export is run by hand, so the only
              thing standing between this page and a silently-ancient catalogue is saying so. */}
          <div
            className={`staleness is-${stale.level}`}
            title={
              stale.level === "fresh"
                ? "Regenerate the snapshot to refresh it"
                : "Regenerate the snapshot, then redeploy it"
            }
          >
            {stale.days < 0
              ? "snapshot date unreadable — treat as stale"
              : stale.days === 0
                ? "refreshed today"
                : `${stale.days} day${stale.days === 1 ? "" : "s"} old${
                    stale.level === "fresh" ? "" : " — consider re-running the export"
                  }`}
          </div>
          <div>
            {snapshot.counts.enumerated} entries / {snapshot.counts.openrouter_catalogue} catalogue
          </div>
          {/* The enumerated-vs-unique gap, explained rather than left as two numbers that disagree.
              /v1/models really does return some ids twice; de-duplicating is right, but doing
              it silently would leave the tally contradicting the export's own count. */}
          {dupes.length > 0 && (
            <div title={`Returned twice by /v1/models:\n${dupes.join("\n")}`}>
              <b style={{ color: "var(--enumerated)" }}>
                {dupes.length} id{dupes.length === 1 ? "" : "s"} returned twice
              </b>{" "}
              — deduped
            </div>
          )}
        </div>
      </header>

      <div className="body">
        <aside className="filters">
          <div className="fgroup">
            <h3>Search</h3>
            <input
              className="search-input"
              type="search"
              placeholder="id or name…"
              value={filters.search}
              onChange={(e) => setFilters({ ...filters, search: e.target.value })}
            />
          </div>

          <div className="fgroup">
            <h3>Reachability</h3>
            <div className="chips">
              {(["enumerated", "routable"] as Reachability[]).map((r) => (
                <button
                  key={r}
                  type="button"
                  className={`chip ${r === "routable" ? "tone-routable" : ""}`}
                  aria-pressed={filters.reachability.has(r)}
                  onClick={() =>
                    setFilters({ ...filters, reachability: toggleSet(filters.reachability, r) })
                  }
                >
                  {r === "enumerated" ? "enumerated" : "routable only"}
                  <span className="count">{countBy((m) => m.reachability === r)}</span>
                </button>
              ))}
            </div>
            <p className="hint">
              <b>enumerated</b> — in <code>GET /v1/models</code>, so Open WebUI can list it.
              <br />
              <b>routable only</b> — the <code>openrouter/*</code> wildcard routes it, but it does
              not enumerate, so it is unpickable until declared.
            </p>
          </div>

          <div className="fgroup">
            <h3>In Open WebUI's picker</h3>
            <div className="chips">
              {(["any", "yes", "no"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.picker === v}
                  onClick={() => setFilters({ ...filters, picker: v })}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>

          <div className="fgroup">
            <h3>Provider</h3>
            <div className="chips">
              {providers.map((p) => (
                <button
                  key={p}
                  type="button"
                  className="chip"
                  aria-pressed={filters.providers.has(p)}
                  onClick={() =>
                    setFilters({ ...filters, providers: toggleSet(filters.providers, p) })
                  }
                >
                  {p}
                  <span className="count">{countBy((m) => m.provider === p)}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="fgroup">
            <h3>Vendor / family</h3>
            <div className="chips">
              {vendors.map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.vendors.has(v)}
                  onClick={() => setFilters({ ...filters, vendors: toggleSet(filters.vendors, v) })}
                >
                  {v}
                  <span className="count">{countBy((m) => m.vendor === v)}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="fgroup">
            <h3>Input modality</h3>
            <div className="chips">
              {inMods.map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.inputModalities.has(v)}
                  onClick={() =>
                    setFilters({
                      ...filters,
                      inputModalities: toggleSet(filters.inputModalities, v),
                    })
                  }
                >
                  {v}
                  <span className="count">{countBy((m) => m.inputModalities.includes(v))}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="fgroup">
            <h3>Output modality</h3>
            <div className="chips">
              {outMods.map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.outputModalities.has(v)}
                  onClick={() =>
                    setFilters({
                      ...filters,
                      outputModalities: toggleSet(filters.outputModalities, v),
                    })
                  }
                >
                  {v}
                  <span className="count">{countBy((m) => m.outputModalities.includes(v))}</span>
                </button>
              ))}
            </div>
            <p className="hint">
              Image output is rare: {countBy((m) => m.outputModalities.includes("image"))} of{" "}
              {models.length} models. FLUX and Stable Diffusion are <b>not</b> brokered by
              OpenRouter at all, so they are absent by fact, not by filter.
            </p>
          </div>

          <div className="fgroup">
            <h3>Minimum context</h3>
            <div className="chips">
              {CONTEXT_PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  className="chip"
                  aria-pressed={filters.minContext === p.value}
                  onClick={() => setFilters({ ...filters, minContext: p.value })}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <p className="hint">
              Excludes models whose context is <b>unknown</b> — every anthropic/openai id is in
              that position, because <code>/v1/models</code> carries no metadata.
            </p>
          </div>

          <div className="fgroup">
            <h3>Pricing</h3>
            <div className="chips">
              {(["any", "free"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.pricing === v}
                  onClick={() => setFilters({ ...filters, pricing: v })}
                >
                  {v}
                  {v === "free" && (
                    <span className="count">
                      {countBy((m) => m.promptPrice === 0 && m.completionPrice === 0)}
                    </span>
                  )}
                </button>
              ))}
            </div>
            <label className="hint" htmlFor="maxprice">
              Max prompt $/M tokens
            </label>
            <input
              id="maxprice"
              className="num-input"
              type="number"
              min={0}
              step={0.5}
              placeholder="any"
              value={filters.maxPromptPrice === null ? "" : filters.maxPromptPrice * 1_000_000}
              onChange={(e) => {
                const v = e.target.value.trim();
                setFilters({
                  ...filters,
                  maxPromptPrice: v === "" ? null : Number(v) / 1_000_000,
                });
              }}
            />
          </div>

          <div className="fgroup">
            <h3>Lifecycle</h3>
            <div className="chips">
              {(["any", "yes", "no"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  className="chip"
                  aria-pressed={filters.expiring === v}
                  onClick={() => setFilters({ ...filters, expiring: v })}
                >
                  {v === "any" ? "any" : v === "yes" ? "expiring" : "no expiry"}
                  {v === "yes" && (
                    <span className="count">{countBy((m) => Boolean(m.expirationDate))}</span>
                  )}
                </button>
              ))}
            </div>
            <label className="hint" htmlFor="createdafter">
              Added on or after
            </label>
            <input
              id="createdafter"
              className="num-input"
              type="date"
              value={
                filters.createdAfter === null
                  ? ""
                  : new Date(filters.createdAfter * 1000).toISOString().slice(0, 10)
              }
              onChange={(e) => {
                const v = e.target.value;
                setFilters({
                  ...filters,
                  createdAfter: v === "" ? null : Math.floor(new Date(v).getTime() / 1000),
                });
              }}
            />
          </div>

          <button type="button" className="clear-all" onClick={() => setFilters(emptyFilters())}>
            clear all filters
          </button>
        </aside>

        <main className="results">
          <div className="results-bar">
            <span>
              <span className="shown">{sorted.length}</span> of {models.length} models
            </span>
            {staged.size > 0 && (
              <span style={{ color: "var(--picker)" }}>{staged.size} staged</span>
            )}
            <span className="sort">
              <label htmlFor="sort">sort</label>
              <select
                id="sort"
                value={sort}
                onChange={(e) => setSort(e.target.value as SortKey)}
              >
                {Object.entries(SORTS).map(([k, label]) => (
                  <option key={k} value={k}>
                    {label}
                  </option>
                ))}
              </select>
            </span>
          </div>

          <div className="table-scroll">
            {sorted.length === 0 ? (
              // An empty RESULT SET, which is a different claim from a failed load and says so.
              // The filters are named as the cause, so this can never be misread as "the
              // gateway has no models".
              <div className="empty-rows">
                <p>
                  <b>No model matches these filters.</b>
                </p>
                <p>
                  {models.length} models did load — this is a filter result, not a loading
                  failure.
                </p>
              </div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th className="stage-cell" title="Stage for the Open WebUI picker">
                      +
                    </th>
                    <th>Model</th>
                    <th>Reach</th>
                    <th className="num">Context</th>
                    <th className="num">In $/M</th>
                    <th className="num">Out $/M</th>
                    <th>Modalities</th>
                    <th className="num">Added</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((m) => {
                    const blocked = stageBlockedReason(m);
                    return (
                      <tr key={m.id}>
                        <td className="stage-cell">
                          <input
                            type="checkbox"
                            checked={staged.has(m.id) || m.inPicker}
                            disabled={blocked !== null}
                            title={blocked ?? `Stage ${m.id} for the picker`}
                            aria-label={`Stage ${m.id}`}
                            onChange={() => setStaged(toggleSet(staged, m.id))}
                          />
                        </td>
                        <td>
                          <span className="model-id">{m.id}</span>
                          {m.name !== m.id && <span className="model-name">{m.name}</span>}
                        </td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          <span className={`badge ${m.reachability}`}>
                            {m.reachability === "enumerated" ? "enum" : "route"}
                          </span>{" "}
                          {m.inPicker && <span className="badge picker">picker</span>}{" "}
                          {m.expirationDate && (
                            <span className="badge expiring" title={`Expires ${m.expirationDate}`}>
                              exp
                            </span>
                          )}
                        </td>
                        <td className="num">{fmtContext(m.contextLength)}</td>
                        <td className="num">{fmtPrice(m.promptPrice)}</td>
                        <td className="num">{fmtPrice(m.completionPrice)}</td>
                        <td className="mods">
                          {m.inputModalities.length === 0 && m.outputModalities.length === 0 ? (
                            <span className="unknown">unknown</span>
                          ) : (
                            <>
                              {m.inputModalities.join("+") || "?"}
                              <span className="io"> → </span>
                              {m.outputModalities.join("+") || "?"}
                            </>
                          )}
                        </td>
                        <td className="num">{fmtDate(m.created)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>

          {staged.size > 0 && (
            <div className="tray">
              <h2>Staged for the picker — {staged.size} model(s)</h2>
              <p>
                This app changes nothing by itself. Adding a model to Open WebUI's picker is a
                configuration change, so it goes through a play: paste the block below over{" "}
                <code>litellm_openrouter_models</code> in the inventory that configures the
                gateway, commit it, then converge. The repo stays the source of truth and the
                change is reviewable as a diff.
              </p>
              <div className="tray-actions">
                <button type="button" className="btn" onClick={copy}>
                  {copied ? "copied ✓" : "copy desired state"}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setStaged(new Set())}
                >
                  clear staged
                </button>
              </div>
              <pre>{desiredState}</pre>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
