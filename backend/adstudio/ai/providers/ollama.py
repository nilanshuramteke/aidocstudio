"""Ollama HTTP adapter (stdlib only). One in-flight request at a time: local models are the bottleneck."""
import json
import threading
import urllib.error
import urllib.request
from typing import Iterator

from ...core.interfaces import ProviderHealth


class OllamaLLM:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = "llama3.1:8b", timeout_s: float = 300.0):
        self.base_url, self.model, self.timeout_s = base_url.rstrip("/"), model, timeout_s
        self._gate = threading.Semaphore(1)  # resource class "llm" = 1

    def _post(self, path: str, body: dict, timeout: float | None = None):
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout or self.timeout_s)

    def health(self) -> ProviderHealth:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=3) as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(False, f"Ollama not reachable at {self.base_url} ({type(e).__name__})")
        if self.model not in names and f"{self.model}:latest" not in names:
            return ProviderHealth(False, f"Model '{self.model}' not pulled (run: ollama pull {self.model})")
        return ProviderHealth(True, self.model)

    def chat(self, messages: list[dict], *, schema: dict | None = None, temperature: float = 0.0,
             stream: bool = False) -> Iterator[str] | str:
        body = {"model": self.model, "messages": messages, "stream": stream, "options": {"temperature": temperature}}
        if schema is not None:
            body["format"] = schema  # structured output: Ollama constrains decoding to the JSON schema
        if not stream:
            with self._gate, self._post("/api/chat", body) as r:
                return json.load(r)["message"]["content"]
        return self._stream(body)

    def _stream(self, body: dict) -> Iterator[str]:
        with self._gate, self._post("/api/chat", body) as r:
            for line in r:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("message", {}).get("content"):
                    yield chunk["message"]["content"]
                if chunk.get("done"):
                    return


class OllamaEmbedding:
    """Embeddings via Ollama /api/embed. `model_id` is recorded with the vectors so a model change is detectable."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 120.0):
        self.base_url, self.model_id, self.timeout_s = base_url.rstrip("/"), model, timeout_s
        self.dim = 0  # learned from the first response
        self._gate = threading.Semaphore(1)

    def _prefix(self, kind: str) -> str:
        # nomic-embed-text was trained with task prefixes; others ignore them harmlessly, so only add where needed
        if "nomic" in self.model_id:
            return "search_document: " if kind == "doc" else "search_query: "
        return ""

    def health(self) -> ProviderHealth:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=3) as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(False, f"Ollama not reachable ({type(e).__name__})")
        if self.model_id not in names and f"{self.model_id}:latest" not in names:
            return ProviderHealth(False, f"Embedding model not pulled (run: ollama pull {self.model_id})")
        return ProviderHealth(True, self.model_id)

    def embed(self, texts, *, kind):
        pre = self._prefix(kind)
        req = urllib.request.Request(self.base_url + "/api/embed", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": self.model_id, "input": [pre + t for t in texts]}).encode())
        with self._gate, urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            vecs = json.load(r)["embeddings"]
        if vecs:
            self.dim = len(vecs[0])
        return vecs
