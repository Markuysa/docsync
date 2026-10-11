"""Confluence clients with a shared interface: Cloud (REST API v2, v1 for attachments) and a file-based mock."""
from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path

import httpx


class ConfluenceClient:
    def get_page(self, page_id: str) -> dict: ...            # {id, title, version, body}
    def update_page(self, page_id: str, title: str, body: str, version: int, message: str) -> dict: ...
    def create_page(self, title: str, body: str, parent_id: str | None = None) -> dict: ...
    def find_page(self, title: str) -> dict | None: ...
    def upload_attachment(self, page_id: str, filename: str, data: bytes, comment: str = "") -> dict: ...
    def add_inline_comment(self, page_id: str, quote: str, body_xhtml: str) -> dict: ...
    def search(self, text: str, limit: int = 10) -> list[dict]: ...
    def page_url(self, page_id: str) -> str: ...
class CloudConfluence(ConfluenceClient):
    def __init__(self, base_url: str, email: str, token: str, space_key: str):
        self.base = base_url.rstrip("/")
        if not self.base.endswith("/wiki"):
            self.base += "/wiki"
        self.space_key = space_key
        self.http = httpx.Client(base_url=self.base, auth=(email, token), timeout=60,
                                 headers={"Accept": "application/json"})
        self._space_id: str | None = None

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 300:
            raise RuntimeError(f"Confluence {r.request.method} {r.request.url} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.content else {}

    @property
    def space_id(self) -> str:
        if not self._space_id:
            data = self._check(self.http.get("/api/v2/spaces", params={"keys": self.space_key}))
            if not data.get("results"):
                raise RuntimeError(f"Space {self.space_key} not found")
            self._space_id = data["results"][0]["id"]
        return self._space_id

    def get_page(self, page_id: str) -> dict:
        p = self._check(self.http.get(f"/api/v2/pages/{page_id}", params={"body-format": "storage"}))
        return {"id": p["id"], "title": p["title"], "version": p["version"]["number"], "body": p["body"]["storage"]["value"]}

    def update_page(self, page_id: str, title: str, body: str, version: int, message: str) -> dict:
        payload = {"id": page_id, "status": "current", "title": title,
                   "body": {"representation": "storage", "value": body},
                   "version": {"number": version + 1, "message": message}}
        p = self._check(self.http.put(f"/api/v2/pages/{page_id}", json=payload))
        return {"id": p["id"], "version": p["version"]["number"]}

    def create_page(self, title: str, body: str, parent_id: str | None = None) -> dict:
        payload = {"spaceId": self.space_id, "status": "current", "title": title,
                   "body": {"representation": "storage", "value": body}}
        if parent_id:
            payload["parentId"] = parent_id
        p = self._check(self.http.post("/api/v2/pages", json=payload))
        self.set_full_width(p["id"])
        return {"id": p["id"], "title": p["title"], "version": p["version"]["number"]}

    def set_full_width(self, page_id: str) -> None:
        """Best effort: generated tables and diagrams need the full page width (content-appearance properties, v1 API)."""
        for key in ("content-appearance-published", "content-appearance-draft"):
            try:
                r = self.http.get(f"/rest/api/content/{page_id}/property/{key}")
                if r.status_code == 200:
                    cur = r.json()
                    self.http.put(f"/rest/api/content/{page_id}/property/{key}",
                                  json={"value": "full-width", "version": {"number": cur["version"]["number"] + 1}})
                else:
                    self.http.post(f"/rest/api/content/{page_id}/property", json={"key": key, "value": "full-width"})
            except httpx.HTTPError:
                pass

    def find_page(self, title: str) -> dict | None:
        data = self._check(self.http.get("/api/v2/pages", params={"space-id": self.space_id, "title": title, "limit": 1}))
        res = data.get("results") or []
        return {"id": res[0]["id"], "title": res[0]["title"]} if res else None

    def upload_attachment(self, page_id: str, filename: str, data: bytes, comment: str = "") -> dict:
        # v2 cannot upload attachments - use v1 (PUT = create or new version)
        r = self.http.put(f"/rest/api/content/{page_id}/child/attachment",
                          headers={"X-Atlassian-Token": "nocheck"},
                          files={"file": (filename, data, "image/svg+xml")},
                          data={"minorEdit": "true", "comment": comment})
        return self._check(r)

    def add_inline_comment(self, page_id: str, quote: str, body_xhtml: str) -> dict:
        payload = {"pageId": page_id, "body": {"representation": "storage", "value": body_xhtml},
                   "inlineCommentProperties": {"textSelection": quote, "textSelectionMatchCount": 1, "textSelectionMatchIndex": 0}}
        return self._check(self.http.post("/api/v2/inline-comments", json=payload))

    def search(self, text: str, limit: int = 10) -> list[dict]:
        cql = f'space = "{self.space_key}" AND type = page AND text ~ "{text}"'
        data = self._check(self.http.get("/rest/api/search", params={"cql": cql, "limit": limit}))
        return [{"id": r["content"]["id"], "title": r["content"]["title"]} for r in data.get("results", []) if r.get("content")]

    def page_url(self, page_id: str) -> str:
        return f"{self.base}/spaces/{self.space_key}/pages/{page_id}"
class MockConfluence(ConfluenceClient):
    """Pages are files data/confluence-mock/pages/<id>.json, attachments - attachments/<id>/<file>.
    Used for the local demo and tests; the HTML viewer is served by the engine API."""

    def __init__(self, root: Path, space_key: str = "SA", public_url: str = ""):
        self.root = Path(root)
        self.space_key = space_key
        self.public_url = public_url
        (self.root / "pages").mkdir(parents=True, exist_ok=True)
        (self.root / "attachments").mkdir(parents=True, exist_ok=True)

    def _path(self, page_id: str) -> Path:
        return self.root / "pages" / f"{page_id}.json"

    def _next_id(self) -> str:
        ids = [int(p.stem) for p in (self.root / "pages").glob("*.json") if p.stem.isdigit()]
        return str((max(ids) if ids else 100000) + 1)

    def get_page(self, page_id: str) -> dict:
        p = self._path(page_id)
        if not p.exists():
            raise RuntimeError(f"mock: page {page_id} not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def _write(self, page: dict, message: str = "") -> None:
        (self.root / "pages").mkdir(parents=True, exist_ok=True)
        hist = self.root / "history" / page["id"]
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f'v{page["version"]}.json').write_text(json.dumps({**page, "message": message}, ensure_ascii=False, indent=2), encoding="utf-8")
        self._path(page["id"]).write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_page(self, page_id: str, title: str, body: str, version: int, message: str) -> dict:
        page = self.get_page(page_id)
        if page["version"] != version:
            raise RuntimeError(f"mock: version conflict on page {page_id}: {page['version']} != {version}")
        page.update({"title": title, "body": body, "version": version + 1})
        self._write(page, message)
        return {"id": page_id, "version": page["version"]}

    def create_page(self, title: str, body: str, parent_id: str | None = None) -> dict:
        page = {"id": self._next_id(), "title": title, "version": 1, "body": body, "parentId": parent_id}
        self._write(page, "created")
        return {"id": page["id"], "title": title, "version": 1}

    def find_page(self, title: str) -> dict | None:
        for p in (self.root / "pages").glob("*.json"):
            page = json.loads(p.read_text(encoding="utf-8"))
            if page["title"] == title:
                return {"id": page["id"], "title": title}
        return None

    def upload_attachment(self, page_id: str, filename: str, data: bytes, comment: str = "") -> dict:
        d = self.root / "attachments" / page_id
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_bytes(data)
        return {"id": f"att-{page_id}-{filename}", "title": filename}

    def add_inline_comment(self, page_id: str, quote: str, body_xhtml: str) -> dict:
        d = self.root / "comments"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{page_id}.json"
        items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        items.append({"id": str(len(items) + 1), "quote": quote, "body": body_xhtml})
        f.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"id": items[-1]["id"]}

    def search(self, text: str, limit: int = 10) -> list[dict]:
        out = []
        for p in (self.root / "pages").glob("*.json"):
            page = json.loads(p.read_text(encoding="utf-8"))
            if text.lower() in (page["title"] + page["body"]).lower():
                out.append({"id": page["id"], "title": page["title"]})
        return out[:limit]

    def page_url(self, page_id: str) -> str:
        return f"{self.public_url}/mock-confluence/{page_id}"

    def list_pages(self) -> list[dict]:
        return sorted((json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "pages").glob("*.json")), key=lambda x: x["title"])

    def render_html(self, page_id: str) -> str:
        """Rough rendering of storage format as HTML - demo only."""
        page = self.get_page(page_id)
        body = page["body"]
        att = self.root / "attachments" / page_id

        def img(m: re.Match) -> str:
            f = att / m.group(1)
            if not f.exists():
                return f'<div class="missing">[attachment {m.group(1)} not uploaded yet]</div>'
            b64 = base64.b64encode(f.read_bytes()).decode()
            return f'<img src="data:image/svg+xml;base64,{b64}" style="max-width:100%"/>'

        body = re.sub(r'<ac:image[^>]*><ri:attachment ri:filename="([^"]+)"/></ac:image>', img, body)
        body = re.sub(r'<ac:structured-macro ac:name="anchor"><ac:parameter ac:name="">(docsync:auto:[\w-]+:(start|end))</ac:parameter></ac:structured-macro>',
                      r'<span class="anchor"> \1</span>', body)
        body = re.sub(r'<ac:structured-macro ac:name="panel"><ac:parameter ac:name="title">([^<]*)</ac:parameter><ac:rich-text-body>',
                      r'<div class="panel"><div class="panel-title">\1</div><div class="panel-body">', body)
        body = re.sub(r'<ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">(\w+)</ac:parameter><ac:parameter ac:name="title">([^<]*)</ac:parameter></ac:structured-macro>',
                      r'<span class="status \1">\2</span>', body)
        body = re.sub(r'<ac:structured-macro ac:name="(\w+)"><ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body></ac:structured-macro>',
                      lambda m: f'<pre class="macro">[macro {m.group(1)}]\n{html.escape(m.group(2))}</pre>', body, flags=re.S)
        body = body.replace("</ac:rich-text-body></ac:structured-macro>", "</div></div>")
        comments = self.root / "comments" / f"{page_id}.json"
        notes = ""
        if comments.exists():
            items = json.loads(comments.read_text(encoding="utf-8"))
            notes = "<h3>DocSync inline comments</h3>" + "".join(
                f'<div class="comment"><div class="quote">«{html.escape(c["quote"])}»</div>{c["body"]}</div>' for c in items)
        return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(page["title"])}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#172b4d}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #dfe1e6;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f4f5f7}}.panel{{border:1px solid #c1c7d0;border-radius:4px;margin:12px 0}}.panel-title{{background:#deebff;padding:6px 10px;font-weight:600}}
.panel-body{{padding:10px}}.anchor{{color:#97a0af;font-size:11px}}.status{{padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700;text-transform:uppercase}}
.Green{{background:#e3fcef;color:#006644}}.Red{{background:#ffebe6;color:#bf2600}}.Yellow{{background:#fffae6;color:#974f0c}}
.missing{{color:#bf2600;background:#ffebe6;padding:8px}}.meta{{color:#6b778c;font-size:12px}}.comment{{border-left:3px solid #ffab00;padding:6px 10px;margin:8px 0;background:#fffae6}}
.quote{{font-style:italic;color:#6b778c}}pre.macro{{background:#f4f5f7;padding:8px;overflow:auto;font-size:12px}}</style></head>
<body><div class="meta">mock Confluence · space {self.space_key} · page {page_id} · version {page["version"]} · <a href="/mock-confluence/">all pages</a></div>
<h1>{html.escape(page["title"])}</h1>{body}{notes}</body></html>"""


def make_client(s) -> ConfluenceClient:
    """Factory by settings (docsync.config.Settings)."""
    if s.confluence_mode == "cloud":
        if not (s.confluence_url and s.confluence_email and s.confluence_token):
            raise RuntimeError("CONFLUENCE_MODE=cloud requires CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        return CloudConfluence(s.confluence_url, s.confluence_email, s.confluence_token, s.confluence_space)
    return MockConfluence(s.mock_confluence_dir, s.confluence_space, s.public_url)
