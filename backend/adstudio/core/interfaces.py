"""Provider contracts. Domain code imports only these, never concrete providers."""
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol


@dataclass
class ProviderHealth:
    ok: bool
    detail: str = ""


@dataclass
class OCRWord:
    text: str
    x: float  # normalized 0..1 relative to page
    y: float
    w: float
    h: float
    conf: float
    line: int = 0
    block: int = 0


@dataclass
class OCRPage:
    words: list[OCRWord] = field(default_factory=list)
    text: str = ""
    mean_conf: float = 0.0


@dataclass
class OCRCaps:
    langs: list[str]
    handwriting: bool = False
    tables: bool = False


class OCRProvider(Protocol):
    name: str
    def capabilities(self) -> OCRCaps: ...
    def recognize(self, image: bytes, lang: list[str]) -> OCRPage: ...
    def health(self) -> ProviderHealth: ...


class LLMProvider(Protocol):
    def chat(self, messages: list[dict], *, schema: dict | None = None,
             temperature: float = 0.0, stream: bool = False) -> Iterator[str] | str: ...
    def health(self) -> ProviderHealth: ...


class EmbeddingProvider(Protocol):
    model_id: str
    dim: int
    def embed(self, texts: list[str], *, kind: Literal["doc", "query"]) -> list[list[float]]: ...
    def health(self) -> ProviderHealth: ...


# ── Phase 3: classification / extraction / validation contracts ───────────
@dataclass
class PageText:
    page_no: int
    text: str
    words: list[OCRWord] | None = None  # None for native-text pages (docx/txt)


@dataclass
class DocText:
    filename: str
    pages: list[PageText]

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


@dataclass
class DocType:
    id: str
    name: str
    description: str = ""
    keywords: list = field(default_factory=list)  # [[keyword, weight], ...]
    fields: list[dict] = field(default_factory=list)  # [{key,label,kind,required,patterns,pick,validators}]


@dataclass
class Classification:
    type_name: str
    confidence: float
    evidence: str = ""
    method: str = "rule"  # rule | llm | rule+llm


@dataclass
class FieldResult:
    key: str
    raw_value: str | None
    value: str | None
    extractor_conf: float
    page_no: int | None = None
    bbox: list[float] | None = None  # [x, y, w, h] normalized
    ocr_conf: float | None = None
    grounded: bool = False
    extractor: str = "rule"
    flag: str | None = None  # missing_required | disagreement


@dataclass
class ValidationResult:
    rule_id: str
    field_key: str | None
    passed: bool
    message: str = ""


class DocumentClassifier(Protocol):
    def classify(self, doc: DocText, types: list[DocType]) -> Classification: ...


class DocumentExtractor(Protocol):
    def extract(self, doc: DocText, doc_type: DocType) -> list[FieldResult]: ...
