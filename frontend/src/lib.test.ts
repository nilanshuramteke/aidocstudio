import { describe, expect, it } from "vitest";
import { flattenResults, formatBytes, healthBanner, summarize, type Health } from "./lib";

const base: Health = {
  version: "0", db: { ok: true, schema_version: 1 }, capabilities: {}, jobs: {},
  providers: { llm: { configured: true, ok: true, detail: "" } },
};

describe("healthBanner", () => {
  it("is null when healthy", () => expect(healthBanner(base)).toBeNull());
  it("flags a missing LLM", () =>
    expect(healthBanner({ ...base, providers: { llm: { configured: false, ok: false, detail: "" } } })).toMatch(/local model/));
  it("flags db problems first", () =>
    expect(healthBanner({ ...base, db: { ok: false, schema_version: 1 } })).toMatch(/Database/));
});

describe("import helpers", () => {
  const rows = [
    { name: "a", status: "created", document_id: "1", reason: null },
    { name: "z.zip", status: "created", document_id: null, reason: null, children: [
      { name: "b", status: "duplicate", document_id: "2", reason: null },
      { name: "c", status: "rejected", document_id: null, reason: "unsupported" }] },
  ];
  it("flattens archive children", () => expect(flattenResults(rows).map((r) => r.name)).toEqual(["a", "b", "c"]));
  it("summarizes", () => expect(summarize(rows)).toBe("1 imported, 1 duplicate, 1 rejected"));
  it("formats bytes", () => expect(formatBytes(1536)).toBe("1.5 KB"));
});

import { confidenceBadge } from "./features/Fields";

describe("confidenceBadge", () => {
  it("is never colour-only", () => {
    expect(confidenceBadge(0.95)).toMatchObject({ icon: "✓", label: "95%" });
    expect(confidenceBadge(0.8).icon).toBe("!");
    expect(confidenceBadge(0.4).icon).toBe("⚠");
    expect(confidenceBadge(null).label).toBe("n/a");
  });
});

import { chipLabel, splitHighlights } from "./features/Search";

describe("search helpers", () => {
  it("splits highlight sentinels without interpreting HTML", () => {
    expect(splitHighlights("a hit <b>b</b>")).toEqual([
      { text: "a ", hit: false }, { text: "hit", hit: true }, { text: " <b>b</b>", hit: false }]);
  });
  it("labels chips", () => expect(chipLabel("amount_min", 50000)).toBe("Amount ≥: 50000"));
});

import { parseSSE, splitCitations } from "./features/Ask";

describe("chat helpers", () => {
  it("parses complete SSE events and keeps the partial remainder", () => {
    const buf = ["event: token", 'data: {"t":"Hi "}', "", "event: done", 'data: {"answer":"x"}', "", "event: tok"].join(String.fromCharCode(10));
    const { events, rest } = parseSSE(buf);
    expect(events).toEqual([{ event: "token", data: { t: "Hi " } }, { event: "done", data: { answer: "x" } }]);
    expect(rest).toBe("event: tok");
  });
  it("splits citation markers", () => {
    expect(splitCitations("a [S1] b [S2]")).toEqual([{ text: "a " }, { cite: 1 }, { text: " b " }, { cite: 2 }]);
  });
});

import { keyToAction, nextFlagged, overlayKind, progressText, rectFromDrag, type ReviewField } from "./features/reviewLogic";

const F = (key: string, status: string, extra: Partial<ReviewField> = {}): ReviewField => ({
  key, status, confidence: 0.8, flag_reason: null, page_no: 1, bbox: null, value: "v", raw_value: "v", ...extra });

describe("review keyboard model", () => {
  it("maps the blueprint keys", () => {
    expect(keyToAction("Enter", {})).toBe("accept");
    expect(keyToAction("e", {})).toBe("edit");
    expect(keyToAction("Backspace", {})).toBe("reject");
    expect(keyToAction("Tab", {})).toBe("next");
    expect(keyToAction("Tab", { shift: true })).toBe("prev");
    expect(keyToAction("j", {})).toBe("next");
    expect(keyToAction("r", {})).toBe("region");
    expect(keyToAction("z", { ctrl: true })).toBe("undo");
    expect(keyToAction("x", {})).toBe("none");
  });
  it("lets the edit input own the keyboard except Escape", () => {
    expect(keyToAction("e", { editing: true })).toBe("none");
    expect(keyToAction("Backspace", { editing: true })).toBe("none");
    expect(keyToAction("Escape", { editing: true })).toBe("cancel");
  });
  it("finds the next flagged field, wrapping, or -1", () => {
    const fs = [F("a", "needs_review"), F("b", "auto"), F("c", "needs_review"), F("d", "accepted")];
    expect(nextFlagged(fs, 0)).toBe(2);
    expect(nextFlagged(fs, 2)).toBe(0);
    expect(nextFlagged(fs, 0, -1)).toBe(2);
    expect(nextFlagged([F("a", "auto")], 0)).toBe(-1);
  });
  it("normalizes drags and ignores accidental clicks", () => {
    expect(rectFromDrag({ x: 50, y: 20 }, { x: 150, y: 60 }, 200, 100)).toEqual([0.25, 0.2, 0.5, 0.4]);
    expect(rectFromDrag({ x: 150, y: 60 }, { x: 50, y: 20 }, 200, 100)).toEqual([0.25, 0.2, 0.5, 0.4]); // any direction
    expect(rectFromDrag({ x: -30, y: -5 }, { x: 400, y: 300 }, 200, 100)).toEqual([0, 0, 1, 1]); // clamped to the page
    expect(rectFromDrag({ x: 10, y: 10 }, { x: 12, y: 11 }, 200, 100)).toBeNull();
  });
  it("describes progress and overlay state without relying on colour", () => {
    expect(progressText(1, 1)).toBe("1 field left · 1 document in queue");
    expect(progressText(12, 3)).toBe("12 fields left · 3 documents in queue");
    expect(overlayKind(F("a", "auto"))).toBe("ok");
    expect(overlayKind(F("a", "needs_review", { flag_reason: "low_confidence" }))).toBe("review");
    expect(overlayKind(F("a", "needs_review", { flag_reason: "validation_failed" }))).toBe("invalid");
  });
});

import { buildDefinition, describe as describeRule, emptyForm } from "./features/workflowForm";

describe("workflow form", () => {
  it("builds the blueprint JSON shape with typed values", () => {
    const def: any = buildDefinition({ name: "x", event: "document.ready", match: "all", conditions: [
      { field: "doc_type", op: "eq", value: "Invoice" }, { field: "field:total", op: "gt", value: "50,000" },
      { field: "tag", op: "in", value: "a, b" }, { field: "field:vendor", op: "exists", value: "" }, { field: " ", op: "eq", value: "ignored" }],
      actions: [{ type: "apply_tag", param: "big" }, { type: "export_copy", param: "D:/Out", template: "{vendor}/{original_name}" }] });
    expect(def.trigger).toEqual({ event: "document.ready" });
    expect(def.conditions.all).toEqual([{ field: "doc_type", op: "eq", value: "Invoice" }, { field: "field:total", op: "gt", value: 50000 },
      { field: "tag", op: "in", value: ["a", "b"] }, { field: "field:vendor", op: "exists" }]);
    expect(def.actions).toEqual([{ type: "apply_tag", tag: "big" }, { type: "export_copy", target: "D:/Out", template: "{vendor}/{original_name}" }]);
  });
  it("describes a rule in words", () => {
    expect(describeRule(buildDefinition({ ...emptyForm(), actions: [{ type: "notify", param: "hi" }] }))).toBe("When a document is ready, if 1 condition match: notify");
  });
});

import { linkText } from "./features/Related";

describe("related links", () => {
  it("words document links by direction", () => {
    expect(linkText({ kind: "duplicate", direction: "out" })).toBe("Duplicate of");
    expect(linkText({ kind: "supersedes", direction: "out" })).toBe("Replaces");
    expect(linkText({ kind: "supersedes", direction: "in" })).toBe("Replaced by");
  });
});
