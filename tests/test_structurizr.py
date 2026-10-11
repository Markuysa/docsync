"""C4 mode `structurizr`: iframe embeds instead of SVG attachments, workspace pushed on publish."""
from pathlib import Path

import pytest

from docsync import render
from docsync.generate import generate
from docsync.model import Model
from docsync.pipeline import Pipeline
from docsync.structurizr import StructurizrServer


def server():
    return StructurizrServer("https://structurizr.example.com", "", "7", "key", "secret", cli="docker")


def test_embed_url_and_macro():
    st = server()
    assert st.embed_url("payments-containers", "docsync-x") == \
        "https://structurizr.example.com/embed/7?diagram=payments-containers&diagramSelector=false&iframe=docsync-x"
    macro = st.iframe_macro("payments-containers", "payments-c4-containers")
    assert 'ac:name="iframe"' in macro and '<ri:url ri:value="https://structurizr.example.com/embed/7?diagram=payments-containers' in macro
    assert "share/7/diagrams#payments-containers" in macro


def test_generate_c4_embed_blocks(env, tmp_path):
    model = Model(Path(env["modelRepo"]))
    m = generate(model, tmp_path / "b", sha="x", structurizr=server())
    assert m["c4Mode"] == "structurizr"
    blocks = {b["blockId"]: b for p in m["pages"] for b in p["blocks"]}
    c4 = blocks["payments-c4-containers"]
    assert c4["diagram"]["kind"] == "structurizr-embed" and c4["diagram"]["embedded"] is True
    xhtml = (tmp_path / "b" / c4["file"]).read_text(encoding="utf-8")
    assert 'ac:name="iframe"' in xhtml and "ri:attachment" not in xhtml
    assert blocks["checkout-sequence"]["diagram"]["kind"] == "plantuml"   # sequence diagrams are still rendered


def test_publish_pushes_workspace_and_embeds(env, monkeypatch):
    monkeypatch.setenv("DOCSYNC_C4_MODE", "structurizr")
    monkeypatch.setenv("STRUCTURIZR_URL", "https://structurizr.example.com")
    monkeypatch.setenv("STRUCTURIZR_WORKSPACE_ID", "7")
    monkeypatch.setenv("STRUCTURIZR_API_KEY", "k")
    monkeypatch.setenv("STRUCTURIZR_API_SECRET", "s")
    from docsync import config
    config.reset_settings()
    pl = Pipeline(config.settings())
    assert pl.structurizr is not None and pl.structurizr.configured
    if not pl.kroki.healthy():
        monkeypatch.setattr(render.Kroki, "healthy", lambda self: True)
        monkeypatch.setattr(render.Kroki, "render", lambda self, kind, src, options=None, fmt="svg": b"<svg/>")
    pushed = {}
    monkeypatch.setattr(StructurizrServer, "push", lambda self, ws: pushed.setdefault("dsl", Path(ws).read_text(encoding="utf-8")) and "ok")
    pl.bootstrap_pages()
    rep = pl.publish()
    assert rep["c4Mode"] == "structurizr" and "softwareSystem" in pushed["dsl"]
    orders = next(p for p in rep["pages"] if p["key"] == "orders-service")
    body = pl.confluence.get_page(orders["pageId"])["body"]
    assert "/embed/7?diagram=payments-containers" in body
    assert not (pl.s.mock_confluence_dir / "attachments" / orders["pageId"] / "payments-containers.svg").exists()
    assert (pl.s.mock_confluence_dir / "attachments").glob("*/checkout-sequence.svg")
