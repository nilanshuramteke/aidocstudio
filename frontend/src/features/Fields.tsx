import { useEffect, useState } from "react";
import { api } from "../lib";

export interface FieldRow {
  key: string; raw_value: string | null; value: string | null; confidence: number | null; status: string;
  flag_reason: string | null; page_no: number | null; bbox: number[] | null; extractor: string | null;
  validation: { message: string }[];
}
interface FieldsResponse { doc_type: string | null; doc_type_conf: number | null; degraded: string[]; fields: FieldRow[] }

/** Never colour-only: icon + number + colour (blueprint design system). */
export function confidenceBadge(c: number | null, auto = 0.9, soft = 0.7): { icon: string; label: string; color: string } {
  if (c === null) return { icon: "–", label: "n/a", color: "var(--text-muted)" };
  const pct = `${Math.round(c * 100)}%`;
  if (c >= auto) return { icon: "✓", label: pct, color: "var(--ok)" };
  if (c >= soft) return { icon: "!", label: pct, color: "var(--warn)" };
  return { icon: "⚠", label: pct, color: "var(--danger)" };
}

const REASONS: Record<string, string> = {
  low_confidence: "Low confidence", validation_failed: "Failed a check", missing_required: "Required, not found",
  disagreement: "Not found in the page text",
};

export function Fields({ docId, version, onSelect }: { docId: string; version: string; onSelect?: (f: FieldRow) => void }) {
  const [data, setData] = useState<FieldsResponse | null>(null);
  useEffect(() => { api<FieldsResponse>(`/documents/${docId}/fields`).then(setData).catch(() => setData(null)); }, [docId, version]);
  if (!data) return null;
  return (
    <section aria-label="Extracted fields">
      <h2 style={{ fontSize: 16 }}>
        {data.doc_type ?? "Unclassified"}
        {data.doc_type_conf !== null && <span style={{ color: "var(--text-muted)", fontWeight: 400 }}> · {Math.round(data.doc_type_conf * 100)}% sure</span>}
      </h2>
      {data.degraded.length > 0 && <p role="status" style={{ color: "var(--warn)" }}>Some steps ran without the local model; re-run when it is available.</p>}
      {data.fields.length === 0 ? <p style={{ color: "var(--text-muted)" }}>No fields for this type.</p> : (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <tbody>
            {data.fields.map((f) => {
              const b = confidenceBadge(f.confidence);
              return (
                <tr key={f.key} onClick={() => onSelect?.(f)} style={{ borderTop: "1px solid var(--border)", cursor: onSelect ? "pointer" : undefined }}>
                  <td style={{ padding: "var(--s2)", color: "var(--text-muted)" }}>{f.key}</td>
                  <td>{f.value ?? f.raw_value ?? <em>—</em>}</td>
                  <td style={{ color: b.color, whiteSpace: "nowrap" }}>{b.icon} {b.label}</td>
                  <td style={{ fontSize: 12 }}>
                    {f.flag_reason && <span>{REASONS[f.flag_reason] ?? f.flag_reason}</span>}
                    {f.validation.map((v, i) => <div key={i}>{v.message}</div>)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
