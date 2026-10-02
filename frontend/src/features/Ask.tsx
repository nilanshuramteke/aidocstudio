import { useRef, useState } from "react";

export interface Citation { n: number; document_id: string; title: string; page_no: number; quote: string }
export interface Warning { sentence: string; reason: string; detail: string }
interface Source { n: number; document_id: string; title: string; page_no: number; preview: string }
interface Turn {
  role: "user" | "assistant"; content: string; citations?: Citation[]; warnings?: Warning[]; sources?: Source[];
  notFound?: boolean; error?: string; pending?: boolean;
}

/** Parse as many complete SSE events as the buffer holds; return the unparsed remainder. */
export function parseSSE(buffer: string): { events: { event: string; data: any }[]; rest: string } {
  const events: { event: string; data: any }[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const b of blocks) {
    let event = "message", data = "";
    for (const line of b.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (data) events.push({ event, data: JSON.parse(data) });
  }
  return { events, rest };
}

/** Split an answer on [S#] markers so each can render as a clickable chip (no HTML injection). */
export function splitCitations(text: string): ({ text: string } | { cite: number })[] {
  const out: ({ text: string } | { cite: number })[] = [];
  let last = 0;
  for (const m of text.matchAll(/\[S(\d+)\]/g)) {
    if (m.index! > last) out.push({ text: text.slice(last, m.index) });
    out.push({ cite: Number(m[1]) });
    last = m.index! + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}

export async function streamChat(body: object, onEvent: (event: string, data: any) => void, signal?: AbortSignal) {
  const res = await fetch("/api/v1/chat", { method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok || !res.body) {
    const p = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(p.detail ?? res.statusText);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const { events, rest } = parseSSE(buf);
    buf = rest;
    events.forEach((e) => onEvent(e.event, e.data));
  }
}

export function Ask({ documentIds, onOpenCitation, placeholder }: {
  documentIds?: string[]; onOpenCitation: (docId: string, page: number) => void; placeholder?: string;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const conv = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const patchLast = (f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, i) => (i === ts.length - 1 ? f(t) : t)));

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    setTurns((ts) => [...ts, { role: "user", content: message }, { role: "assistant", content: "", pending: true }]);
    abort.current = new AbortController();
    try {
      await streamChat(
        { message, conversation_id: conv.current, scope: documentIds ? { document_ids: documentIds } : { type: "all" } },
        (ev, d) => {
          if (ev === "meta") conv.current = d.conversation_id;
          else if (ev === "sources") patchLast((t) => ({ ...t, sources: d.sources }));
          else if (ev === "token") patchLast((t) => ({ ...t, content: t.content + d.t }));
          else if (ev === "error") patchLast((t) => ({ ...t, pending: false, error: d.detail }));
          else if (ev === "done") patchLast((t) => ({ ...t, pending: false, content: d.answer, citations: d.citations,
            warnings: d.warnings, notFound: d.not_found }));
        }, abort.current.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") patchLast((t) => ({ ...t, pending: false, error: (e as Error).message }));
    } finally { setBusy(false); }
  }

  return (
    <section aria-label="Ask AI">
      <div aria-live="polite">
        {turns.map((t, i) => (
          <div key={i} style={{ margin: "var(--s3) 0", padding: "var(--s3)", borderRadius: "var(--r-input)",
            background: t.role === "user" ? "var(--surface-2)" : "var(--surface)", border: "1px solid var(--border)" }}>
            {t.role === "user" ? t.content : (
              <>
                {t.notFound ? <em>Not found in your documents.</em> : splitCitations(t.content).map((p, j) => {
                  if ("text" in p) return <span key={j}>{p.text}</span>;
                  const c = t.citations?.find((x) => x.n === p.cite);
                  return c ? (
                    <button key={j} title={c.quote} onClick={() => onOpenCitation(c.document_id, c.page_no)}
                      style={{ margin: "0 2px", border: "1px solid var(--border)", borderRadius: 10, padding: "0 6px", fontSize: 12 }}>
                      {c.title} · p.{c.page_no}
                    </button>
                  ) : <span key={j}>[S{p.cite}]</span>;
                })}
                {t.pending && !t.content && <span style={{ color: "var(--text-muted)" }}>Thinking…</span>}
                {t.error && <p role="alert">{t.error}</p>}
                {t.warnings && t.warnings.length > 0 && (
                  <details style={{ marginTop: "var(--s2)", color: "var(--warn)" }}>
                    <summary>⚠ {t.warnings.length} statement{t.warnings.length > 1 ? "s" : ""} could not be verified</summary>
                    <ul>{t.warnings.map((w, k) => <li key={k}>{w.sentence ? `“${w.sentence}” — ` : ""}{w.detail}</li>)}</ul>
                  </details>
                )}
                {t.error && t.sources && t.sources.length > 0 && (
                  <ul style={{ fontSize: 13 }}>
                    {t.sources.slice(0, 5).map((s) => (
                      <li key={s.n}><button onClick={() => onOpenCitation(s.document_id, s.page_no)}>{s.title} · p.{s.page_no}</button> {s.preview}</li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>
        ))}
      </div>
      <form onSubmit={(e) => { e.preventDefault(); void send(); }} style={{ display: "flex", gap: "var(--s2)" }}>
        <input aria-label="Ask a question" value={input} onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder ?? "Ask across your documents…"} style={{ flex: 1, padding: "var(--s3)" }} />
        <button type="submit" disabled={busy}>Ask</button>
        {busy && <button type="button" onClick={() => abort.current?.abort()}>Stop</button>}
      </form>
    </section>
  );
}
