.PHONY: venv test seed demo serve up down n8n-import diagrams clean

venv:            ## local Python environment
	python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev,llm]"

test:            ## tests (Kroki optional)
	.venv/bin/python -m pytest -q

seed:            ## sample repositories in data/
	.venv/bin/docsync demo seed

demo: seed       ## full local scenario without Docker (needs Kroki: make up or docker run -p 8000:8000 yuzutech/kroki)
	.venv/bin/docsync demo run

serve:           ## engine HTTP API locally
	.venv/bin/docsync serve --port 8088

up:              ## stack: n8n + engine + Kroki
	docker compose up -d --build

down:
	docker compose down

n8n-import:      ## import and activate DocSync workflows in n8n (container must be running)
	./n8n/import.sh

diagrams:        ## re-render documentation diagrams (docs/diagrams/*.puml -> svg/png)
	.venv/bin/python docs/diagrams/render.py

clean:
	rm -rf data build .pytest_cache
