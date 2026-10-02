import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DocDetail } from "../lib";
import { confidenceBadge } from "./Fields";
import { isPending, keyToAction, nextFlagged, overlayKind, progressText, rectFromDrag, type ReviewField } from "./reviewLogic";

interface QueueSummary { documents: number; fields_left: number; items: { document_id: string; title: string; blocking: number; fields_left: number }[] }
interface FieldsResponse { doc_type: string | null; fields: (ReviewField & { validation: { message: string }[] })[] }

const post = (path: string, body?: unknown, method = "POST") =>
  api<any>(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

export function Review({ onOpenDoc }: { onOpenDoc: (id: string) => void }) {
  const [queue, setQueue] = useState<QueueSummary | null>(null);
  const [docId, setDocId] = useState<string | null>(null);
  const load = useCallback(() => { api<QueueSummary>("/review/queue").then(setQueue).catch(() => setQueue(null)); }, []);
  useEffect(load, [load, docId]);

  if (docId) return <ReviewScreen docId={docId} queueDocs={queue?.documents ?? 0} onExit={() => { setDocId(null); load(); }}
    onNext={async (after) => { const n = await api<{ document_id: string | null }>(`/review/next?after=${after}`);
      if (n.document_id && n.document_id !== after) setDocId(n.document_id); else setDocId(null); }} />;
  return (
    <div>
      <p>{queue ? progressText(queue.fields_left, queue.documents) : "Loading…"}</p>
      <button disabled={!queue?.documents} onClick={async () => { const n = await api<{ document_id: string | null }>("/review/next"); setDocId(n.document_id); }}>
        Start review
      </button>
      <ul>{queue?.items.map((i) => (
        <li key={i.document_id}><button onClick={() => onOpenDoc(i.document_id)}>{i.title}</button> — {i.fields_left} to check{i.blocking ? `, ${i.blocking} must be fixed` : ""}</li>
      ))}</ul>
    </div>
  );
}

function ReviewScreen({ docId, queueDocs, onExit, onNext }: { docId: string; queueDocs: number; onExit: () => void; onNext: (after: string) => void }) {
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [fields, setFields] = useState<FieldsResponse["fields"]>([]);
  const [idx, setIdx] = useState(0);
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const [regionMode, setRegionMode] = useState(false);
  const [drag, setDrag] = useState<{ a: { x: number; y: number }; b: { x: number; y: number } } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const img = useRef<HTMLImageElement>(null);

  const refresh = useCallback(async (keepKey?: string) => {
    const [d, f] = await Promise.all([api<DocDetail>(`/documents/${docId}`), api<FieldsResponse>(`/documents/${docId}/fields`)]);
    setDoc(d); setFields(f.fields);
    setIdx((cur) => { const k = keepKey ?? f.fields[cur]?.key; const i = f.fields.findIndex((x) => x.key === k); return i >= 0 ? i : 0; });
  }, [docId]);
  const loaded = useRef<string | null>(null);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { // on first load of a document, jump to its first field that needs review
    if (fields.length && loaded.current !== docId) { loaded.current = docId; const i = fields.findIndex(isPending); if (i >= 0) setIdx(i); }
  }, [docId, fields]);

  const cur = fields[idx];
  useEffect(() => { if (cur?.page_no) setPage(cur.page_no); }, [cur?.key, cur?.page_no]);

  // live validation while typing
  useEffect(() => {
    if (!editing || !cur) return;
    const t = setTimeout(() => { post(`/documents/${docId}/fields/${cur.key}/validate`, { value: draft })
      .then((r) => setHint(r.ok ? `OK → ${r.value}` : r.messages.join("; "))).catch(() => setHint(null)); }, 200);
    return () => clearTimeout(t);
  }, [draft, editing, cur?.key, docId]);

  const act = useCallback(async (fn: () => Promise<unknown>, success?: string) => {
    try { await fn(); setMsg(success ?? null); await refresh(cur?.key); } catch (e) { setMsg((e as Error).message); }
  }, [refresh, cur?.key]);

  const moveNext = useCallback((dir: 1 | -1 = 1) => { const i = nextFlagged(fields, idx, dir); if (i >= 0) setIdx(i); else setMsg("Nothing left to review in this document — press N to finish."); }, [fields, idx]);

  const submitEdit = async () => {
    if (!cur) return;
    try { await post(`/documents/${docId}/fields/${cur.key}`, { value: draft }, "PATCH"); setEditing(false); setHint(null); setMsg(`${cur.key} corrected`); await refresh(cur.key); moveNext(); }
    catch (e) { setMsg((e as Error).message); }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const a = keyToAction(e.key, { ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey, editing });
      if (a === "none" || !cur && a !== "undo" && a !== "complete") return;
      e.preventDefault();
      if (a === "accept") void act(() => post(`/documents/${docId}/fields/${cur.key}`, { status: "accepted" }, "PATCH"), `${cur.key} accepted`).then(() => moveNext());
      else if (a === "reject") void act(() => post(`/documents/${docId}/fields/${cur.key}`, { status: "rejected" }, "PATCH"), `${cur.key} rejected`).then(() => moveNext());
      else if (a === "edit") { setDraft(cur.value ?? cur.raw_value ?? ""); setEditing(true); }
      else if (a === "cancel") { setEditing(false); setRegionMode(false); setHint(null); }
      else if (a === "next") moveNext(1);
      else if (a === "prev") moveNext(-1);
      else if (a === "region") { setRegionMode((m) => !m); setMsg("Drag a box around the value on the page"); }
      else if (a === "undo") void act(() => post(`/documents/${docId}/fields/undo`), "Undone");
      else if (a === "complete") void post(`/review/${docId}/complete`).then(() => onNext(docId)).catch((e: Error) => setMsg(e.message));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [act, cur, docId, editing, moveNext, onNext]);

  const left = fields.filter(isPending).length;
  const pt = (e: React.PointerEvent) => { const r = img.current!.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const finishDrag = async () => {
    if (!drag || !img.current || !cur) return;
    const r = img.current.getBoundingClientRect();
    const bbox = rectFromDrag(drag.a, drag.b, r.width, r.height);
    setDrag(null); setRegionMode(false);
    if (bbox) await act(() => post(`/documents/${docId}/fields/${cur.key}/rerun`, { page_no: page, bbox }), `${cur.key} re-read from the selected region`);
  };

  if (!doc) return <p>Loading…</p>;
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <button onClick={onExit}>← Review queue</button>
        <strong role="status">{progressText(left, queueDocs)}</strong>
      </div>
      <h1 style={{ fontSize: 18 }}>{doc.title}</h1>
      <p style={{ color: "var(--text-muted)", fontSize: 12 }}>Enter accept · E edit · Backspace reject · Tab next · R re-read region · Ctrl+Z undo · N finish</p>
      {msg && <p role="status">{msg}</p>}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 340px", gap: "var(--s4)" }}>
        <div style={{ position: "relative", alignSelf: "start", cursor: regionMode ? "crosshair" : undefined, touchAction: "none" }}
          onPointerDown={(e) => regionMode && setDrag({ a: pt(e), b: pt(e) })}
          onPointerMove={(e) => drag && setDrag({ ...drag, b: pt(e) })} onPointerUp={() => void finishDrag()}>
          <img ref={img} alt={`Page ${page}`} draggable={false} src={`/api/v1/documents/${docId}/pages/${page}/image?w=900`} style={{ width: "100%", display: "block" }} />
          {fields.filter((f) => f.bbox && f.page_no === page).map((f) => {
            const k = overlayKind(f), active = f.key === cur?.key;
            return <div key={f.key} aria-hidden style={{ position: "absolute", left: `${f.bbox![0] * 100}%`, top: `${f.bbox![1] * 100}%`,
              width: `${f.bbox![2] * 100}%`, height: `${f.bbox![3] * 100}%`, boxSizing: "border-box", pointerEvents: "none",
              border: `${active ? 3 : 2}px ${k === "ok" ? "solid" : "dashed"} ${k === "ok" ? "var(--ok)" : k === "review" ? "var(--warn)" : "var(--danger)"}` }} />;
          })}
          {drag && img.current && <div aria-hidden style={{ position: "absolute", left: Math.min(drag.a.x, drag.b.x), top: Math.min(drag.a.y, drag.b.y),
            width: Math.abs(drag.a.x - drag.b.x), height: Math.abs(drag.a.y - drag.b.y), border: "2px solid var(--accent)", background: "rgba(59,91,219,.15)" }} />}
        </div>
        <ul aria-label="Fields" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {fields.map((f, i) => {
            const b = confidenceBadge(f.confidence);
            return (
              <li key={f.key} aria-current={i === idx ? "true" : undefined} onClick={() => setIdx(i)}
                style={{ padding: "var(--s2)", borderLeft: `3px solid ${i === idx ? "var(--accent)" : "transparent"}`, borderBottom: "1px solid var(--border)", cursor: "pointer" }}>
                <div style={{ color: "var(--text-muted)", fontSize: 12 }}>{f.key} · {f.status}</div>
                {editing && i === idx ? (
                  <>
                    <input autoFocus aria-label={`Edit ${f.key}`} value={draft} onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void submitEdit(); } }} style={{ width: "100%" }} />
                    {hint && <div role="status" style={{ fontSize: 12 }}>{hint}</div>}
                  </>
                ) : <div>{f.value ?? f.raw_value ?? <em>— not found —</em>} <span style={{ color: b.color }}>{b.icon} {b.label}</span></div>}
                {f.flag_reason && <div style={{ fontSize: 12, color: "var(--warn)" }}>{f.flag_reason.replace("_", " ")}</div>}
                {f.validation.map((v, k) => <div key={k} style={{ fontSize: 12, color: "var(--danger)" }}>{v.message}</div>)}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
