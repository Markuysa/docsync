"""Diagram rendering through Kroki: Structurizr DSL (view-key) and PlantUML -> SVG."""
from __future__ import annotations

import json
from pathlib import Path

import httpx


class Kroki:
    def __init__(self, base_url: str, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def render(self, kind: str, source: str, options: dict | None = None, fmt: str = "svg") -> bytes:
        payload = {"diagram_source": source, "diagram_type": kind, "output_format": fmt}
        if options:
            payload["diagram_options"] = options
        r = httpx.post(f"{self.base_url}/", content=json.dumps(payload),
                       headers={"Content-Type": "application/json"}, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Kroki {kind} -> {r.status_code}: {r.text[:500]}")
        return r.content

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


def render_manifest(manifest: dict, build_dir: Path, kroki: Kroki) -> list[str]:
    """Renders every diagram from the manifest into build/diagrams/*.svg. Returns file names."""
    diag_dir = build_dir / "diagrams"
    workspace = (build_dir / "workspace.dsl").read_text(encoding="utf-8")
    done: list[str] = []
    for page in manifest["pages"]:
        for b in page["blocks"]:
            d = b.get("diagram")
            if not d or d.get("embedded") or d["name"] in done:
                continue
            if d["kind"] == "structurizr":
                svg = kroki.render("structurizr", workspace, {"view-key": d["view"]})
            else:
                svg = kroki.render("plantuml", (diag_dir / d["name"].replace(".svg", ".puml")).read_text(encoding="utf-8"))
            (diag_dir / d["name"]).write_bytes(svg)
            done.append(d["name"])
    return done
