"""Scenarios: ingest (service change -> model MR), approve (merge -> publish), publish (model -> Confluence),
audit (LLM comments on hand-written text), bootstrap (create pages from the registry)."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import autoblocks
from .config import Settings, settings
from .confluence import make_client
from .extractor import extract
from .generate import generate, placeholder_block
from .githost import make_host
from .journal import Journal
from .llm import LLM
from .model import Model
from .render import Kroki, render_manifest
from .structurizr import make_structurizr

log = logging.getLogger("docsync")


class Pipeline:
    def __init__(self, s: Settings | None = None):
        self.s = s or settings()
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.s.journal_path)
        self.host = make_host(self.s)
        self.kroki = Kroki(self.s.kroki_url)
        self.llm = LLM(self.s.llm_provider, self.s.llm_model, self.s.llm_api_key, self.s.llm_base_url)
        self.structurizr = make_structurizr(self.s)
        self._confluence = None

    @property
    def confluence(self):
        if self._confluence is None:
            self._confluence = make_client(self.s)
        return self._confluence
    def ingest(self, project_path: str, before: str, after: str, project_id: int = 0, mr: dict | None = None) -> dict:
        repo = self.host.service_repo(project_path, after)
        cs = extract(str(repo), before, after, project_path, project_id)
        if mr is None and hasattr(self.host, "merge_request_for_commit"):
            m = self.host.merge_request_for_commit(project_path, after)
            mr = {"iid": m["iid"], "title": m["title"], "url": m["web_url"], "author": (m.get("author") or {}).get("username"),
                  "labels": m.get("labels", [])} if m else None
        cs["mr"] = mr
        if mr and "no-docs" in (mr.get("labels") or []):
            cs["skipReason"] = "no_docs_label"

        run = self.journal.create_run(cs, run_key=f"{project_path}:{after}")
        if run is None:
            return {"status": "DUPLICATE", "runKey": f"{project_path}:{after}"}
        if cs.get("skipReason"):
            return run
        try:
            return self._propose(run, cs)
        except Exception as e:
            log.exception("ingest failed")
            try:
                self.host.discard_working_tree()
            except Exception:
                pass
            return self.journal.update_run(run["run_id"], status="FAILED", error=str(e))

    def _propose(self, run: dict, cs: dict) -> dict:
        run_id = run["run_id"]
        cs["runId"] = run_id
        model_repo = self.host.model_checkout()
        model = Model(model_repo)
        known = sorted(set(model["services"]) | set(model["external"]))
        cs = self.llm.enrich(cs, known, model.name_of)

        report = model.apply_changeset(cs)
        errors = model.validate()
        (model_repo / "changesets").mkdir(exist_ok=True)
        (model_repo / "changesets" / f"{run_id}.json").write_text(json.dumps(cs, ensure_ascii=False, indent=2), encoding="utf-8")

        touched = set(report["touched"])
        preview = generate(model, self.s.build_dir / f"preview-{run_id[:8]}", changed=touched or None, sha="preview",
                           diagram_mode=self.s.diagram_mode, macro_name=self.s.plantuml_macro_name,
                           structurizr=self.structurizr) if touched else {"pages": []}
        preview_pages = [{"key": p["key"], "pageId": p.get("pageId"), "title": p["title"],
                          "blocks": [b["blockId"] for b in p["blocks"]]} for p in preview["pages"]]

        branch = f"docsync/{cs['service']}-{run_id[:8]}"
        title = f"DocSync: {cs['service']} - {(cs.get('mr') or {}).get('title') or cs['mergeSha'][:8]}"
        description = self._mr_description(cs, report, errors, preview_pages)
        mr = self.host.propose(branch, f"docsync: changeset {run_id[:8]} ({cs['service']})", title, description)
        status = "NEEDS_REVIEW" if (errors or report["needsReview"]) else "PROPOSED"
        self.host.note_source_mr(cs["project"]["path"], (cs.get("mr") or {}).get("iid"),
                                 f"DocSync proposed a documentation update: {mr['url']}")
        return self.journal.update_run(run_id, status=status, branch=branch, model_mr_iid=mr.get("iid"), model_mr_url=mr["url"],
                                       changeset=cs, apply_report={**report, "validationErrors": errors},
                                       preview={"pages": preview_pages, "approveUrl": self.s.approve_url.format(runId=run_id)},
                                       proposed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @staticmethod
    def _mr_description(cs: dict, report: dict, errors: list[str], pages: list[dict]) -> str:
        mr = cs.get("mr") or {}
        src = f"[{cs['project']['path']}!{mr['iid']}]({mr['url']}) - {mr.get('title', '')}" if mr else f"`{cs['mergeSha'][:12]}`"
        lines = [f"### DocSync: changes from {src}", "",
                 "| Type | Link | Protocol | Operations | Source | Confidence | Evidence |", "|---|---|---|---|---|---|---|"]
        for e in cs["events"]:
            ev = "<br>".join(f"`{v['path']}:{v.get('lines', '')}`" for v in e.get("evidence", []))
            lines.append(f"| {e['type']} | {e['from']} → {e.get('to', '')} | {e.get('protocol', '')} | "
                         f"{', '.join(e.get('operations', []))} | {e['source']} | {e['confidence']} | {ev} |")
        if report["needsReview"]:
            lines += ["", "**Needs manual handling:**"] + [f"- {x['eventId']}: {x['reason']}" for x in report["needsReview"]]
        if report.get("info"):
            lines += ["", "**For information:**"] + [f"- {x['eventId']}: {x['reason']}" for x in report["info"]]
        if errors:
            lines += ["", "**Model validation errors:**"] + [f"- {x}" for x in errors]
        lines += ["", "**Pages that will be updated:**"] + ([f"- {p['title']}: {', '.join(p['blocks'])}" for p in pages] or ["- (no registered blocks)"])
        lines += ["", f"run: `{cs.get('runId', '')}`"]
        return "\n".join(lines)
    def approve(self, run_id: str) -> dict:
        run = self.journal.get_run(run_id)
        if not run:
            raise KeyError(f"run {run_id} not found")
        if run["status"] in ("APPROVED", "PUBLISHED", "PARTIALLY_PUBLISHED"):
            return {"run": run, "publish": None, "status": "ALREADY_APPROVED"}      # idempotent: repeated clicks are harmless
        if run["status"] not in ("PROPOSED", "NEEDS_REVIEW"):
            raise ValueError(f"run {run_id} is in status {run['status']}, approval is not possible")
        sha = self.host.merge(run["branch"])
        run = self.journal.update_run(run_id, status="APPROVED", model_merge_sha=sha,
                                      approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        touched = set((run.get("apply_report") or {}).get("touched", []))
        report = self.publish(changed=touched or None, run_id=run_id)
        return {"run": self.journal.get_run(run_id), "publish": report, "status": "APPROVED"}

    def reject(self, run_id: str) -> dict:
        run = self.journal.get_run(run_id)
        if not run:
            raise KeyError(f"run {run_id} not found")
        return self.journal.update_run(run_id, status="REJECTED")

    def mark_model_merged(self, branch: str, merge_sha: str) -> dict | None:
        """Called from the GitLab webhook when a model MR was merged in the UI (without approve via the API)."""
        run = self.journal.run_by_branch(branch)
        if run and run["status"] in ("PROPOSED", "NEEDS_REVIEW"):
            self.journal.update_run(run["run_id"], status="APPROVED", model_merge_sha=merge_sha,
                                    approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return run
    def publish(self, changed: set[str] | None = None, run_id: str | None = None, force: bool = False) -> dict:
        """force=True: ignore manual-edit conflicts and take ownership of the blocks again."""
        model_repo = self.host.model_checkout()
        sha = self.host.model_head()
        model = Model(model_repo)
        errors = model.validate()
        if errors:
            raise RuntimeError("Model is invalid, publishing stopped:\n" + "\n".join(errors))
        build_dir = self.s.build_dir / sha[:12]
        manifest = generate(model, build_dir, changed=changed, sha=sha,
                            diagram_mode=self.s.diagram_mode, macro_name=self.s.plantuml_macro_name, structurizr=self.structurizr)
        if self.structurizr is not None and not self.s.dry_run:
            # C4 mode "structurizr": the whole workspace goes to the server first, pages then embed live diagrams
            self.structurizr.push(build_dir / "workspace.dsl")
        if not self.kroki.healthy():
            raise RuntimeError(f"Kroki is unavailable at {self.s.kroki_url}")
        render_manifest(manifest, build_dir, self.kroki)

        report = {"modelSha": sha, "runId": run_id, "dryRun": self.s.dry_run, "buildDir": str(build_dir),
                  "c4Mode": manifest.get("c4Mode", "svg"), "pages": [], "conflicts": []}
        for page in manifest["pages"]:
            report["pages"].append(self._publish_page(page, build_dir, sha, run_id, force))
        report["conflicts"] = [c for p in report["pages"] for c in p.get("conflicts", [])]
        if run_id:
            status = "PARTIALLY_PUBLISHED" if report["conflicts"] else "PUBLISHED"
            self.journal.update_run(run_id, status=status, published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return report

    def _publish_page(self, page: dict, build_dir: Path, sha: str, run_id: str | None, force: bool = False) -> dict:
        out = {"key": page["key"], "pageId": page.get("pageId"), "title": page["title"], "blocks": [], "conflicts": []}
        if not page.get("pageId"):
            out["status"] = "NO_PAGE_ID"
            out["hint"] = "page is not linked: run `docsync confluence bootstrap`"
            return out
        cf = self.confluence
        try:
            current = cf.get_page(page["pageId"])
        except Exception as e:
            out.update(status="FAILED", error=str(e))
            return out
        blocks = [{"blockId": b["blockId"], "xhtml": (build_dir / b["file"]).read_text(encoding="utf-8")} for b in page["blocks"]]
        result = autoblocks.patch_page(current["body"], blocks, {} if force else self.journal.last_hashes(page["pageId"]))
        out["blocks"] = result.results
        out["conflicts"] = [{"pageId": page["pageId"], "title": page["title"], **c} for c in result.conflicts]
        out["url"] = cf.page_url(page["pageId"])
        dry = self.s.dry_run or page.get("dryRun")
        version_after = None
        if result.changed and not dry:
            published = {r["blockId"] for r in result.results if r["status"] == "PUBLISHED"}
            for b in page["blocks"]:
                d = b.get("diagram")
                if d and not d.get("embedded") and b["blockId"] in published:
                    cf.upload_attachment(page["pageId"], d["name"], (build_dir / "diagrams" / d["name"]).read_bytes(),
                                         comment=f"DocSync architecture-model@{sha[:12]}")
            upd = cf.update_page(page["pageId"], current["title"], result.body, current["version"],
                                 f"DocSync: architecture-model@{sha[:12]}")
            version_after = upd["version"]
            out["status"] = "PUBLISHED"
            # read-after-write: Confluence rewrites storage format (attribute order, macro ids, entities),
            # so the hash used for manual-edit detection must be computed from what Confluence actually stored
            stored = cf.get_page(page["pageId"])["body"]
            for r in result.results:
                if r["status"] == "PUBLISHED":
                    pos = autoblocks.find_block(stored, r["blockId"])
                    if pos:
                        r["hashAfter"] = autoblocks.sha256(autoblocks.normalize(stored[pos[0]:pos[1]]))
        elif result.changed and dry:
            out["status"] = "DRY_RUN"
            for r in result.results:
                if r["status"] == "PUBLISHED":
                    r["status"] = "SKIPPED_DRY_RUN"
        else:
            out["status"] = "UNCHANGED" if not result.conflicts else "CONFLICT"
        self.journal.record_publication(run_id, sha, page["pageId"], result.results, version_after)
        return out
    def audit(self, run_id: str, post_comments: bool = True) -> dict:
        run = self.journal.get_run(run_id)
        if not run:
            raise KeyError(f"run {run_id} not found")
        if not self.llm.enabled:
            return {"status": "SKIPPED", "reason": "LLM_PROVIDER=none"}
        post_comments = post_comments and not self.s.dry_run   # dry-run: findings are reported, comments are not posted
        cs = run.get("changeset") or {}
        applied = {a["eventId"] for a in (run.get("apply_report") or {}).get("applied", [])}
        changes = [{"type": e["type"], "from": e["from"], "to": e.get("to"), "protocol": e.get("protocol"),
                    "operations": e.get("operations", []), "description": e.get("description", "")}
                   for e in cs.get("events", []) if e["eventId"] in applied]
        if not changes:
            return {"status": "SKIPPED", "reason": "no accepted changes"}
        model = Model(self.host.model_checkout())
        registry_ids = {p.get("pageId") for p in model.registry["pages"]}
        terms = {t for c in changes for t in (c["from"], c["to"] or "") if t and not t.startswith("topic:")}
        terms |= {model.name_of(t) for t in list(terms)}
        # registered pages whose scope touches the changed elements are always checked (CQL index may lag);
        # CQL search adds unregistered pages that mention the elements
        candidates: dict[str, dict] = {}
        changed_ids = {c["from"] for c in changes} | {c["to"] for c in changes if c["to"]}
        for pg in model.registry["pages"]:
            if pg.get("pageId") and any(model.scope_elements(b["scope"]) & (changed_ids | model.neighbours(changed_ids))
                                        for b in pg["blocks"]):
                candidates[pg["pageId"]] = {"id": pg["pageId"], "title": pg["title"]}
        for term in terms:
            try:
                for p in self.confluence.search(term, limit=5):
                    candidates.setdefault(p["id"], p)
            except Exception as e:  # search is best-effort
                log.warning("confluence search failed for %r: %s", term, e)
        pages = []
        for pid, p in list(candidates.items())[:10]:
            body = self.confluence.get_page(pid)["body"]
            text = self._manual_text(body)
            if text.strip():
                pages.append({"pageId": pid, "title": p["title"], "text": text[:8000],
                              "registered": pid in registry_ids})
        findings = self.llm.audit(changes, pages)
        texts = {p["pageId"]: p["text"] for p in pages}
        out = []
        for f in findings:
            quote = re.sub(r"\s+", " ", f["quote"]).strip()
            ok = quote and quote in re.sub(r"\s+", " ", texts.get(f["pageId"], "")) and float(f["confidence"]) >= 0.7
            dup = self.journal.finding_exists(f["pageId"], quote)
            resolution = "open" if (ok and not dup) else ("duplicate" if dup else "report_only")
            fid = self.journal.record_finding(run_id, {**f, "quote": quote}, resolution)
            item = {**f, "quote": quote, "resolution": resolution}
            if resolution == "open" and post_comments:
                body = (f"<p><strong>DocSync:</strong> {f['reason']}</p><p>Предлагаемый текст: {f['suggestion']}</p>"
                        f"<p><sub>run {run_id[:8]}</sub></p>")
                try:
                    c = self.confluence.add_inline_comment(f["pageId"], quote, body)
                    self.journal.set_finding_comment(fid, str(c.get("id", "")))
                    item["commentId"] = c.get("id")
                except Exception as e:
                    item["commentError"] = str(e)
            out.append(item)
        return {"status": "DONE", "pagesChecked": [p["title"] for p in pages], "findings": out}

    @staticmethod
    def _manual_text(storage: str) -> str:
        """Page text without autoblocks and markup."""
        for bid in re.findall(r"docsync:auto:([\w-]+):start", storage):
            pos = autoblocks.find_block(storage, bid)
            if pos:
                storage = storage[: pos[0]] + " " + storage[pos[1]:]
        text = re.sub(r"<[^>]+>", " ", storage)
        return re.sub(r"\s+", " ", text).strip()
    def bootstrap_pages(self) -> list[dict]:
        """Creates/links Confluence pages from docs-registry and stores pageId in the registry."""
        model = Model(self.host.model_checkout())
        registry = model.registry
        cf = self.confluence
        parent = registry.get("parentPageId") or self.s.confluence_parent_page or None
        out = []
        for page in registry["pages"]:
            existing = None
            if page.get("pageId"):
                try:
                    existing = cf.get_page(page["pageId"])
                except Exception:
                    existing = None
            if not existing:
                found = cf.find_page(page["title"])
                existing = cf.get_page(found["id"]) if found else None
            if existing:
                body, added = existing["body"], []
                for b in page["blocks"]:
                    if not autoblocks.find_block(body, b["blockId"]):
                        body += f'<h2>{b.get("heading", b["blockId"])}</h2>' + placeholder_block(b["blockId"])
                        added.append(b["blockId"])
                if added:
                    cf.update_page(existing["id"], existing["title"], body, existing["version"], "DocSync: autoblocks added")
                page["pageId"] = existing["id"]
                out.append({"key": page["key"], "pageId": existing["id"], "status": "LINKED", "addedBlocks": added, "url": cf.page_url(existing["id"])})
            else:
                body = page.get("intro", "<h2>Назначение</h2><p>Ручной раздел: заполняет аналитик.</p>")
                for b in page["blocks"]:
                    body += f'<h2>{b.get("heading", b["blockId"])}</h2>' + placeholder_block(b["blockId"])
                body += page.get("outro", "<h2>Нефункциональные требования</h2><p>Ручной раздел: DocSync его не меняет, но может оставить inline-комментарий.</p>")
                created = cf.create_page(page["title"], body, parent)
                page["pageId"] = created["id"]
                out.append({"key": page["key"], "pageId": created["id"], "status": "CREATED", "url": cf.page_url(created["id"])})
        model.save_registry(registry)
        from .githost import git
        git(model.root, "add", "docs-registry.yaml")
        if git(model.root, "status", "--porcelain", "docs-registry.yaml"):
            git(model.root, "commit", "-q", "-m", "docsync: bootstrap pageId")
            if self.s.git_mode == "gitlab":
                git(model.root, "push", "-q", "origin", self.s.model_default_branch)
        return out


def changed_elements(repo: Path, base: str, head: str) -> set[str]:
    """IDs of model elements changed between revisions (for publishing after a manual merge into the model)."""
    import subprocess
    import yaml

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout

    ids: set[str] = set()
    for line in git("diff", "--name-status", base, head, "--", "model/").splitlines():
        status, path = line.split("\t")[0], line.split("\t")[-1]
        if not path.endswith(".yaml") or "/generated/" in path:
            continue
        try:
            data = yaml.safe_load(git("show", f"{base if status.startswith('D') else head}:{path}")) or {}
        except subprocess.CalledProcessError:
            data = {}
        if "integrations/" in path:
            ids |= {data.get("from", ""), data.get("to", "")}
        else:
            ids.add(data.get("id") or Path(path).stem)
    return {i for i in ids if i}
