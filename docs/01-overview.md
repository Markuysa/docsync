# 1. The problem and implementation options

## The problem

System-analysis artifacts (integration descriptions, C4 diagrams, sequence diagrams, NFRs) live in a Confluence space.
When any service merges to `main` (for example, a new gRPC call to another service appears), we need to:

1. understand **what changed in the architecture** (relationships between systems, not code in general);
2. update **every** document where that relationship appears: integration tables of both services, the domain matrix, C4 Container/Context, sequence scenarios;
3. never break what the analyst wrote by hand, and give them a way to **approve** the change;
4. do it through **n8n**, moving towards **Architecture as Code + Structurizr** for C4.

## Three options that were considered

| | Option 1. "LLM edits pages" | Option 2. Architecture as Code (chosen as the core) | Option 3. Agentic loop |
|---|---|---|---|
| Idea | n8n hands the diff and current pages to an LLM, the LLM returns new page text | **Facts** (relationships, protocols, operations) are extracted from code deterministically → applied to a **model** in git → every artifact is **generated** from the model → published into autoblocks | An LLM agent with tools (Confluence search, page read, model query) finds stale statements itself |
| Completeness of "update all documents" | Depends on what fits into the context; easy to miss a page | Complete: the registry knows which pages depend on which model elements | Complete with enough iterations, but non-deterministic |
| Determinism, reproducibility | No | Yes: same input → same diagrams and tables | No |
| Risk of damaging hand-written text | High | Zero: only autoblocks between anchors are touched | Low (comments only) |
| C4/UML | The LLM "draws" PlantUML - errors, inconsistencies between pages | Structurizr DSL - one model, views by the architect, identical everywhere | - |
| Approval | At page-version level | **MR into the model repository** (GitOps) + an "approve" link in the messenger | Comments on pages |
| LLM cost | High, on every merge | Low/zero: LLM only for descriptions and ambiguous targets | Medium |
| Weak spot | Hallucinations, non-determinism | Does not see **prose** outside autoblocks (e.g. "Orders does not call Billing directly") | Needs guardrails |

## Chosen solution: option 2 as the core + option 3 as an add-on

* **Structured** content (integration tables, cards, C4, sequence) - only deterministic generation from the model (option 2).
* **Prose** in hand-written sections - after publishing, an LLM audit (option 3) looks for stale statements and leaves **inline comments** with the quote and a suggested wording. It never rewrites anything.
* The LLM also helps on input: proposes `description` for new integrations and maps unresolved targets (`unresolved:*`) to model elements - only from the list of known elements, and all of it goes into an MR that a human reads.
* Without an LLM the system is fully functional (`LLM_PROVIDER=none`): descriptions are templated, the audit is skipped.
