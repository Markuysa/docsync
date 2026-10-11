# architecture-model - template of the architecture model repository

This is the **source of truth** for DocSync. In production it is a separate GitLab project; in the demo it is copied to `data/repos/architecture-model`.

| Path | Who edits | What it is |
|---|---|---|
| `model/systems/*.yaml` | architect | systems (domains), owners, analysts |
| `model/services/*.yaml` | architect / DocSync (`service_added`) | services = C4 containers, datastores |
| `model/external/*.yaml` | architect / DocSync (Kafka topics) | external systems and queues |
| `model/integrations/*.yaml` | **DocSync** (via MR); the analyst edits `description`, `nfr`, `manual` | relationships `<from>__<to>__<protocol>` |
| `model/flows/*.yaml` | analyst | scenarios for sequence diagrams |
| `workspace.dsl` | architect | Structurizr DSL: views; elements are inlined from `model/generated/` |
| `docs-registry.yaml` | analyst | which Confluence page contains which autoblocks |
| `templates/*.j2` | analyst / platform | autoblock templates (XHTML) and PlantUML |
| `changesets/*.json` | DocSync | audit trail: which ChangeSet produced which MR |

Rules:
* DocSync **never** touches `manual.*` in cards.
* Everything DocSync changes goes into an MR labelled `docsync` - a human merges.
* `docsync validate` is mandatory in the model CI (see `ci/architecture-model.gitlab-ci.yml`).
* Demo content (names, descriptions, page texts) is in Russian because the target Confluence space is Russian-speaking.
