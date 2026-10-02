# ADR 001 — Corrections to BLUEPRINT.md found in review

Decision · apply these deviations from the blueprint.

1. **FTS5 external content (§7):** `chunks` must use `rowid INTEGER PRIMARY KEY` (+ `id TEXT UNIQUE`); `chunk_vec` keyed by that rowid. A TEXT-keyed table's implicit rowid can be renumbered by VACUUM and silently break `chunks_fts`. Applies in Phase 4.
2. **Missing FKs:** `document_links` and `validation_results.rule_id` get FKs with ON DELETE CASCADE (Phase 1/3 migrations).
3. **Capability probe at startup** (`storage.db.probe_capabilities`): SQLite >= 3.37, FTS5, `enable_load_extension` (sqlite-vec). Vector search falls back to FTS-only when the extension cannot load. Done in Phase 0.
4. **IDs:** in-repo ULID (`core/ids.py`); stdlib has no UUIDv7 before Python 3.14.
5. **Windows process pool:** long-lived OCR workers with a model-loading initializer; per-page timeout recycles the worker (Phase 2).
6. **OCR default candidate:** RapidOCR (onnxruntime) alongside Tesseract/Paddle; pick by Phase 2 eval.
7. **Vision model** (`ornith-1.5:9b`) as optional re-extract/low-confidence fallback behind `LLMProvider`; grounding rule still applies.
8. **PyMuPDF (AGPL):** accepted for personal/open-source use; keep `PageRenderer` interface so `pypdfium2` is a swap.
9. **Single writer:** implemented as one serialized write connection behind a lock (`Database.write()`), not a dedicated thread; same guarantee, less machinery. Revisit if bulk-import contention shows up.
10. **Eval thresholds** are set from Phase 2/3 baselines, not invented upfront.

## Phase 2 findings
11. **OCR default = RapidOCR** (only engine measured; Tesseract binary not installed on the dev machine, adapter and contract path exist but are unmeasured). Baseline on the synthetic set (`evals/ocr/results.json`): CER 0.049 clean / 0.070 noisy / 0.048 skewed; WER 0.38-0.69 because RapidOCR often drops inter-word spaces. Re-decide when Tesseract/Paddle numbers exist.
12. **onnxruntime thread oversubscription:** default threads-per-core made RapidOCR 10x slower and erratic on a 22-core machine (3-40 s/page). Workers default to 1 intra-op thread (`ADSTUDIO_OCR_THREADS`), parallelism comes from 2 worker processes: ~4 s/page for a 1000x320 image on a loaded machine.
13. **Re-paginate keeps page ids** (upsert) so cached `ocr_results` survive; the cache key is (page, engine, language set).

## Phase 4 findings
14. **Search latency (scripts/bench_search.py, 10k docs / 30k chunks / dim 768, loaded 22-core dev box):** keyword p50 ~25 ms (p95 40-180 ms); hybrid p50 ~270-295 ms (p95 700-1000 ms). Targets are keyword < 100 ms and hybrid < 500 ms: p50 meets both, hybrid **p95 does not** on this machine. Brute-force KNN alone is ~85 ms; planned lever is int8 quantization (blueprint Appendix B) before any ANN swap.
15. **Semantic search needs an embedding model:** `embedding.model` is empty by default, so search is keyword-only (reported in the response as `semantic.available=false`) until the user pulls one (e.g. `ollama pull nomic-embed-text`) and sets it. Changing the model resets `chunk_vec` and re-embeds every document.
16. **Fusion is document-level RRF** (keyword chunks, field hits, vector chunks each contribute their best rank per document), not chunk-level; snippets still come from the best chunks.
17. **FTS key:** `chunks.rid INTEGER PRIMARY KEY` is the FTS content rowid (correction #1 applied).

## Phase 5 findings
18. **Citation post-check is lexical, not entailment** (`ai/faithfulness.py`): invalid `[S#]` indexes are removed and flagged; cited sentences must contain all numbers/IDs of the source and >= 60% of content-word prefixes. It catches fabricated citations and wrong figures, not subtle misreadings. The faithfulness judge by a second local model (blueprint section 25) is not built; `evals/rag/run.py` reports citation accuracy/abstention against a real model.
19. **No real-model numbers yet:** Ollama was not running when Phase 3-5 were built, so every LLM path (classify, extract, chat, compare) is covered with scripted fakes only. Run `python evals/rag/run.py` and a few real documents before trusting the defaults (confidence 0.75 for LLM values is an uncalibrated placeholder).
20. **Abstention:** if retrieval returns nothing the model is not called at all; the model abstains with the sentinel `NOT_FOUND`, shown as "Not found in your documents."
21. **Compare** answers each (document, aspect) separately, drops answers not grounded in the retrieved passages, diffs in code, and gives the model only the table for the narrative.

## Phase 7-9 findings
22. **Graph traversal is a bounded BFS in Python, not a recursive CTE.** The CTE took 27 s on hub entities at 100k entities; BFS with node/depth caps is ~0.2 s (`scripts/bench_graph.py`).
23. **Entity resolution** uses a normalized name plus a space-less blocking key (`nk`, so "A.B.C." matches "ABC") and GSTIN as a strong identifier. A name the user corrected is never overridden by an identifier match.
24. **Hybrid search p95 miss stands** (see 14). int8 quantization remains the planned lever.
25. **Encryption scope:** SQLCipher (`sqlcipher3-wheels`, Argon2id-derived raw key, optional OS-keychain cache) encrypts the database only. Original files under `files/` are NOT encrypted; use BitLocker/FileVault for full-disk protection. Encrypted backups use scrypt + chunked AES-GCM.
26. **App lock is separate from encryption.** It is server-side state (Argon2 hash in `lock.json`, rate-limited unlock with back-off, idle timeout). The API answers 423 while locked, and the SPA shows a lock screen. It guards a running app against someone at the keyboard, not the files at rest.
27. **Frontend design system:** local-only tokens (no CDN or external fonts, CSP is `self`), rail navigation, Ctrl/Cmd+K command palette, light/dark/system theme, empty and loading states.

## Not built (honest list)
LAN mode, handwriting OCR, plugin loader, auto-updater, tray icon, packaging (PyInstaller), subprocess sandbox for document parsing (parsers run in-process), LLM reranker, `set_field` / `move_managed_file` workflow actions, encryption of original files. Real-LLM and embedding behavior and Tesseract accuracy are unmeasured.
