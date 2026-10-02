export interface Health {
  version: string;
  db: { ok: boolean; schema_version: number };
  capabilities: Record<string, unknown>;
  providers: Record<string, { configured: boolean; ok: boolean; detail: string }>;
  jobs: Record<string, number>;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...init });
  if (res.status === 423) window.dispatchEvent(new Event("adstudio:locked"));
  if (!res.ok) {
    const p = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(p.detail ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

/** Banner text for degraded states; null when everything needed is healthy. */
export function healthBanner(h: Health): string | null {
  if (!h.db.ok) return "Database integrity check failed.";
  if (!h.providers.llm?.ok) return "Connect a local model to enable Ask AI and smarter extraction.";
  return null;
}

export interface DocRow {
  id: string; title: string; original_name: string; mime: string; size_bytes: number; state: string;
  review_status: string; doc_type_id: string | null; doc_type_conf: number | null; page_count: number | null;
  created_at: string;
}
export interface DocDetail extends DocRow {
  state_detail: { stage?: string; error?: string } | null;
  pages: { page_no: number; width: number | null; height: number | null; text_source: string | null }[];
  metadata: Record<string, string>;
}
export interface ImportResultRow {
  name: string; status: string; document_id: string | null; reason: string | null; children?: ImportResultRow[];
}

export async function importFiles(files: FileList | File[]): Promise<ImportResultRow[]> {
  const fd = new FormData();
  Array.from(files).forEach((f) => fd.append("files", f));
  const r = await api<{ results: ImportResultRow[] }>("/documents/import", { method: "POST", body: fd });
  return r.results;
}

export function flattenResults(rows: ImportResultRow[]): ImportResultRow[] {
  return rows.flatMap((r) => (r.children?.length ? flattenResults(r.children) : [r]));
}

export function summarize(rows: ImportResultRow[]): string {
  const flat = flattenResults(rows);
  const n = (s: string) => flat.filter((r) => r.status === s).length;
  const parts = [`${n("created")} imported`];
  if (n("duplicate")) parts.push(`${n("duplicate")} duplicate`);
  if (n("rejected")) parts.push(`${n("rejected")} rejected`);
  return parts.join(", ");
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Subscribe to the multiplexed SSE stream; returns an unsubscribe function. */
export function subscribeEvents(onEvent: (event: string, data: any) => void): () => void {
  const es = new EventSource("/api/v1/events");
  for (const name of ["job.updated", "document.state"]) {
    es.addEventListener(name, (e) => onEvent(name, JSON.parse((e as MessageEvent).data)));
  }
  return () => es.close();
}
