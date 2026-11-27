# Virtualis — comandi di sviluppo e rilascio
# Uso: make <target>   (make help per l'elenco)

.DEFAULT_GOAL := help

.PHONY: help setup test test-e2e lint build dev-backend dev-frontend docker-up docker-build eval cert demo-seed collaudo backup load-test

help: ## Mostra questo elenco
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Ambiente completo: venv backend + deps Python + deps npm
	cd backend && uv venv && uv pip install -e ".[dev]"
	cd frontend && npm install

test: ## Test backend (pytest)
	cd backend && uv run pytest -q

test-e2e: ## Test end-to-end Playwright (build frontend + backend reale su dati di test, porta 8033)
	cd frontend && npm run test:e2e

lint: ## Lint completo: ruff + mypy (backend) + oxlint (frontend)
	cd backend && uv run ruff check app tests scripts
	cd backend && uv run mypy app
	cd frontend && npm run lint

build: ## Build di produzione del frontend (typecheck + vite)
	cd frontend && npm run build

dev-backend: ## Backend in sviluppo con reload (porta 8000)
	cd backend && uv run uvicorn app.api.main:app --reload --port 8000

dev-frontend: ## Frontend in sviluppo (porta 5173, proxy /api → 8000)
	cd frontend && npm run dev

docker-up: ## Stack completo in produzione (build + avvio detached)
	docker compose up -d --build

docker-build: ## Solo build delle immagini Docker
	docker compose build

eval: ## Batteria di valutazione continua (richiede MOONSHOT_API_KEY e corpus indicizzato)
	cd backend && uv run python scripts/evaluate.py

cert: ## Certificato TLS self-signed per HTTPS su LAN
	cd backend && uv run python scripts/gen_cert.py

collaudo: ## Collaudo end-to-end sul corpus demo (richiede MOONSHOT_API_KEY attiva)
	cd backend && uv run python scripts/collaudo.py

demo-seed: ## Genera il corpus dimostrativo in backend/demo_corpus/ (con INGEST=1 lo indicizza in data-demo/)
ifeq ($(INGEST),1)
	cd backend && uv run python scripts/demo_seed.py --ingest
else
	cd backend && uv run python scripts/demo_seed.py
endif

backup: ## Backup di data_dir (sqlite, upload, qdrant embedded) in backend/backups/
	cd backend && uv run python scripts/backup.py backup

load-test: ## Test di carico sulla ricerca ibrida (backend attivo; PW=richiesta)
	cd backend && uv run python scripts/load_test.py --password "$(PW)"
