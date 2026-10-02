/** Form <-> workflow JSON. The builder is "When... / If... / Then..." rows; this file is the (testable) translation. */

export const EVENTS = [
  ["document.imported", "a document is imported"], ["document.classified", "a document is classified"],
  ["document.extracted", "fields are extracted"], ["document.ready", "a document is ready"],
  ["document.needs_review", "a document needs review"], ["document.review_resolved", "a review is finished"],
] as const;
export const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "matches", "in", "exists"] as const;
export const ACTIONS = [
  ["apply_tag", "Apply tag", "tag"], ["remove_tag", "Remove tag", "tag"], ["add_to_collection", "Add to collection", "collection"],
  ["export_copy", "Copy file to folder", "target"], ["run_export", "Export fields (CSV/JSON/XLSX)", "format"],
  ["notify", "Notify me", "message"], ["webhook", "Call a webhook (off by default)", "url"],
] as const;

export interface CondRow { field: string; op: string; value: string }
export interface ActionRow { type: string; param: string; template?: string }
export interface FormState { name: string; event: string; match: "all" | "any"; conditions: CondRow[]; actions: ActionRow[] }

const NUMERIC_OPS = new Set(["gt", "gte", "lt", "lte"]);

function coerce(op: string, raw: string): string | number | string[] | undefined {
  if (op === "exists") return undefined;
  if (op === "in") return raw.split(",").map((s) => s.trim()).filter(Boolean);
  if (NUMERIC_OPS.has(op) && raw.trim() !== "" && !Number.isNaN(Number(raw.replace(/,/g, "")))) return Number(raw.replace(/,/g, ""));
  return raw;
}

export function buildDefinition(f: FormState): Record<string, unknown> {
  const leaves = f.conditions.filter((c) => c.field.trim()).map((c) => {
    const v = coerce(c.op, c.value);
    return v === undefined ? { field: c.field.trim(), op: c.op } : { field: c.field.trim(), op: c.op, value: v };
  });
  const actions = f.actions.map((a) => {
    const key = ACTIONS.find((x) => x[0] === a.type)?.[2] ?? "tag";
    const out: Record<string, unknown> = { type: a.type, [key]: a.param };
    if (a.type === "export_copy" && a.template) out.template = a.template;
    return out;
  });
  return { trigger: { event: f.event }, conditions: { [f.match]: leaves }, actions };
}

export function describe(def: any): string {
  const ev = EVENTS.find((e) => e[0] === def?.trigger?.event)?.[1] ?? def?.trigger?.event;
  const n = (def?.conditions?.all ?? def?.conditions?.any ?? []).length;
  return `When ${ev}${n ? `, if ${n} condition${n > 1 ? "s" : ""} match` : ""}: ${(def?.actions ?? []).map((a: any) => a.type).join(", ")}`;
}

export function emptyForm(): FormState {
  return { name: "", event: "document.ready", match: "all", conditions: [{ field: "doc_type", op: "eq", value: "Invoice" }],
    actions: [{ type: "apply_tag", param: "" }] };
}
