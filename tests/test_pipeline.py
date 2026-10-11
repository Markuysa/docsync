"""End-to-end run in mock mode. Diagram rendering is stubbed when Kroki is unavailable."""
import json
from pathlib import Path

import pytest

from docsync import render
from docsync.pipeline import Pipeline


@pytest.fixture
def pl(env, monkeypatch):
    from docsync.config import settings
    p = Pipeline(settings())
    if not p.kroki.healthy():
        monkeypatch.setattr(render.Kroki, "healthy", lambda self: True)
        monkeypatch.setattr(render.Kroki, "render", lambda self, kind, src, options=None, fmt="svg":
                            f'<svg xmlns="http://www.w3.org/2000/svg"><text>{kind} stub</text></svg>'.encode())
    return p


def test_end_to_end(pl, env):
    pages = pl.bootstrap_pages()
    assert {p["status"] for p in pages} == {"CREATED"} and len(pages) == 4
    assert all(p.get("pageId") for p in __import__("docsync.model", fromlist=["Model"]).Model(Path(env["modelRepo"])).registry["pages"])

    run = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    assert run["status"] == "PROPOSED", run
    assert run["branch"].startswith("docsync/orders-service-")
    assert [p["key"] for p in run["preview"]["pages"]] == ["orders-service", "billing-service", "checkout-flow", "payments-overview"]
    assert (Path(env["modelRepo"]) / "changesets").exists() is False  # the changeset lives in the branch, main is clean

    dup = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    assert dup["status"] == "DUPLICATE"

    res = pl.approve(run["run_id"])
    assert res["run"]["status"] == "PUBLISHED"
    rep = res["publish"]
    assert {p["status"] for p in rep["pages"]} == {"PUBLISHED"} and rep["conflicts"] == []
    card = Path(env["modelRepo"]) / "model/integrations/orders-service__billing-service__grpc.yaml"
    assert card.exists()

    cf = pl.confluence
    orders = next(p for p in rep["pages"] if p["key"] == "orders-service")
    body = cf.get_page(orders["pageId"])["body"]
    assert "не вызывает Billing Service напрямую" in body   # hand-written text untouched
    assert "<code>Charge</code>" in body and 'ri:filename="payments-containers.svg"' in body
    assert (pl.s.mock_confluence_dir / "attachments" / orders["pageId"] / "payments-containers.svg").exists()

    # re-publishing changes nothing
    rep2 = pl.publish()
    assert {p["status"] for p in rep2["pages"]} == {"UNCHANGED"}

    # manual edit inside an autoblock -> CONFLICT, the page is not overwritten
    page = cf.get_page(orders["pageId"])
    cf.update_page(page["id"], page["title"], page["body"].replace("<code>Charge</code>", "<code>Charge (вручную)</code>"), page["version"], "manual")
    run2 = pl.ingest("payments/orders-service", env["commits"]["v2"], env["commits"]["v3"])
    assert run2["status"] == "PROPOSED"
    res2 = pl.approve(run2["run_id"])
    assert res2["run"]["status"] == "PARTIALLY_PUBLISHED"
    statuses = {c["blockId"]: c["status"] for c in res2["publish"]["conflicts"]}
    assert statuses == {"orders-integrations-table": "CONFLICT"}
    assert "Charge (вручную)" in cf.get_page(orders["pageId"])["body"]
    assert "orders.created" in cf.get_page(orders["pageId"])["body"]   # the other blocks were updated

    # force: take ownership of the block again -> conflict resolved, manual edit overwritten
    rep3 = pl.publish(force=True)
    assert rep3["conflicts"] == [] and "Charge (вручную)" not in cf.get_page(orders["pageId"])["body"]
    assert {p["status"] for p in pl.publish()["pages"]} == {"UNCHANGED"}

    runs = pl.journal.list_runs()
    assert sorted(r["status"] for r in runs) == ["PARTIALLY_PUBLISHED", "PUBLISHED"]
    assert pl.audit(run["run_id"])["status"] == "SKIPPED"


def test_dry_run(pl, env, monkeypatch):
    pl.bootstrap_pages()
    pl.s.dry_run = True
    run = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    rep = pl.approve(run["run_id"])["publish"]
    assert {p["status"] for p in rep["pages"]} == {"DRY_RUN"}
    assert "<code>Charge</code>" not in pl.confluence.get_page(rep["pages"][0]["pageId"])["body"]
