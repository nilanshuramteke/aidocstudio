/** Pure review-screen logic (no React) so the keyboard model is unit-testable. */

export type ReviewAction =
  | "accept" | "edit" | "reject" | "next" | "prev" | "region" | "undo" | "complete" | "cancel" | "none";

/** Blueprint keys: Enter accept, E edit, Backspace reject, Tab/J next flagged, K previous, R region, Ctrl+Z undo. */
export function keyToAction(key: string, opts: { ctrl?: boolean; shift?: boolean; editing?: boolean }): ReviewAction {
  if (opts.editing) return key === "Escape" ? "cancel" : "none"; // the input owns every other key (Enter submits it)
  if (opts.ctrl && key.toLowerCase() === "z") return "undo";
  if (opts.ctrl) return "none";
  switch (key) {
    case "Enter": return "accept";
    case "e": case "E": return "edit";
    case "Backspace": case "Delete": return "reject";
    case "Tab": return opts.shift ? "prev" : "next";
    case "j": case "J": return "next";
    case "k": case "K": return "prev";
    case "r": case "R": return "region";
    case "n": case "N": return "complete";
    case "Escape": return "cancel";
    default: return "none";
  }
}

export interface ReviewField {
  key: string; status: string; confidence: number | null; flag_reason: string | null;
  page_no: number | null; bbox: number[] | null; value: string | null; raw_value: string | null;
}

export const isPending = (f: ReviewField) => f.status === "needs_review";

/** Index of the next (dir=1) or previous (dir=-1) field still needing review, wrapping; -1 if none. */
export function nextFlagged(fields: ReviewField[], from: number, dir: 1 | -1 = 1): number {
  const n = fields.length;
  for (let step = 1; step <= n; step++) {
    const i = (((from + dir * step) % n) + n) % n;
    if (isPending(fields[i])) return i;
  }
  return -1;
}

/** Normalized [x, y, w, h] from two pointer positions inside an element of size (w, h); null if too small. */
export function rectFromDrag(a: { x: number; y: number }, b: { x: number; y: number }, w: number, h: number, minPx = 6): number[] | null {
  const clamp = (v: number, max: number) => Math.min(Math.max(v, 0), max);
  const x0 = clamp(Math.min(a.x, b.x), w), x1 = clamp(Math.max(a.x, b.x), w);
  const y0 = clamp(Math.min(a.y, b.y), h), y1 = clamp(Math.max(a.y, b.y), h);
  if (x1 - x0 < minPx || y1 - y0 < minPx) return null;
  return [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h].map((v) => Math.round(v * 1e5) / 1e5);
}

export function progressText(fieldsLeft: number, docsInQueue: number): string {
  const f = `${fieldsLeft} field${fieldsLeft === 1 ? "" : "s"} left`;
  return `${f} · ${docsInQueue} document${docsInQueue === 1 ? "" : "s"} in queue`;
}

/** Overlay style by field state: solid = fine, dashed = needs review (never colour alone). */
export function overlayKind(f: ReviewField): "ok" | "review" | "invalid" {
  if (f.status === "needs_review") return f.flag_reason === "validation_failed" || f.flag_reason === "missing_required" ? "invalid" : "review";
  return "ok";
}
