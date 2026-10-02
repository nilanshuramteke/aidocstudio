import { useEffect, useState } from "react";
import { api, type DocDetail } from "../lib";
import { Ask } from "./Ask";
import { Fields } from "./Fields";
import { Related } from "./Related";

interface Word { t: string; x: number; y: number; w: number; h: number; c: number }

export function Detail({ id, onBack, initialPage = 1, onOpenCitation, onOpenEntity }: {
  id: string; onBack: () => void; initialPage?: number; onOpenCitation?: (docId: string, page: number) => void;
  onOpenEntity?: (id: string) => void;
}) {
  const [doc, setDoc] = useState<DocDetail | null>(null);
  const [page, setPage] = useState(initialPage);
  const [text, setText] = useState<string | null>(null);
  const [noImage, setNoImage] = useState(false);
  const [words, setWords] = useState<Word[]>([]);
  const [find, setFind] = useState("");

  useEffect(() => { api<DocDetail>(`/documents/${id}`).then(setDoc).catch(() => setDoc(null)); }, [id]);
  useEffect(() => { setNoImage(false); setText(null); }, [id, page]);
  useEffect(() => {
    if (noImage) api<{ text: string }>(`/documents/${id}/pages/${page}/text`).then((t) => setText(t.text)).catch(() => {});
  }, [noImage, id, page]);

  useEffect(() => {
    setWords([]);
    api<{ words: Word[] }>(`/documents/${id}/pages/${page}/words`).then((r) => setWords(r.words)).catch(() => {});
  }, [id, page]);

  if (!doc) return <p>Loading…</p>;
  const pages = doc.page_count ?? 0;
  return (
    <div>
      <button onClick={onBack}>← Documents</button>
      <h1>{doc.title}</h1>
      <p style={{ color: "var(--text-muted)" }}>
        {doc.original_name} · {doc.state}{doc.state_detail?.error ? ` · ${doc.state_detail.error}` : ""}
      </p>
      {pages > 1 && (
        <div role="group" aria-label="Pages">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
          <span style={{ margin: "0 var(--s3)" }}>Page {page} of {pages}</span>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
      {words.length > 0 && (
        <input placeholder="Find in page text…" aria-label="Find in page" value={find}
          onChange={(e) => setFind(e.target.value)} style={{ margin: "var(--s3) 0", padding: "var(--s2)" }} />
      )}
      <div className="split">
      <div className="viewer">
      {pages > 0 && !noImage && (
        <div style={{ position: "relative", display: "inline-block", maxWidth: "100%", marginTop: "var(--s3)" }}>
          <img alt={`Page ${page}`} src={`/api/v1/documents/${id}/pages/${page}/image?w=900`}
            onError={() => setNoImage(true)} style={{ maxWidth: "100%", border: "1px solid var(--border)", display: "block" }} />
          {find.trim() && words.filter((w) => w.t.toLowerCase().includes(find.trim().toLowerCase())).map((w, i) => (
            <div key={i} aria-hidden style={{ position: "absolute", left: `${w.x * 100}%`, top: `${w.y * 100}%`,
              width: `${w.w * 100}%`, height: `${w.h * 100}%`, outline: "2px solid var(--warn)", background: "rgba(232,137,12,.18)" }} />
          ))}
        </div>
      )}
      {noImage && <pre style={{ whiteSpace: "pre-wrap", background: "var(--surface)", padding: "var(--s4)" }}>{text ?? "…"}</pre>}
      </div>
      <aside className="side">
      <Fields docId={id} version={doc.state} />
      <Related docId={id} version={doc.state} onOpenDoc={(d) => onOpenCitation?.(d, 1)} onOpenEntity={(e) => onOpenEntity?.(e)} />
      <h2 style={{ fontSize: 16 }}>Ask about this document</h2>
      <Ask documentIds={[id]} placeholder="Ask about this document…"
        onOpenCitation={(d, p) => (d === id ? setPage(p) : onOpenCitation?.(d, p))} />
      <p><a href={`/api/v1/documents/${id}/file`} target="_blank" rel="noreferrer">Open original</a></p>
      </aside>
      </div>
    </div>
  );
}
