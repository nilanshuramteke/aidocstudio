# Contributing

Thanks for your interest. This is a local-first app, so the bar for any change is: documents stay on the user's machine, and extracted values stay traceable to the page.

## Set up
```
cd backend && python -m venv .venv && .venv\Scripts\pip install -e .[dev]   # Windows
cd ../frontend && npm install
```
Run `make test` before opening a PR. It runs pytest, import-linter and the Vitest suite. `python scripts/smoke.py` runs the real app end to end (build the frontend first).

## Ground rules
- **Respect the layers:** `api → services → providers/storage`. Import-linter enforces it.
- **Providers sit behind interfaces** (OCR, LLM, embeddings). Add a fake in `core/fakes.py` so tests don't need a model.
- **Never let a model invent a value.** Extracted text must be grounded against OCR words; see `extraction/grounding.py`.
- **Add a test** with every bug fix. Pure UI logic goes in a plain `.ts` module with a Vitest test.
- **No network calls, telemetry or external fonts/CDNs.** The CSP is `self` only.
- **Record decisions** that change the design in `docs/adr/`.
- Frontend changes should follow the tokens in `frontend/src/styles/tokens.css`, work in light and dark, and be keyboard-accessible.

## Good first issues
Pick any item from the "Known limitations" list in the README. Small, well-bounded starting points:
- Run the evals with `nomic-embed-text` and record the numbers (`evals/`, `scripts/bench_search.py`).
- Add a second OCR engine's results to `evals/ocr` (Tesseract or PaddleOCR).
- Add validators for another locale (VAT numbers, US EIN, IBAN) in `validation/builtin.py`.
- Add document types and rules for bank statements or utility bills.
- Replace PyMuPDF with `pypdfium2` behind the existing page-renderer interface (unblocks a permissive license).
- Move document parsing into a subprocess with a timeout.
- Cross-platform CI: confirm the suite passes on Linux and macOS and fix what doesn't.

## Pull requests
Keep them focused, describe the user-visible change, and note what you tested. By contributing you agree that your work is licensed under the project's AGPL-3.0 license.
