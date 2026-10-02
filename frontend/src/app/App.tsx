import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Ask } from "../features/Ask";
import { Automations } from "../features/Automations";
import { SettingsPage } from "../features/Settings";
import { Detail } from "../features/Detail";
import { Documents } from "../features/Documents";
import { DropZone } from "../features/DropZone";
import { EntityPage } from "../features/Related";
import { Review } from "../features/Review";
import { Search } from "../features/Search";
import { api, healthBanner, type Health } from "../lib";

const NAV = ["Home", "Documents", "Search", "Review", "Ask AI", "Automations", "Settings"] as const;
type Page = (typeof NAV)[number];

const ICONS: Record<Page, ReactNode> = {
  Home: <path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10" />,
  Documents: <path d="M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6" />,
  Search: <path d="M11 4a7 7 0 100 14 7 7 0 000-14zM21 21l-5-5" />,
  Review: <path d="M5 12l4 4 10-10" />,
  "Ask AI": <path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" />,
  Automations: <path d="M13 2L4 14h7l-1 8 9-12h-7z" />,
  Settings: <path d="M12 9a3 3 0 100 6 3 3 0 000-6zM19 12l2-1-2-4-2 1-2-1-1-2h-4l-1 2-2 1-2-1-2 4 2 1v2l-2 1 2 4 2-1 2 1 1 2h4l1-2 2-1 2 1 2-4-2-1z" />,
};

function Icon({ name }: { name: Page | "lock" | "sun" }) {
  const d = name === "lock" ? <path d="M6 11h12v9H6zM8 11V8a4 4 0 018 0v3" />
    : name === "sun" ? <path d="M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5" />
    : ICONS[name];
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>;
}

interface LockStatus { enabled: boolean; locked: boolean; retry_after: number }

function LockScreen({ onUnlocked }: { onUnlocked: () => void }) {
  const [pass, setPass] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => ref.current?.focus(), []);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/lock/unlock", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passphrase: pass }) });
      setPass(""); setErr(null); onUnlocked();
    } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
  }
  return (
    <div className="lockscreen" role="dialog" aria-modal="true" aria-label="Locked">
      <form className="lockcard" onSubmit={submit}>
        <div className="brand-mark" style={{ margin: "0 auto var(--s4)" }} />
        <h1 style={{ fontSize: "22px" }}>Doc Studio is locked</h1>
        <p style={{ color: "var(--text-muted)", margin: "6px 0 0" }}>Enter your passphrase to continue.</p>
        <input ref={ref} type="password" aria-label="Passphrase" value={pass} onChange={(e) => setPass(e.target.value)} placeholder="Passphrase" />
        {err && <p role="alert" style={{ color: "var(--danger)" }}>{err}</p>}
        <button className="primary" type="submit" disabled={busy || !pass} style={{ width: "100%" }}>{busy ? "Checking…" : "Unlock"}</button>
      </form>
    </div>
  );
}

interface Cmd { id: string; label: string; group: string; run: () => void }

function Palette({ commands, onClose }: { commands: Cmd[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const items = useMemo(() => {
    const t = q.trim().toLowerCase();
    const base = t ? commands.filter((c) => c.label.toLowerCase().includes(t)) : commands;
    return t ? [...base, { id: "search", group: "Search", label: `Search documents for “${q.trim()}”`, run: () => commands.find((c) => c.id === "q")?.run() } as Cmd] : base;
  }, [q, commands]);
  useEffect(() => setSel(0), [q]);
  const fire = (c: Cmd | undefined) => { if (!c) return; onClose(); if (c.id === "search") window.dispatchEvent(new CustomEvent("adstudio:search", { detail: q.trim() })); else c.run(); };
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <input autoFocus placeholder="Type a command or search…" aria-label="Command" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(items.length - 1, s + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
            else if (e.key === "Enter") fire(items[sel]);
            else if (e.key === "Escape") onClose();
          }} />
        <ul role="listbox">
          {items.map((c, i) => (
            <li key={c.id} role="presentation">
              {(i === 0 || items[i - 1].group !== c.group) && <div className="group">{c.group}</div>}
              <button role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={() => fire(c)}>{c.label}</button>
            </li>
          ))}
          {!items.length && <li className="group">No matches</li>}
        </ul>
        <footer><span><kbd>↑</kbd> <kbd>↓</kbd> navigate</span><span><kbd>Enter</kbd> select</span><span><kbd>Esc</kbd> close</span></footer>
      </div>
    </div>
  );
}

const THEMES = ["system", "light", "dark"] as const;

export function App() {
  const [page, setPage] = useState<Page>("Home");
  const [openId, setOpenId] = useState<string | null>(null);
  const [openPage, setOpenPage] = useState(1);
  const [entityId, setEntityId] = useState<string | null>(null);
  const open = (id: string, p = 1) => { setEntityId(null); setOpenPage(p); setOpenId(id); };
  const openEntity = (id: string) => { setOpenId(null); setEntityId(id); };
  const [tick, setTick] = useState(0);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lock, setLock] = useState<LockStatus | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [reviewCount, setReviewCount] = useState(0);
  const [docCount, setDocCount] = useState<number | null>(null);
  const [homeQuery, setHomeQuery] = useState("");
  const [searchSeed, setSearchSeed] = useState<string | null>(null);
  const [theme, setTheme] = useState<(typeof THEMES)[number]>(() => {
    try { const t = localStorage.getItem("adstudio.theme"); return THEMES.includes(t as never) ? (t as (typeof THEMES)[number]) : "system"; } catch { return "system"; }
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", theme);
    try { localStorage.setItem("adstudio.theme", theme); } catch { /* storage unavailable */ }
  }, [theme]);

  const refreshLock = useCallback(() => api<LockStatus>("/lock/status").then(setLock).catch(() => undefined), []);
  useEffect(() => {
    void refreshLock();
    const onLocked = () => setLock((l) => ({ enabled: true, retry_after: 0, ...l, locked: true }));
    window.addEventListener("adstudio:locked", onLocked);
    const t = window.setInterval(refreshLock, 15000);
    return () => { window.removeEventListener("adstudio:locked", onLocked); window.clearInterval(t); };
  }, [refreshLock]);

  const locked = lock?.locked ?? false;
  const loadStats = useCallback(() => {
    api<Health>("/health").then(setHealth).catch((e: Error) => setError(e.message));
    api<{ documents: number }>("/review/queue").then((q) => setReviewCount(q.documents)).catch(() => undefined);
    api<{ items: unknown[] }>("/documents?limit=500").then((r) => setDocCount(r.items.length)).catch(() => undefined);
  }, []);
  useEffect(() => { if (!locked) loadStats(); }, [locked, tick, loadStats]);

  const go = useCallback((n: Page) => { setOpenId(null); setEntityId(null); setPage(n); }, []);
  const runSearch = useCallback((q: string) => { setSearchSeed(q); go("Search"); }, [go]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen((o) => !o); } };
    const onSearch = (e: Event) => runSearch((e as CustomEvent<string>).detail);
    window.addEventListener("keydown", onKey);
    window.addEventListener("adstudio:search", onSearch);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("adstudio:search", onSearch); };
  }, [runSearch]);

  const commands: Cmd[] = useMemo(() => [
    ...NAV.map((n) => ({ id: `go:${n}`, group: "Go to", label: n, run: () => go(n) })),
    { id: "theme", group: "Appearance", label: "Switch theme (system → light → dark)", run: () => setTheme((t) => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length]) },
    ...(lock?.enabled ? [{ id: "lock", group: "Security", label: "Lock now", run: () => { void api("/lock/lock", { method: "POST" }).then(refreshLock); } }] : []),
  ], [go, lock?.enabled, refreshLock]);

  const banner = error ?? (health ? healthBanner(health) : null);
  const jobs = health?.jobs ?? {};
  const busy = (jobs["running"] ?? 0) + (jobs["queued"] ?? 0);

  if (locked) return <LockScreen onUnlocked={() => { void refreshLock(); setTick((t) => t + 1); }} />;

  return (
    <div className="app">
      <aside className="rail">
        <div className="brand"><span className="brand-mark" />Doc Studio</div>
        <button className="cmdk-hint" onClick={() => setPaletteOpen(true)} aria-label="Open command palette">
          <span>Search or jump to…</span><kbd>Ctrl K</kbd>
        </button>
        <nav aria-label="Primary">
          <ul className="nav">
            {NAV.map((n) => (
              <li key={n}>
                <button onClick={() => go(n)} aria-current={page === n && !openId && !entityId ? "page" : undefined}>
                  <Icon name={n} />{n}
                  {n === "Review" && reviewCount > 0 && <span className="count">{reviewCount}</span>}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <div className="rail-foot">
          <small style={{ color: "var(--text-muted)", padding: "0 var(--s3)" }}>
            <span className={`status-dot ${health ? (health.db.ok ? "ok" : "bad") : ""}`} />
            {busy > 0 ? `Processing ${busy} job${busy > 1 ? "s" : ""}` : "Everything local"}
          </small>
          <button className="ghost" onClick={() => setTheme((t) => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length])} title="Theme">
            <Icon name="sun" /> <span style={{ marginLeft: 8 }}>Theme: {theme}</span>
          </button>
          {lock?.enabled && <button className="ghost" onClick={() => void api("/lock/lock", { method: "POST" }).then(refreshLock)}><Icon name="lock" /> <span style={{ marginLeft: 8 }}>Lock</span></button>}
        </div>
      </aside>
      <main className="main page" key={`${page}:${openId}:${entityId}`}>
        {banner && <div role="status" className="banner">{banner}</div>}
        {entityId ? <EntityPage id={entityId} onBack={() => setEntityId(null)} onOpenDoc={(d) => open(d)} onOpenEntity={openEntity} /> : openId ? <Detail key={`${openId}:${openPage}`} id={openId} initialPage={openPage} onBack={() => setOpenId(null)} onOpenCitation={open} onOpenEntity={openEntity} /> : (
          <>
            {page === "Home" ? (
              <>
                <section className="hero">
                  <h1>Everything in your documents, found in seconds.</h1>
                  <p>Drop in files, and they are read, classified and indexed on this machine. Nothing leaves it.</p>
                  <form className="hero-search" onSubmit={(e) => { e.preventDefault(); if (homeQuery.trim()) runSearch(homeQuery.trim()); }}>
                    <Icon name="Search" />
                    <input aria-label="Search all documents" placeholder="Try “invoices over 50,000 from March”" value={homeQuery} onChange={(e) => setHomeQuery(e.target.value)} />
                    <button className="primary" type="submit">Search</button>
                  </form>
                </section>
                <div className="tiles">
                  <button className="tile" onClick={() => go("Documents")}><div className="num">{docCount ?? "–"}</div><div className="lbl">Documents</div></button>
                  <button className={`tile${reviewCount ? " alert" : ""}`} onClick={() => go("Review")}><div className="num">{reviewCount}</div><div className="lbl">Waiting for review</div></button>
                  <button className="tile" onClick={() => go("Documents")}><div className="num">{busy}</div><div className="lbl">Processing now</div></button>
                  <button className="tile" onClick={() => go("Ask AI")}><div className="num">{health?.providers.llm?.ok ? "Ready" : "Off"}</div><div className="lbl">Local AI model</div></button>
                </div>
                <DropZone onDone={() => setTick((t) => t + 1)} />
                <h2>Recent documents</h2>
                <Documents key={tick} showDrop={false} limit={8} onOpen={(id) => open(id)} />
              </>
            ) : (
              <header className="page-head"><h1>{page}</h1></header>
            )}
            {page === "Documents" && <Documents onOpen={(id) => open(id)} />}
            {page === "Search" && <Search key={searchSeed ?? ""} initialQuery={searchSeed ?? undefined} onOpen={(id) => open(id)} />}
            {page === "Ask AI" && <Ask onOpenCitation={open} />}
            {page === "Review" && <Review onOpenDoc={(id) => open(id)} />}
            {page === "Automations" && <Automations />}
            {page === "Settings" && <SettingsPage health={health} onLockChange={refreshLock} />}
          </>
        )}
      </main>
      {paletteOpen && <Palette commands={commands} onClose={() => setPaletteOpen(false)} />}
    </div>
  );
}
