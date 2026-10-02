.PHONY: dev test lint build
dev:
	cd backend && python -m adstudio.main --no-browser --port 8765 & cd frontend && npm run dev
test:
	cd backend && python -m pytest -q && lint-imports
	cd frontend && npm test
build:
	cd frontend && npm run build
