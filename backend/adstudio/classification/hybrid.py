"""Rules first; the LLM is consulted only when rule confidence is low. Falls back to rules if no LLM."""
import logging

from ..ai.promptstore import clip, load, parse_json_object
from ..core.interfaces import Classification, DocText, DocType, LLMProvider
from .rule import RuleClassifier

log = logging.getLogger("adstudio.classify")
PROMPT_VERSION = "classify.v1"
LLM_THRESHOLD = 0.85  # rule confidence at or above this is accepted without asking the model


class LLMClassifier:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    def classify(self, doc: DocText, types: list[DocType]) -> Classification | None:
        names = [t.name for t in types]
        listing = "\n".join(f"- {t.name}: {t.description}" for t in types)
        schema = {"type": "object", "required": ["type", "confidence"], "properties": {
            "type": {"type": "string", "enum": names}, "confidence": {"type": "number"},
            "evidence": {"type": "string"}}}
        prompt = load(PROMPT_VERSION).format(types=listing, text=clip(doc.full_text, 6000))
        reply = self.llm.chat([{"role": "user", "content": prompt}], schema=schema, temperature=0.0)
        obj = parse_json_object(reply if isinstance(reply, str) else "".join(reply))
        if not obj or obj.get("type") not in names:
            return None
        try:
            conf = max(0.0, min(1.0, float(obj.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        return Classification(obj["type"], conf, str(obj.get("evidence", ""))[:200], "llm")


class HybridClassifier:
    def __init__(self, llm: LLMProvider | None = None):
        self.rule = RuleClassifier()
        self.llm = LLMClassifier(llm) if llm else None
        self.degraded: str | None = None  # set when the LLM was needed but unusable

    def classify(self, doc: DocText, types: list[DocType]) -> Classification:
        self.degraded = None
        r = self.rule.classify(doc, types)
        if r.confidence >= LLM_THRESHOLD or self.llm is None:
            if r.confidence < LLM_THRESHOLD:
                self.degraded = "llm_unavailable"
            return r
        try:
            m = self.llm.classify(doc, types)
        except Exception as e:  # noqa: BLE001 - LLM down/slow must never break the pipeline
            log.warning("LLM classification failed: %s", e)
            self.degraded = "llm_unavailable"
            return r
        if m is None:
            return r
        if m.type_name == r.type_name:
            return Classification(r.type_name, max(0.9, r.confidence, m.confidence), r.evidence, "rule+llm")
        # disagreement: trust the model's type but cap confidence so a human looks at it
        return Classification(m.type_name, min(m.confidence, 0.8),
                              f"LLM chose {m.type_name}; rules chose {r.type_name}. {m.evidence}", "llm")
