"""Cheap keyword classifier. Keywords live in document_types.keywords_json as [[keyword, weight], ...]."""
import re

from ..core.interfaces import Classification, DocText, DocType

MIN_SCORE = 3  # below this the evidence is too thin: fall back to "Other" with low confidence


def _rx(keyword: str) -> re.Pattern:
    # tolerate OCR that dropped spaces: "tax invoice" also matches "taxinvoice"
    return re.compile(r"\s*".join(re.escape(p) for p in keyword.split()), re.IGNORECASE)


class RuleClassifier:
    def classify(self, doc: DocText, types: list[DocType]) -> Classification:
        text = doc.pages[0].text if doc.pages else ""
        # first page carries the signal; also look a little further for short multi-page docs
        text = text + "\n" + "\n".join(p.text for p in doc.pages[1:3])
        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for t in types:
            for kw, weight in t.keywords:
                if _rx(kw).search(text):
                    scores[t.name] = scores.get(t.name, 0) + weight
                    hits.setdefault(t.name, []).append(kw)
                if _rx(kw).search(doc.filename):
                    scores[t.name] = scores.get(t.name, 0) + 2  # filename hint
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] < MIN_SCORE:
            return Classification("Other", 0.4, "no strong keyword evidence", "rule")
        best, best_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        margin = (best_score - second) / best_score
        conf = min(0.95, 0.50 + 0.04 * best_score + 0.30 * margin)
        return Classification(best, round(conf, 3), "keywords: " + ", ".join(hits[best][:5]), "rule")
