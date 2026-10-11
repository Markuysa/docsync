import json
from pathlib import Path

from docsync.extractor import extract
from docsync.generate import generate, generate_dsl, build_workspace
from docsync.model import Model


def test_apply_validate_generate(env, tmp_path):
    repo = Path(env["modelRepo"])
    model = Model(repo)
    assert model.validate() == []
    cs = extract(env["serviceRepo"], env["commits"]["v1"], env["commits"]["v2"], "payments/orders-service")
    cs["runId"] = "r-test"
    report = model.apply_changeset(cs)
    assert [a["file"] for a in report["applied"]] == ["model/integrations/orders-service__billing-service__grpc.yaml"]
    assert report["needsReview"] == [] and report["info"][0]["type"] == "datastore_changed"
    assert set(report["touched"]) == {"orders-service", "billing-service"}
    card = model["integrations"]["orders-service__billing-service__grpc"]
    assert card["operations"] == ["Charge", "Refund"] and card["status"] == "active"
    assert model.validate() == []

    dsl = generate_dsl(model)
    assert 'payments.orders_service -> payments.billing_service "Charge, Refund" "grpc" "grpc,sync"' in dsl["relationships.dsl"]
    ws = build_workspace(model, dsl)
    assert "!include" not in ws and "softwareSystem" in ws

    out = tmp_path / "build"
    m = generate(model, out, changed={"orders-service", "billing-service"}, sha="abc123")
    blocks = {b["blockId"]: b for p in m["pages"] for b in p["blocks"]}
    # orders + billing + domain matrix + C4 (neighbour) + sequence (participants include orders/billing)
    assert {"orders-integrations-table", "billing-integrations-table", "payments-integration-matrix",
            "payments-c4-containers", "checkout-sequence"} <= set(blocks)
    table = (out / "blocks/orders-integrations-table.xhtml").read_text(encoding="utf-8")
    assert "Billing Service" in table and "<code>Charge</code>" in table and "docsync:auto:orders-integrations-table:start" in table
    assert (out / "diagrams/checkout-sequence.puml").exists() and (out / "workspace.dsl").exists()
    assert blocks["payments-c4-containers"]["diagram"]["view"] == "payments-containers"


def test_unrelated_change_touches_nothing(env, tmp_path):
    model = Model(Path(env["modelRepo"]))
    m = generate(model, tmp_path / "b", changed={"something-else"}, sha="x")
    assert m["pages"] == []


def test_removed_integration_marks_deprecated_and_flow_invalid(env):
    model = Model(Path(env["modelRepo"]))
    cs = extract(env["serviceRepo"], env["commits"]["v1"], env["commits"]["v2"], "payments/orders-service")
    model.apply_changeset(cs)
    rem = extract(env["serviceRepo"], env["commits"]["v2"], env["commits"]["v1"], "payments/orders-service")
    model.apply_changeset(rem)
    assert model["integrations"]["orders-service__billing-service__grpc"]["status"] == "deprecated"


def test_kafka_topic_becomes_external_queue(env):
    model = Model(Path(env["modelRepo"]))
    cs = extract(env["serviceRepo"], env["commits"]["v2"], env["commits"]["v3"], "payments/orders-service")
    model.apply_changeset(cs)
    assert model["external"]["topic:orders.created"]["kind"] == "queue"
    assert model.validate() == []
    assert 'topic_orders_created = softwareSystem "orders.created" "Kafka topic" "Queue"' in generate_dsl(model)["elements.dsl"]


def test_changed_flow_regenerates_its_sequence_block(env, tmp_path):
    model = Model(Path(env["modelRepo"]))
    m = generate(model, tmp_path / "b", changed={"checkout"}, sha="x")
    assert [b["blockId"] for p in m["pages"] for b in p["blocks"]] == ["checkout-sequence"]
