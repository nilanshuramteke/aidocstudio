import { useEffect, useState } from "react";
import { api } from "../lib";

interface RelatedResponse {
  entities: { id: string; name: string; kind: string; role: string }[];
  related: { id: string; title: string; via: string }[];
  links: { kind: string; direction: string; other_id: string; other_title: string }[];
}
interface EntityResponse {
  id: string; name: string; kind: string; aliases: string[]; identifiers: Record<string, string>;
  documents: { id: string; title: string; role: string }[];
  relationships: { kind: string; direction: string; other_id: string; other_name: string; evidence_documents: number }[];
}

export function linkText(l: { kind: string; direction: string }): string {
  if (l.kind === "duplicate") return "Duplicate of";
  if (l.kind === "supersedes") return l.direction === "out" ? "Replaces" : "Replaced by";
  return l.kind;
}

export function Related({ docId, version, onOpenDoc, onOpenEntity }: {
  docId: string; version: string; onOpenDoc: (id: string) => void; onOpenEntity: (id: string) => void;
}) {
  const [r, setR] = useState<RelatedResponse | null>(null);
  useEffect(() => { api<RelatedResponse>(`/documents/${docId}/related`).then(setR).catch(() => setR(null)); }, [docId, version]);
  if (!r || (!r.entities.length && !r.related.length && !r.links.length)) return null;
  return (
    <section aria-label="Related">
      <h2 style={{ fontSize: 16 }}>Related</h2>
      {r.entities.length > 0 && <p>{r.entities.map((e) => (
        <button key={e.id + e.role} onClick={() => onOpenEntity(e.id)} style={{ marginRight: "var(--s2)" }}>{e.name} <small>({e.role})</small></button>))}</p>}
      {r.links.map((l) => <p key={l.kind + l.other_id}>{linkText(l)} <button onClick={() => onOpenDoc(l.other_id)}>{l.other_title}</button></p>)}
      {r.related.length > 0 && <ul>{r.related.map((d) => (
        <li key={d.id}><button onClick={() => onOpenDoc(d.id)}>{d.title}</button> <small style={{ color: "var(--text-muted)" }}>via {d.via}</small></li>))}</ul>}
    </section>
  );
}

export function EntityPage({ id, onBack, onOpenDoc, onOpenEntity }: {
  id: string; onBack: () => void; onOpenDoc: (id: string) => void; onOpenEntity: (id: string) => void;
}) {
  const [e, setE] = useState<EntityResponse | null>(null);
  useEffect(() => { api<EntityResponse>(`/entities/${id}`).then(setE).catch(() => setE(null)); }, [id]);
  if (!e) return <p>Loading…</p>;
  return (
    <div>
      <button onClick={onBack}>← Back</button>
      <h1 style={{ fontSize: 20 }}>{e.name} <small style={{ color: "var(--text-muted)" }}>{e.kind}</small></h1>
      {e.aliases.length > 1 && <p style={{ color: "var(--text-muted)" }}>Also seen as: {e.aliases.filter((a) => a !== e.name).join(" · ")}</p>}
      {Object.entries(e.identifiers).map(([k, v]) => <p key={k}>{k.toUpperCase()}: {v}</p>)}
      <h2 style={{ fontSize: 16 }}>Documents ({e.documents.length})</h2>
      <ul>{e.documents.map((d) => <li key={d.id + d.role}><button onClick={() => onOpenDoc(d.id)}>{d.title}</button> <small>{d.role}</small></li>)}</ul>
      <h2 style={{ fontSize: 16 }}>Relationships</h2>
      {e.relationships.length === 0 ? <p style={{ color: "var(--text-muted)" }}>None yet.</p> : (
        <ul>{e.relationships.map((r) => (
          <li key={r.kind + r.direction + r.other_id}>{r.direction === "out" ? `${r.kind} →` : `← ${r.kind}`} <button onClick={() => onOpenEntity(r.other_id)}>{r.other_name}</button>
            <small> ({r.evidence_documents} document{r.evidence_documents === 1 ? "" : "s"})</small></li>))}</ul>
      )}
    </div>
  );
}
