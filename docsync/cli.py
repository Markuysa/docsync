"""Engine CLI: `docsync <command>`. All commands read settings from the environment (.env)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    p = Path(os.environ.get("DOCSYNC_ENV_FILE", ".env"))
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    logging.basicConfig(level=os.environ.get("DOCSYNC_LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="docsync", description="DocSync - keeps system-analysis documentation in sync with code")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="ChangeSet from two revisions of a service repository")
    p.add_argument("--repo", default="."); p.add_argument("--base", required=True); p.add_argument("--head", required=True)
    p.add_argument("--project-path", required=True); p.add_argument("--out")

    p = sub.add_parser("validate", help="validate the model (schema + referential integrity + registry)")
    p.add_argument("--model", help="path to architecture-model (default: from settings)")

    p = sub.add_parser("generate", help="generate DSL/PlantUML/autoblocks/manifest")
    p.add_argument("--model"); p.add_argument("--out", default="build"); p.add_argument("--changed", help="comma-separated element IDs")

    p = sub.add_parser("render", help="render manifest diagrams through Kroki")
    p.add_argument("--out", default="build")

    p = sub.add_parser("ingest", help="service changes -> ChangeSet -> model branch/MR")
    p.add_argument("--project-path", required=True); p.add_argument("--before", default=""); p.add_argument("--after", required=True)

    p = sub.add_parser("approve", help="approve a run: merge + publish"); p.add_argument("run_id")
    p = sub.add_parser("reject", help="reject a run"); p.add_argument("run_id")
    p = sub.add_parser("publish", help="publish the model main to Confluence"); p.add_argument("--changed")
    p.add_argument("--force", action="store_true", help="ignore manual-edit conflicts (take ownership of the blocks again)")
    p = sub.add_parser("audit", help="LLM audit of hand-written sections for a run"); p.add_argument("run_id"); p.add_argument("--no-comments", action="store_true")
    p = sub.add_parser("runs", help="list runs"); p.add_argument("--status")
    p = sub.add_parser("run", help="run details"); p.add_argument("run_id")

    p = sub.add_parser("confluence", help="Confluence operations: bootstrap pages / check links")
    p.add_argument("action", choices=["bootstrap", "check"])

    p = sub.add_parser("structurizr", help="Structurizr server: init (create workspace via admin API) | push (push current model)")
    p.add_argument("action", choices=["init", "push"])

    p = sub.add_parser("serve", help="start the HTTP API for n8n")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8088)

    p = sub.add_parser("demo", help="local demo: seed | run | reset | push")
    p.add_argument("action", choices=["seed", "run", "reset", "push"])
    p.add_argument("--step", choices=["grpc", "kafka"], default="grpc", help="push: which commit to simulate")
    p.add_argument("--n8n-url", default=os.environ.get("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/docsync/gitlab"))

    a = ap.parse_args(argv)

    if a.cmd == "extract":
        from .extractor import extract
        cs = extract(a.repo, a.base, a.head, a.project_path)
        if a.out:
            Path(a.out).write_text(json.dumps(cs, ensure_ascii=False, indent=2), encoding="utf-8")
        _print(cs)
        return 0

    if a.cmd in ("validate", "generate"):
        from .config import settings
        from .model import Model
        root = Path(a.model) if a.model else settings().model_repo_path
        model = Model(root)
        if a.cmd == "validate":
            errors = model.validate()
            for e in errors:
                print("ERROR", e)
            print(f"validate: {len(errors)} errors in {root}")
            return 1 if errors else 0
        from .generate import generate
        s = settings()
        m = generate(model, Path(a.out), set(a.changed.split(",")) if a.changed else None,
                     diagram_mode=s.diagram_mode, macro_name=s.plantuml_macro_name)
        print(f"generate: {sum(len(p['blocks']) for p in m['pages'])} blocks on {len(m['pages'])} pages -> {a.out}/")
        return 0

    if a.cmd == "render":
        from .config import settings
        from .render import Kroki, render_manifest
        out = Path(a.out)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        done = render_manifest(manifest, out, Kroki(settings().kroki_url))
        print("rendered:", ", ".join(done) or "nothing to render")
        return 0

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("docsync.api:app", host=a.host, port=a.port, log_level="info")
        return 0

    if a.cmd == "demo":
        from . import demo
        return demo.main(a.action, a.step, a.n8n_url)

    from .model import Model
    from .pipeline import Pipeline
    pl = Pipeline()
    if a.cmd == "ingest":
        _print(pl.ingest(a.project_path, a.before, a.after))
    elif a.cmd == "approve":
        _print(pl.approve(a.run_id))
    elif a.cmd == "reject":
        _print(pl.reject(a.run_id))
    elif a.cmd == "publish":
        _print(pl.publish(set(a.changed.split(",")) if a.changed else None, force=a.force))
    elif a.cmd == "audit":
        _print(pl.audit(a.run_id, post_comments=not a.no_comments))
    elif a.cmd == "runs":
        _print(pl.journal.list_runs(a.status))
    elif a.cmd == "run":
        _print(pl.journal.get_run(a.run_id))
    elif a.cmd == "structurizr":
        from .structurizr import StructurizrServer
        st = pl.structurizr or StructurizrServer(pl.s.structurizr_url, pl.s.structurizr_api_url, pl.s.structurizr_workspace_id,
                                                 pl.s.structurizr_api_key, pl.s.structurizr_api_secret, pl.s.structurizr_admin_key, pl.s.structurizr_cli)
        if a.action == "init":
            ws = st.create_workspace()
            print("Workspace created. Put these into .env and set DOCSYNC_C4_MODE=structurizr:")
            print(f"STRUCTURIZR_WORKSPACE_ID={ws.get('id')}\nSTRUCTURIZR_API_KEY={ws.get('apiKey')}\nSTRUCTURIZR_API_SECRET={ws.get('apiSecret')}")
        else:
            from .generate import generate
            model = Model(pl.host.model_checkout())
            out = pl.s.build_dir / "structurizr-push"
            generate(model, out, sha=pl.host.model_head(), structurizr=st)
            print(st.push(out / "workspace.dsl"))
    elif a.cmd == "confluence":
        if a.action == "bootstrap":
            _print(pl.bootstrap_pages())
        else:
            from .model import Model
            cf = pl.confluence
            print("Confluence:", type(cf).__name__, "| space:", pl.s.confluence_space)
            for pg in Model(pl.host.model_checkout()).registry["pages"]:
                status = "no pageId - run bootstrap"
                if pg.get("pageId"):
                    try:
                        cur = cf.get_page(pg["pageId"])
                        status = f"ok v{cur['version']} {cf.page_url(pg['pageId'])}"
                    except Exception as e:
                        status = f"error: {e}"
                print(f"  {pg['title']}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
