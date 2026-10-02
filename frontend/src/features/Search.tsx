import { useEffect, useState } from "react";
import { api } from "../lib";

const MARK_START = "\x02";
const MARK_END = "\x03";

export interface SearchResult {
  document: { id: string; title: string; doc_type: string | null; state: string; page_count: number | null };
  snippets: { page_no: number; text: string }[];
  field_hits: { key: string; value: string }[];
  summary: Record<string, string>;
}
interface SearchResponse {
  text: string; parsed_filters: Record<string, unknown>; filters: Record<string, unknown>; total: number;
  semantic: { available: boolean; reason: string | null }; results: SearchResult[];
}

/** Split a snippet on the highlight sentinels. Rendering plain text nodes avoids any HTML injection. */
export function splitHighlights(s: string): { text: string; hit: boolean }[] {
  const out: { text: string; hit: boolean }[] = [];
  let hit = false;
  for (const part of s.split(new RegExp(`([${MARK_START}${MARK_END}])`))) {
    if (part === MARK_START) hit = true;
    else if (part === MARK_END) hit = false;
    else if (part) out.push({ text: part, hit });
  }
  return out;
}

export function chipLabel(key: string, value: unknown): string {
  const labels: Record<string, string> = {
    type: "Type", date_from: "From", date_to: "Until", amount_min: "Amount ≥", amount_max: "Amount ≤",
    entity: "Party", review: "Review", state: "State",
  };
  return `${labels[key] ?? key}: ${String(value)}`;
}

export function Search({ onOpen, initialQuery }: { onOpen: (id: string) => void; initialQuery?: string }) {
  const [q, setQ] = useState(initialQuery ?? "");
  const [mode, setMode] = useState<"best" | "keyword" | "meaning">("best");
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  async function run(query: string, filters?: Record<string, unknown>, parse = true) {
    setError(null);
    try {
      setRes(await api<SearchResponse>("/search", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: query, mode, filters, parse }) }));
    } catch (e) { setError((e as Error).message); }
  }

  useEffect(() => { if (initialQuery) void run(initialQuery); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  function removeChip(key: string) {
    if (!res) return;
    const rest = { ...res.filters };
    delete rest[key];
    setQ(res.text);
    void run(res.text, rest, false); // chips are explicit now: stop re-parsing the text
  }

  async function save() {
    const name = window.prompt("Name this search");
    if (!name || !res) return;
    await api("/search/saved", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, query: { q: res.text, filters: res.filters, mode } }) });
    setSaved(name);
  }

  return (
    <div>
      <form onSubmit={(e) => { e.preventDefault(); void run(q); }} style={{ display: "flex", gap: "var(--s2)" }}>
        <input aria-label="Search" placeholder='Search documents… try "invoices in 2026 above 50,000"' value={q}
          onChange={(e) => setQ(e.target.value)} style={{ flex: 1, padding: "var(--s3)" }} />
        <select aria-label="Search mode" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
          <option value="best">Best match</option><option value="keyword">Keyword</option><option value="meaning">Meaning</option>
        </select>
        <button type="submit">Search</button>
      </form>
      {error && <p role="alert">{error}</p>}
      {res && (
        <>
          <div style={{ margin: "var(--s3) 0", display: "flex", gap: "var(--s2)", flexWrap: "wrap", alignItems: "center" }}>
            {Object.entries(res.filters).map(([k, v]) => (
              <button key={k} onClick={() => removeChip(k)} aria-label={`Remove filter ${chipLabel(k, v)}`}
                style={{ border: "1px solid var(--border)", borderRadius: 12, padding: "2px 10px", background: "var(--surface-2)" }}>
                {chipLabel(k, v)} ✕
              </button>
            ))}
            <span style={{ color: "var(--text-muted)" }}>{res.total} result{res.total === 1 ? "" : "s"}</span>
            <button onClick={() => void save()}>Save search</button>
            {saved && <span role="status">Saved “{saved}”</span>}
          </div>
          {mode !== "keyword" && !res.semantic.available && res.semantic.reason && (
            <p style={{ color: "var(--text-muted)" }}>Keyword results only: {res.semantic.reason}.</p>
          )}
          {res.results.length === 0 && <p>No documents matched.</p>}
          <ul style={{ listStyle: "none", padding: 0 }}>
            {res.results.map((r) => (
              <li key={r.document.id} style={{ borderTop: "1px solid var(--border)", padding: "var(--s3) 0" }}>
                <button onClick={() => onOpen(r.document.id)} style={{ all: "unset", cursor: "pointer", color: "var(--accent)", fontWeight: 600 }}>
                  {r.document.title}
                </button>
                <span style={{ color: "var(--text-muted)" }}> · {r.document.doc_type ?? "Unclassified"}
                  {r.summary.total ? ` · ${r.summary.total}` : ""}{r.summary.vendor ? ` · ${r.summary.vendor}` : ""}</span>
                {r.field_hits.map((h, i) => <div key={i} style={{ fontSize: 13 }}>{h.key}: {h.value}</div>)}
                {r.snippets.map((s, i) => (
                  <p key={i} style={{ margin: "var(--s1) 0", fontSize: 13, color: "var(--text-muted)" }}>
                    p.{s.page_no} · {splitHighlights(s.text).map((p, j) => p.hit ? <mark key={j}>{p.text}</mark> : <span key={j}>{p.text}</span>)}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
