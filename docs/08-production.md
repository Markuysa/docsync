# 8. Production, security, limitations, roadmap

## Deployment

| Component | How |
|---|---|
| docsync engine | image from `Dockerfile` (`docsync serve`), 1 replica is enough (a run takes seconds; publishing tens of seconds); `data/` on a persistent volume; `DOCSYNC_GIT_MODE=gitlab` |
| n8n | your existing instance; import `n8n/workflows/*.json`; variables from section 5 |
| Kroki | `yuzutech/kroki` on the internal network (diagram sources never leave it) |
| Structurizr server (C4 live mode) | `structurizr/onpremises:2025.11.09` (last free on-premises release; `structurizr/structurizr server` is the licensed successor) behind https, reachable from readers' browsers; admin API key as bcrypt hash in `structurizr.properties`; one workspace per model repository |
| architecture-model | a GitLab project; CI from `ci/architecture-model.gitlab-ci.yml` (`validate` on MR and main, diagram preview in the MR); CODEOWNERS on `model/integrations/*` → domain analysts |
| services | group webhook + `docsync.yaml` (see `ci/service-webhook.md`); no CI job needed |

Least-privilege accounts: a GitLab project/group access token with `api` + `write_repository` on `architecture-model` and `read_repository` on the services; a Confluence API token of a technical account with rights on the space.

## Security

* `X-Gitlab-Token` is checked in n8n; `X-DocSync-Token` - on every `/api/*` of the engine (except `/healthz`, `/notify`).
* Code, evidence and page content are passed to the LLM as **data**; the prompts explicitly require ignoring instructions inside. The LLM has no write tools - the only side effect of the audit (comments) is created by the engine after guardrails.
* The engine writes to Confluence only between anchors and only after a merge into the model; every publish is a new page version (rollback with standard Confluence means).
* Only evidence snippets (up to 400 characters per line, up to 5 per event) and hand-written page sections go to the LLM, never whole repositories. If code must not leave the perimeter - `LLM_PROVIDER=openai-compatible` with an internal gateway, or `none`.

## Performance and scale

* Ingest: clone/fetch + scanning two revisions. For large monorepos narrow `include` in `docsync.yaml`.
* Publish refreshes only blocks depending on the changed elements and their neighbours; a full publish (`changed=None`) - for manual model edits without `before` or after registry migrations.
* Concurrent publishes of one page: the engine is single-threaded per instance (uvicorn worker 1) - a Confluence version conflict is impossible within one instance; with several replicas add a per-`pageId` lock in the journal (the table is ready for extension).

## Known limitations

1. The extractor is a set of regex rules for typical patterns (Spring/Kotlin gRPC + config, Go gRPC, Python gRPC, Kafka, HTTP base-url). Non-standard client wrappers need new rules or aliases. Nothing unrecognised is lost: it ends up in the MR as `unresolved`.
2. HTTP operations are extracted heuristically (`.get("/path")`); for OpenAPI contracts it is better to read the spec - `RELEVANT_GLOBS` already includes it.
3. Sequence scenarios (`flows`) are owned by the analyst: DocSync does not invent call order, it only redraws the diagram and validates that steps reference live integrations.
4. A removed integration is marked `deprecated`, not deleted: final deletion of the card is the analyst's decision in the MR.
5. Confluence Data Center and GitHub are not supported out of the box (the `ConfluenceClient`/`GitHost` interfaces allow adding them).

## Roadmap (epics)

| Epic | Content | Estimate |
|---|---|---|
| E1. Pilot on one domain | accounts, group webhook, `docsync.yaml` in 3–5 services, page bootstrap, 2 weeks in dry-run, then live | 2–3 weeks |
| E2. Model bootstrap | run the extractor over `main` of all domain services (`before` = root commit) → initial model → analyst validation | 1–2 weeks |
| E3. Rules for your stacks | gRPC through DI/wrappers, REST clients (Feign/Retrofit/OpenAPI generator), RabbitMQ, S3/JDBC | 1–3 days per pattern |
| E4. Scale-out | remaining domains, CODEOWNERS, a dashboard over `GET /api/v1/kpi` (doc lag, acceptance rate, conflicts) | 2 weeks |
| E5. LLM audit in production | provider choice with security, weekly drift audit of the whole space, usefulness metric of comments | 1–2 weeks |
| E6. Optional | Confluence DC, GitHub, Structurizr on-prem as a single diagram host, C4 export into a PlantUML macro | on demand |
