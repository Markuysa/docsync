"""HTTP API for n8n (FastAPI). Routes under /api/* require X-DocSync-Token when DOCSYNC_API_TOKEN is set."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .config import settings
from .confluence import MockConfluence
from .notify import Notifier
from .pipeline import Pipeline, changed_elements
from .telegram import TelegramBot

log = logging.getLogger("docsync.api")
app = FastAPI(title="DocSync engine", version=__version__,
              description="Engine that keeps system-analysis documentation in sync with code. Called by n8n.")
_pipeline: Pipeline | None = None
_notifier: Notifier | None = None


def notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        s = settings()
        bot = None
        if s.telegram_bot_token and s.telegram_chat_id:
            bot = TelegramBot(s.telegram_bot_token, s.telegram_chat_id, s.telegram_allowed_users,
                              s.approve_internal_url or s.approve_url, pipeline, s.public_url)
            if s.telegram_polling:
                bot.start()
        _notifier = Notifier(s, bot)
    return _notifier


@app.on_event("startup")
def _startup() -> None:
    notifier()


def pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None or not settings().journal_path.exists():   # data/ was removed (demo reset) - recreate
        _pipeline = Pipeline()
    return _pipeline


def auth(x_docsync_token: str | None = Header(default=None)) -> None:
    token = settings().api_token
    if token and x_docsync_token != token:
        raise HTTPException(401, "bad X-DocSync-Token")


@app.exception_handler(Exception)
async def errors(_: Request, exc: Exception):
    log.exception("API error")
    code = 404 if isinstance(exc, KeyError) else 409 if isinstance(exc, ValueError) else 500
    return JSONResponse({"error": str(exc)}, status_code=code)


@app.get("/healthz")
def healthz() -> dict:
    s = settings()
    return {"ok": True, "version": __version__, "gitMode": s.git_mode, "confluenceMode": s.confluence_mode,
            "llm": s.llm_provider, "kroki": pipeline().kroki.healthy(), "dryRun": s.dry_run,
            "telegram": bool(s.telegram_bot_token and s.telegram_chat_id), "slack": bool(s.slack_webhook_url)}
@app.post("/api/v1/ingest", dependencies=[Depends(auth)])
def ingest(payload: dict[str, Any]) -> dict:
    """Accepts either a GitLab Push Hook (object_kind=push) or a simplified
    {project_path, before, after, project_id?, mr?}."""
    if payload.get("object_kind") == "push":
        project = payload.get("project") or {}
        ref = payload.get("ref", "")
        default = project.get("default_branch", settings().model_default_branch)
        if ref != f"refs/heads/{default}":
            return {"status": "IGNORED", "reason": f"push is not to {default}: {ref}"}
        args = {"project_path": project.get("path_with_namespace"), "before": payload.get("before", ""),
                "after": payload.get("after") or payload.get("checkout_sha"), "project_id": project.get("id", 0)}
    else:
        args = {k: payload[k] for k in ("project_path", "before", "after") if k in payload}
        args["project_id"] = payload.get("project_id", 0)
        args["mr"] = payload.get("mr")
    if not args.get("project_path") or not args.get("after"):
        raise HTTPException(400, "project_path and after (sha) are required")
    return pipeline().ingest(**args)
@app.get("/api/v1/runs", dependencies=[Depends(auth)])
def runs(status: str | None = None, limit: int = 50) -> list[dict]:
    return pipeline().journal.list_runs(status, limit)


@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(auth)])
def run(run_id: str) -> dict:
    r = pipeline().journal.get_run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.post("/api/v1/runs/{run_id}/approve", dependencies=[Depends(auth)])
@app.get("/api/v1/runs/{run_id}/approve", dependencies=[Depends(auth)])
def approve(run_id: str) -> dict:
    return pipeline().approve(run_id)


@app.post("/api/v1/runs/{run_id}/reject", dependencies=[Depends(auth)])
def reject(run_id: str) -> dict:
    return pipeline().reject(run_id)
@app.post("/api/v1/publish", dependencies=[Depends(auth)])
def publish(payload: dict[str, Any] | None = None) -> dict:
    """{changed?: [ids], run_id?, force?} or a GitLab Merge Request Hook (action=merge) / Push Hook of the model repository."""
    payload = payload or {}
    p = pipeline()
    changed = set(payload.get("changed") or []) or None
    run_id = payload.get("run_id")
    force = bool(payload.get("force", False))
    if payload.get("object_kind") == "merge_request":
        a = payload.get("object_attributes") or {}
        if a.get("action") != "merge":
            return {"status": "IGNORED", "reason": f"action={a.get('action')}"}
        run = p.mark_model_merged(a.get("source_branch", ""), a.get("merge_commit_sha") or "")
        if run:
            run_id = run["run_id"]
            changed = set((run.get("apply_report") or {}).get("touched", [])) or None
    elif payload.get("object_kind") == "push":
        before, after = payload.get("before", ""), payload.get("after", "")
        repo = p.host.model_checkout()
        changed = changed_elements(repo, before, after) if before and set(before) != {"0"} else None
        if changed is not None and not changed:
            return {"status": "IGNORED", "reason": "model/ did not change"}
    return p.publish(changed=changed, run_id=run_id, force=force)


@app.post("/api/v1/audit", dependencies=[Depends(auth)])
def audit(payload: dict[str, Any]) -> dict:
    return pipeline().audit(payload["run_id"], post_comments=payload.get("post_comments", True))


@app.post("/api/v1/confluence/bootstrap", dependencies=[Depends(auth)])
def bootstrap() -> list[dict]:
    return pipeline().bootstrap_pages()


@app.get("/api/v1/kpi", dependencies=[Depends(auth)])
def kpi() -> dict:
    return pipeline().journal.kpi()
@app.post("/api/v1/notify")
def notify(payload: dict[str, Any]) -> dict:
    """Notification fan-out: log + Slack incoming webhook (SLACK_WEBHOOK_URL) + Telegram (TELEGRAM_BOT_TOKEN/CHAT_ID).
    A `proposed` notification with runId gets Approve/Reject buttons in Telegram."""
    return {"ok": True, **notifier().send(payload)}


@app.get("/api/v1/notifications")
def notifications(limit: int = 20) -> list[dict]:
    f = settings().data_dir / "notifications.log"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()[-limit:]]
@app.get("/mock-confluence/", response_class=HTMLResponse)
def mock_index() -> str:
    cf = pipeline().confluence
    if not isinstance(cf, MockConfluence):
        return "<p>CONFLUENCE_MODE=cloud: pages live in the real Confluence.</p>"
    items = "".join(f'<li><a href="/mock-confluence/{p["id"]}">{p["title"]}</a> <small>v{p["version"]}</small></li>' for p in cf.list_pages())
    return f"<!doctype html><meta charset=utf-8><title>mock Confluence</title><h1>mock Confluence · space {cf.space_key}</h1><ul>{items or '<li>no pages - run docsync confluence bootstrap</li>'}</ul>"


@app.get("/mock-confluence/{page_id}", response_class=HTMLResponse)
def mock_page(page_id: str) -> str:
    cf = pipeline().confluence
    if not isinstance(cf, MockConfluence):
        raise HTTPException(404, "mock is disabled")
    return cf.render_html(page_id)
