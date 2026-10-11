"""Structurizr server integration: push the generated workspace with structurizr-cli and embed live
diagrams into Confluence pages through the iframe macro (DOCSYNC_C4_MODE=structurizr)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("docsync.structurizr")


class StructurizrServer:
    def __init__(self, url: str, api_url: str, workspace_id: str, api_key: str, api_secret: str,
                 admin_key: str = "", cli: str = "docker", iframe_height: int = 650):
        self.url = url.rstrip("/")                      # public URL (what readers' browsers can reach)
        self.api_url = (api_url or f"{self.url}/api").rstrip("/")   # URL the engine uses to push
        self.workspace_id = str(workspace_id or "")
        self.api_key = api_key
        self.api_secret = api_secret
        self.admin_key = admin_key
        self.cli = cli
        self.iframe_height = iframe_height

    @property
    def configured(self) -> bool:
        return bool(self.url and self.workspace_id and self.api_key and self.api_secret)
    def embed_url(self, view_key: str, iframe_id: str) -> str:
        return f"{self.url}/embed/{self.workspace_id}?diagram={quote(view_key)}&diagramSelector=false&iframe={quote(iframe_id)}"

    def diagram_url(self, view_key: str) -> str:
        return f"{self.url}/share/{self.workspace_id}/diagrams#{quote(view_key)}"   # public share link: no sign-in needed

    def iframe_macro(self, view_key: str, block_id: str) -> str:
        """Confluence Cloud iframe macro (storage format) with a link to the full-screen diagram below it."""
        iframe_id = f"docsync-{block_id}"
        return (f'<ac:structured-macro ac:name="iframe"><ac:parameter ac:name="src"><ri:url ri:value="{self.embed_url(view_key, iframe_id)}"/></ac:parameter>'
                f'<ac:parameter ac:name="width">100%</ac:parameter><ac:parameter ac:name="height">{self.iframe_height}</ac:parameter>'
                f'<ac:parameter ac:name="frameborder">hide</ac:parameter></ac:structured-macro>'
                f'<p><a href="{self.diagram_url(view_key)}">Открыть диаграмму «{view_key}» в Structurizr</a></p>')
    def create_workspace(self) -> dict:
        """Admin API (structurizr.apiKey on the server): creates a workspace, returns {id, apiKey, apiSecret, ...}."""
        if not self.admin_key:
            raise RuntimeError("STRUCTURIZR_ADMIN_KEY is required to create a workspace")
        r = httpx.post(f"{self.api_url}/workspace", headers={"X-Authorization": self.admin_key}, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Structurizr admin API -> {r.status_code}: {r.text[:300]}")
        return r.json()
    def push(self, workspace_dsl: Path) -> str:
        if not self.configured:
            raise RuntimeError("Structurizr is not configured (STRUCTURIZR_URL/WORKSPACE_ID/API_KEY/API_SECRET)")
        workspace_dsl = Path(workspace_dsl).resolve()
        args = ["push", "-id", self.workspace_id, "-key", self.api_key, "-secret", self.api_secret, "-url", self.api_url]
        if self.cli == "docker":
            api_url = self.api_url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
            cmd = ["docker", "run", "--rm", "-v", f"{workspace_dsl.parent}:/work", "structurizr/cli:2025.11.09",
                   *args[:-1], api_url, "-workspace", f"/work/{workspace_dsl.name}"]
        else:
            exe = self.cli if Path(self.cli).exists() else shutil.which(self.cli) or self.cli
            cmd = [exe, *args, "-workspace", str(workspace_dsl)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            raise RuntimeError(f"structurizr-cli push failed: {out[-800:]}")
        log.info("structurizr push ok: workspace %s -> %s", self.workspace_id, self.api_url)
        return out[-400:]


def make_structurizr(s) -> StructurizrServer | None:
    if s.c4_mode != "structurizr":
        return None
    return StructurizrServer(s.structurizr_url, s.structurizr_api_url, s.structurizr_workspace_id, s.structurizr_api_key,
                             s.structurizr_api_secret, s.structurizr_admin_key, s.structurizr_cli, s.c4_iframe_height)
