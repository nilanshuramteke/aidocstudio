import { useCallback, useEffect, useState } from "react";
import { api, formatBytes, subscribeEvents, type DocRow } from "../lib";
import { DropZone } from "./DropZone";

interface Tag { id: string; name: string; documents: number }
interface Collection { id: string; name: string; kind: string }

const send = (path: string, body: unknown) =>
  api<any>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function Documents({ onOpen, limit = 50, showDrop = true }: { onOpen: (id: string) => void; limit?: number; showDrop?: boolean }) {
  const [rows, setRows] = useState<DocRow[]>([]);
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [tags, setTags] = useState<Tag[]>([]);
  const [cols, setCols] = useState<Collection[]>([]);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const qs = `limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}${tag ? `&tag=${encodeURIComponent(tag)}` : ""}`;
    api<{ items: DocRow[] }>(`/documents?${qs}`).then((r) => setRows(r.items)).catch((e: Error) => setError(e.message));
  }, [q, tag, limit]);

  useEffect(load, [load]);
  useEffect(() => { if (showDrop) { api<{ items: Tag[] }>("/tags").then((r) => setTags(r.items)); api<{ items: Collection[] }>("/collections").then((r) => setCols(r.items)); } }, [showDrop, msg]);
  useEffect(() => subscribeEvents((ev) => ev === "document.state" && load()), [load]);

  const ids = [...sel];
  const bulk = async (action: string, params?: object, done?: string) => {
    try { await send("/documents/bulk", { ids, action, params }); setMsg(done ?? "Done"); setSel(new Set()); load(); }
    catch (e) { setMsg((e as Error).message); }
  };
  const toggle = (id: string) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div>
      {showDrop && <DropZone onDone={load} />}
      <div style={{ display: "flex", gap: "var(--s3)", margin: "var(--s4) 0" }}>
        <input placeholder="Filter by name…" aria-label="Filter documents" value={q} onChange={(e) => setQ(e.target.value)} style={{ padding: "var(--s2)", width: 280 }} />
        {showDrop && <select aria-label="Filter by tag" value={tag} onChange={(e) => setTag(e.target.value)}>
          <option value="">All tags</option>{tags.map((t) => <option key={t.id} value={t.name}>{t.name} ({t.documents})</option>)}</select>}
      </div>
      {sel.size > 0 && (
        <div role="toolbar" aria-label="Bulk actions" style={{ display: "flex", gap: "var(--s2)", padding: "var(--s2)", background: "var(--surface-2)", marginBottom: "var(--s3)" }}>
          <strong>{sel.size} selected</strong>
          <button onClick={() => { const t = window.prompt("Tag name"); if (t) void bulk("tag", { tags: [t] }, `Tagged “${t}”`); }}>Tag</button>
          <select aria-label="Add to collection" value="" onChange={(e) => e.target.value && void bulk("add_to_collection", { collection_id: e.target.value }, "Added to collection")}>
            <option value="">Add to collection…</option>{cols.filter((c) => c.kind === "manual").map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
          <button onClick={() => void bulk("accept_all_confident", undefined, "Accepted confident fields")}>Accept confident</button>
          <button onClick={() => void bulk("reprocess", undefined, "Reprocessing")}>Reprocess</button>
          <button onClick={async () => { const r = await send("/documents/bulk", { ids, action: "export", params: { format: "csv" } }); setMsg(`Exported ${r.file.rows} rows to ${r.file.path}`); }}>Export CSV</button>
          <button onClick={() => window.confirm(`Delete ${sel.size} document(s)?`) && void bulk("delete", undefined, "Deleted")}>Delete</button>
        </div>
      )}
      {msg && <p role="status">{msg}</p>}
      {error && <p role="alert">{error}</p>}
      {rows.length === 0 ? <div className="empty"><strong>Nothing here yet</strong>Drop a file and it will be read, classified and made searchable in seconds.</div> : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
            <th style={{ width: 28 }}><input type="checkbox" aria-label="Select all" checked={sel.size === rows.length} onChange={(e) => setSel(e.target.checked ? new Set(rows.map((r) => r.id)) : new Set())} /></th>
            <th>Title</th><th>Status</th><th>Pages</th><th>Size</th><th>Added</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} style={{ borderTop: "1px solid var(--border)" }}>
                <td><input type="checkbox" aria-label={`Select ${r.title}`} checked={sel.has(r.id)} onChange={() => toggle(r.id)} /></td>
                <td><button onClick={() => onOpen(r.id)} style={{ all: "unset", cursor: "pointer", color: "var(--accent)" }}>{r.title}</button></td>
                <td>{r.state === "failed" ? "⚠ failed" : r.state}</td>
                <td>{r.page_count ?? "–"}</td>
                <td>{formatBytes(r.size_bytes)}</td>
                <td>{new Date(r.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
