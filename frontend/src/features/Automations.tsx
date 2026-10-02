import { useCallback, useEffect, useState } from "react";
import { api } from "../lib";
import { ACTIONS, buildDefinition, describe, emptyForm, EVENTS, OPS, type FormState } from "./workflowForm";

interface Workflow { id: string; name: string; enabled: boolean; definition: any; last_run: { status: string; finished_at: string } | null }
interface Watch { id: string; path: string; recursive: boolean; enabled: boolean; after_import: string; last_scan_at: string | null;
  last_scan: { imported: number; duplicate: number; rejected: number } | null }

const send = (path: string, method: string, body?: unknown) =>
  api<any>(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

export function Automations() {
  const [wfs, setWfs] = useState<Workflow[]>([]);
  const [watch, setWatch] = useState<Watch[]>([]);
  const [form, setForm] = useState<FormState | null>(null);
  const [testDoc, setTestDoc] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [folder, setFolder] = useState("");

  const load = useCallback(() => {
    api<{ items: Workflow[] }>("/workflows").then((r) => setWfs(r.items));
    api<{ items: Watch[] }>("/watch-folders").then((r) => setWatch(r.items));
  }, []);
  useEffect(load, [load]);
  const guard = async (fn: () => Promise<unknown>) => { setError(null); try { await fn(); load(); } catch (e) { setError((e as Error).message); } };

  return (
    <div>
      {error && <p role="alert">{error}</p>}
      <h2 style={{ fontSize: 16 }}>Rules</h2>
      {wfs.length === 0 && <p style={{ color: "var(--text-muted)" }}>No rules yet.</p>}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {wfs.map((w) => (
          <li key={w.id} style={{ borderTop: "1px solid var(--border)", padding: "var(--s2) 0" }}>
            <label><input type="checkbox" checked={w.enabled} onChange={(e) => void guard(() => send(`/workflows/${w.id}`, "PATCH", { enabled: e.target.checked }))} /> <strong>{w.name}</strong></label>
            <div style={{ color: "var(--text-muted)", fontSize: 13 }}>{describe(w.definition)} · last run: {w.last_run ? w.last_run.status : "never"}</div>
            <button onClick={() => void guard(() => send(`/workflows/${w.id}`, "DELETE"))}>Delete</button>
            <span> Test on document id: <input aria-label="Test document id" size={26} value={testDoc} onChange={(e) => setTestDoc(e.target.value)} />
              <button onClick={() => void guard(async () => { const r = await send(`/workflows/${w.id}/run`, "POST", { document_id: testDoc, dry_run: true });
                setResult(JSON.stringify({ matched: r.matched, conditions: r.conditions, actions: r.actions }, null, 2)); })}>Dry run</button></span>
          </li>
        ))}
      </ul>
      {result && <pre aria-label="Dry run result" style={{ background: "var(--surface)", padding: "var(--s3)", overflow: "auto" }}>{result}</pre>}
      {!form ? <button onClick={() => setForm(emptyForm())}>New rule</button> : (
        <fieldset style={{ border: "1px solid var(--border)", padding: "var(--s3)" }}>
          <legend>New rule</legend>
          <input aria-label="Rule name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <p>When <select aria-label="Event" value={form.event} onChange={(e) => setForm({ ...form, event: e.target.value })}>
            {EVENTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></p>
          <p>If <select aria-label="Match" value={form.match} onChange={(e) => setForm({ ...form, match: e.target.value as "all" | "any" })}>
            <option value="all">all</option><option value="any">any</option></select> of:</p>
          {form.conditions.map((c, i) => (
            <div key={i}>
              <input aria-label="Field" placeholder="field:total / doc_type / tag" value={c.field} onChange={(e) => setForm({ ...form, conditions: form.conditions.map((x, j) => j === i ? { ...x, field: e.target.value } : x) })} />
              <select aria-label="Operator" value={c.op} onChange={(e) => setForm({ ...form, conditions: form.conditions.map((x, j) => j === i ? { ...x, op: e.target.value } : x) })}>
                {OPS.map((o) => <option key={o}>{o}</option>)}</select>
              <input aria-label="Value" value={c.value} onChange={(e) => setForm({ ...form, conditions: form.conditions.map((x, j) => j === i ? { ...x, value: e.target.value } : x) })} />
              <button onClick={() => setForm({ ...form, conditions: form.conditions.filter((_, j) => j !== i) })}>✕</button>
            </div>
          ))}
          <button onClick={() => setForm({ ...form, conditions: [...form.conditions, { field: "", op: "eq", value: "" }] })}>+ condition</button>
          <p>Then:</p>
          {form.actions.map((a, i) => (
            <div key={i}>
              <select aria-label="Action" value={a.type} onChange={(e) => setForm({ ...form, actions: form.actions.map((x, j) => j === i ? { ...x, type: e.target.value } : x) })}>
                {ACTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
              <input aria-label="Action parameter" value={a.param} onChange={(e) => setForm({ ...form, actions: form.actions.map((x, j) => j === i ? { ...x, param: e.target.value } : x) })} />
              {a.type === "export_copy" && <input aria-label="Folder template" placeholder="{vendor}/{invoice_date:%Y-%m}/{original_name}" value={a.template ?? ""}
                onChange={(e) => setForm({ ...form, actions: form.actions.map((x, j) => j === i ? { ...x, template: e.target.value } : x) })} />}
              <button onClick={() => setForm({ ...form, actions: form.actions.filter((_, j) => j !== i) })}>✕</button>
            </div>
          ))}
          <button onClick={() => setForm({ ...form, actions: [...form.actions, { type: "apply_tag", param: "" }] })}>+ action</button>
          <p><button onClick={() => void guard(async () => { await send("/workflows", "POST", { name: form.name, definition: buildDefinition(form) }); setForm(null); })}>Save rule</button>
            <button onClick={() => setForm(null)}>Cancel</button></p>
        </fieldset>
      )}
      <h2 style={{ fontSize: 16 }}>Watch folders</h2>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {watch.map((w) => (
          <li key={w.id} style={{ borderTop: "1px solid var(--border)", padding: "var(--s2) 0" }}>
            <strong>{w.path}</strong> · after import: {w.after_import} · {w.last_scan ? `last scan: ${w.last_scan.imported} imported, ${w.last_scan.rejected} rejected` : "not scanned yet"}
            <button onClick={() => void guard(() => send(`/watch-folders/${w.id}/scan`, "POST"))}>Scan now</button>
            <button onClick={() => void guard(() => send(`/watch-folders/${w.id}`, "DELETE"))}>Remove</button>
          </li>
        ))}
      </ul>
      <input aria-label="Folder path" placeholder="C:\Scans\Inbox" value={folder} onChange={(e) => setFolder(e.target.value)} />
      <button onClick={() => void guard(async () => { await send("/watch-folders", "POST", { path: folder }); setFolder(""); })}>Watch folder</button>
    </div>
  );
}
