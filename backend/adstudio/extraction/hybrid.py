"""Rules first, LLM for the rest, then grounding against the source words (hallucination guard)."""
import json
import logging

from ..ai.promptstore import clip, load, parse_json_object
from ..core.interfaces import DocText, DocType, FieldResult, LLMProvider
from .confidence import LLM_CONF, RULE_CONF, RULE_LLM_AGREE_CONF
from .grounding import ground, key
from .normalize import normalize
from .rules import extract_by_rules

log = logging.getLogger("adstudio.extract")
PROMPT_VERSION = "extract.v2"  # v2 adds few-shot examples from human corrections


class HybridExtractor:
    def __init__(self, llm: LLMProvider | None = None, model_name: str = "",
                 examples: list[tuple[str, str | None, str, str | None]] | None = None):
        """`examples`: (field key, wrong value, corrected value, source line) from earlier human corrections."""
        self.llm, self.model_name, self.examples = llm, model_name, examples or []
        self.degraded: str | None = None

    def extract(self, doc: DocText, doc_type: DocType) -> list[FieldResult]:
        self.degraded = None
        fields = {f["key"]: f for f in doc_type.fields}
        rule_hits = extract_by_rules(doc, doc_type)
        llm_vals = self._llm_pass(doc, doc_type, [k for k in fields if k not in rule_hits]) if self.llm else {}
        # when the LLM is available also cross-check fields the rules found? Only for rule-missed fields (cost).
        results: list[FieldResult] = []
        for k, f in fields.items():
            kind = f.get("kind", "text")
            if k in rule_hits:
                raw, extractor, econf = rule_hits[k].raw, "rule", RULE_CONF
            elif llm_vals.get(k):
                raw, extractor, econf = llm_vals[k], f"llm:{self.model_name}:{PROMPT_VERSION}", LLM_CONF
            else:
                results.append(FieldResult(k, None, None, 0.0, flag="missing_required" if f.get("required") else None,
                                           extractor="none"))
                continue
            value = normalize(kind, raw)
            g = ground(raw, doc.pages)
            flag = None
            if g is None:
                flag = "disagreement"  # not found in the page text: possible hallucination
            results.append(FieldResult(
                key=k, raw_value=raw, value=value, extractor_conf=econf,
                page_no=g.page_no if g else None, bbox=g.bbox if g else None,
                ocr_conf=g.ocr_conf if g else None, grounded=g is not None, extractor=extractor, flag=flag))
        return results

    @staticmethod
    def _example_line(key: str, wrong: str | None, right: str, context: str | None) -> str:
        line = f"- {key}: " + (f"in the line {context!r} " if context else "") + f"the correct value is {right!r}"
        return line + (f" (not {wrong!r})" if wrong else "")

    def _llm_pass(self, doc: DocText, doc_type: DocType, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        fields = [f for f in doc_type.fields if f["key"] in keys]
        listing = "\n".join(f"- {f['key']}: {f.get('label', f['key'])} ({f.get('kind', 'text')})" for f in fields)
        schema = {"type": "object", "properties": {f["key"]: {"type": ["string", "null"]} for f in fields}}
        mine = [e for e in self.examples if e[0] in keys][:5]
        nl = "\n"
        ex = (f"{nl}Earlier human corrections for this document type (learn the pattern; do not copy these values):{nl}"
              + nl.join(self._example_line(*e) for e in mine) + nl) if mine else ""
        prompt = load(PROMPT_VERSION).format(type_name=doc_type.name, fields=listing, examples=ex, text=clip(doc.full_text))
        try:
            reply = self.llm.chat([{"role": "user", "content": prompt}], schema=schema, temperature=0.0)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM extraction failed: %s", e)
            self.degraded = "llm_unavailable"
            return {}
        obj = parse_json_object(reply if isinstance(reply, str) else "".join(reply)) or {}
        out = {}
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
                out[k] = str(v).strip()
        return out
