import { useCallback, useEffect, useState } from "react";
import { api, type Health } from "../lib";

type Settings = Record<string, string | number | boolean | string[]>;
interface Backup { name: string; size: number; encrypted: boolean; auto: boolean }

const send = (path: string, method: string, body?: unknown) =>
  api<any>(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

const NUMBER_KEYS = ["review.auto_accept", "review.soft_review", "review.ocr_retry_below", "backup.interval_hours", "backup.keep"];
const TEXT_KEYS = ["llm.base_url", "llm.model", "embedding.model", "ocr.engine"];

export function SettingsPage({ health, onLockChange }: { health: Health | null; onLockChange?: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [lockOn, setLockOn] = useState(false);
  const [lockPass, setLockPass] = useState("");
  const [backups, setBackups] = useState<Backup[]>([]);
  const [pass, setPass] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    api<Settings>("/settings").then(setS);
    api<{ items: Backup[] }>("/backups").then((r) => setBackups(r.items));
    api<{ enabled: boolean }>("/lock/status").then((l) => setLockOn(l.enabled)).catch(() => undefined);
  }, []);
  useEffect(load, [load]);
  const run = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); setMsg(ok); load(); } catch (e) { setMsg((e as Error).message); } };

  if (!s) return <p>Loading…</p>;
  const field = (k: string, numeric: boolean) => (
    <label key={k} style={{ display: "block", margin: "var(--s2) 0" }}>
      <span style={{ display: "inline-block", width: 220 }}>{k}</span>
      <input defaultValue={String(s[k])} onBlur={(e) => { const v = numeric ? Number(e.target.value) : e.target.value;
        if (v !== s[k]) void run(() => send("/settings", "PATCH", { [k]: v }), `${k} saved`); }} />
    </label>
  );
  return (
    <div>
      {msg && <p role="status">{msg}</p>}
      <h2 style={{ fontSize: 16 }}>Models</h2>
      <p style={{ color: "var(--text-muted)" }}>
        Local model: {health?.providers.llm?.ok ? "connected" : health?.providers.llm?.detail ?? "not configured"} ·
        Embeddings: {health?.providers.embedding?.ok ? "connected" : "off (keyword search only)"} · OCR: {health?.providers.ocr?.ok ? "ready" : "not available"}
      </p>
      {TEXT_KEYS.map((k) => field(k, false))}
      <p style={{ color: "var(--text-muted)", fontSize: 12 }}>Model changes take effect after restarting the app.</p>
      <h2 style={{ fontSize: 16 }}>Review thresholds</h2>
      {NUMBER_KEYS.slice(0, 3).map((k) => field(k, true))}
      <h2 style={{ fontSize: 16 }}>Automation</h2>
      <label><input type="checkbox" checked={Boolean(s["automation.allow_webhooks"])} onChange={(e) => void run(() => send("/settings", "PATCH", { "automation.allow_webhooks": e.target.checked }), "Saved")} /> Allow workflow webhooks (sends data to the network)</label>
      <h2 style={{ fontSize: 16 }}>App lock</h2>
      <p style={{ color: "var(--text-muted)" }}>
        {lockOn ? "A passphrase is required to open the app, and it re-locks when idle." : "Set a passphrase to lock the app when you step away."}
      </p>
      <input aria-label="App lock passphrase" type="password" placeholder={lockOn ? "Current passphrase (to turn off)" : "New passphrase (6+ characters)"}
        value={lockPass} onChange={(e) => setLockPass(e.target.value)} />
      <button disabled={!lockPass} onClick={() => void run(async () => {
        await send(lockOn ? "/lock/disable" : "/lock/setup", "POST", { passphrase: lockPass });
        setLockPass(""); onLockChange?.();
      }, lockOn ? "App lock turned off" : "App lock turned on")}>{lockOn ? "Turn off" : "Turn on"}</button>
      {lockOn && field("security.idle_lock_minutes", true)}
      <h2 style={{ fontSize: 16 }}>Appearance</h2>
      <p style={{ color: "var(--text-muted)" }}>Use the theme button at the bottom of the sidebar, or press Ctrl+K and choose “Switch theme”.</p>
      <h2 style={{ fontSize: 16 }}>Backup &amp; restore</h2>
      {NUMBER_KEYS.slice(3).map((k) => field(k, true))}
      <input aria-label="Backup passphrase" type="password" placeholder="Passphrase (optional, encrypts the backup)" value={pass} onChange={(e) => setPass(e.target.value)} />
      <button onClick={() => void run(() => send("/backup", "POST", { passphrase: pass || undefined }), "Backup created")}>Back up now</button>
      <ul>{backups.map((b) => (
        <li key={b.name}>{b.name} · {(b.size / 1024 / 1024).toFixed(1)} MB{b.encrypted ? " · 🔒" : ""}
          <a href={`/api/v1/backups/${b.name}`}> download</a>
          <button onClick={() => void run(() => send("/restore", "POST", { name: b.name, passphrase: pass || undefined }), "Restore staged. Restart the app to apply it.")}>Restore…</button></li>
      ))}</ul>
      <h2 style={{ fontSize: 16 }}>System</h2>
      <pre style={{ color: "var(--text-muted)" }}>{JSON.stringify(health, null, 2)}</pre>
    </div>
  );
}
