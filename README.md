# DocSync - keeping system-analysis documentation in sync with code

Code changes in GitLab → documentation in Confluence is updated. No manual rewriting of integration tables,
C4 and sequence diagrams, but **with mandatory human approval** and with zero risk of overwriting hand-written text.

```
merge to main of a service ──GitLab webhook──▶ n8n ──▶ docsync engine ──▶ MR into architecture-model
                                                                                │ (analyst approves)
Confluence Cloud ◀── autoblocks: tables, C4 (Structurizr), sequence (PlantUML) ◀── publish ◀──┘
```

![C4 Container](docs/diagrams/c4-container.svg)

## What is inside

| Component | Role |
|---|---|
| **n8n** (`n8n/`) | Orchestrator: receives GitLab webhooks, calls the engine, sends notifications with an "approve" link, reminds about stale MRs, handles errors. 4 workflows generated from `n8n/build_workflows.py`. |
| **Notifications** | Slack (incoming webhook) and Telegram (bot with **Approve / Reject** buttons, long polling, no public URL needed). |
| **docsync engine** (`docsync/`) | Python engine: CLI + HTTP API. `extract` (facts from two git revisions) → `apply` (YAML model) → `validate` → `generate` (Structurizr DSL, PlantUML, XHTML autoblocks) → `render` (Kroki) → `publish` (Confluence) → `audit` (LLM looks for stale hand-written text). |
| **architecture-model** (`architecture-model/`) | Template of the model repository (Architecture as Code): YAML cards, `workspace.dsl` (views owned by the architect), `docs-registry.yaml` (which pages contain which autoblocks), Jinja2 templates. |
| **Kroki** | Renders Structurizr DSL and PlantUML to SVG. |
| **Structurizr server** (optional) | `DOCSYNC_C4_MODE=structurizr`: the workspace is pushed to the server on every publish and C4 blocks embed the live diagrams via iframe instead of SVG snapshots. |
| **Confluence** | `cloud` - real Confluence Cloud (REST v2); `mock` - files on disk with an HTML viewer for demos and tests. |

## Quick start (local demo, ~5 minutes)

Requires Docker and Python 3.11+. No Confluence or GitLab needed: GitLab is simulated, Confluence is mocked.

```bash
cp .env.example .env
make venv                      # .venv + dependencies
docker compose up -d --build   # n8n :5678, engine :8088, Kroki :8000
./n8n/import.sh                # import and activate the 4 workflows
./demo/run-stack.sh            # end-to-end: push → proposal → approve → publish (2 iterations)
```

Result: http://localhost:8088/mock-confluence/ - pages "as in Confluence" with refreshed autoblocks and SVGs;
http://localhost:5678 - workflow executions; http://localhost:8088/docs - engine OpenAPI.

![Page in mock Confluence](docs/images/mock-confluence-orders-top.jpg)

Without Docker (only Kroki in a container): `docker run -d -p 8000:8000 yuzutech/kroki && make demo`.
Tests: `make test` (20 tests including the end-to-end scenario and manual-edit detection). The acceptance run against Confluence Cloud and gitlab.com is described in `docs/07-confluence-cloud.md`.

Documentation, CLI, scripts and n8n notifications are in English. The content that DocSync writes into Confluence (panel title, table headers, generated descriptions, demo pages) is in Russian, the language of the target documentation space. It lives in `architecture-model/templates/*.j2`, `docsync/generate.py`, `docsync/llm.py` and `docsync/prompts/*.md`.

## Documentation

| File | Topic |
|---|---|
| [docs/01-overview.md](docs/01-overview.md) | The problem, three implementation options, why the hybrid was chosen |
| [docs/02-architecture.md](docs/02-architecture.md) | C4 context/container, components, key design decisions |
| [docs/03-pipeline.md](docs/03-pipeline.md) | End-to-end scenarios (sequence diagrams), run statuses, idempotency, conflicts, dry-run |
| [docs/04-model.md](docs/04-model.md) | Model format, `docsync.yaml` of a service, page registry, templates, how to add a service/page/view |
| [docs/05-n8n.md](docs/05-n8n.md) | n8n workflows, variables, GitLab webhooks, notifications to Slack/Teams/Telegram |
| [docs/06-local-demo.md](docs/06-local-demo.md) | Detailed local run and what to look at |
| [docs/07-confluence-cloud.md](docs/07-confluence-cloud.md) | Testing on your Confluence Cloud: token, page bootstrap, dry-run, PlantUML macro mode |
| [docs/08-production.md](docs/08-production.md) | Production deployment, security, limitations, extending extractor rules, roadmap |

## Repository layout

```
docsync/            engine (extractor, model, generate, render, autoblocks, confluence, githost, journal, llm, pipeline, api, cli, demo)
docsync/schemas/    JSON Schema: ChangeSet, integration card, docs-registry
docsync/prompts/    LLM prompts: ChangeSet enrichment, audit of hand-written sections
architecture-model/ model repository template (copied into data/repos by `demo seed`)
n8n/                build_workflows.py → workflows/*.json, import.sh
ci/                 .gitlab-ci.yml for architecture-model, service webhook guide
demo/run-stack.sh   end-to-end demo through n8n
docs/               documentation, diagrams (puml + svg/png), examples
tests/              pytest
Dockerfile, docker-compose.yml, Makefile, .env.example
```
