"""Deterministic fakes so every module is testable without models."""
import hashlib
import re
from typing import Iterator, Literal

from .interfaces import OCRCaps, OCRPage, OCRWord, ProviderHealth


class FakeOCR:
    name = "fake-ocr"

    def __init__(self, text: str = "hello world"):
        self._text = text

    def capabilities(self) -> OCRCaps:
        return OCRCaps(langs=["eng"])

    def recognize(self, image: bytes, lang: list[str]) -> OCRPage:
        words = [OCRWord(t, 0.1 * i, 0.1, 0.08, 0.03, 0.95) for i, t in enumerate(self._text.split())]
        return OCRPage(words=words, text=self._text, mean_conf=0.95)

    def health(self) -> ProviderHealth:
        return ProviderHealth(True, "fake")


class FakeLLM:
    def __init__(self, reply: str = "{}"):
        self.reply = reply

    def chat(self, messages, *, schema=None, temperature=0.0, stream=False) -> Iterator[str] | str:
        if stream:
            return iter(re.findall(r"\S+\s*", self.reply))
        return self.reply

    def health(self) -> ProviderHealth:
        return ProviderHealth(True, "fake")


class FakeEmbedding:
    model_id = "fake-embed"
    dim = 8

    def embed(self, texts: list[str], *, kind: Literal["doc", "query"]) -> list[list[float]]:
        return [[b / 255 for b in hashlib.sha256(t.encode()).digest()[: self.dim]] for t in texts]

    def health(self) -> ProviderHealth:
        return ProviderHealth(True, "fake")
