import { useRef, useState } from "react";
import { importFiles, summarize } from "../lib";

export function DropZone({ onDone }: { onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function go(files: FileList | File[]) {
    if (!files.length) return;
    setBusy(true);
    try {
      setMsg(summarize(await importFiles(files)));
      onDone();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={`dropzone${over ? " over" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); void go(e.dataTransfer.files); }}
      style={{ textAlign: "center" }}
    >
      <p style={{ margin: 0, fontWeight: 600 }}>Drop documents here</p>
      <p style={{ margin: "4px 0 0", color: "var(--text-muted)" }}>PDF, images, Word, Excel, email, CSV, JSON, or a ZIP of them</p>
      <button className="primary" disabled={busy} onClick={() => input.current?.click()} style={{ marginTop: "var(--s4)" }}>
        {busy ? "Importing…" : "Choose files"}
      </button>
      <input ref={input} type="file" multiple hidden aria-label="Import files"
        onChange={(e) => e.target.files && void go(e.target.files)} />
      {msg && <p role="status" style={{ color: "var(--text-muted)" }}>{msg}</p>}
    </div>
  );
}
