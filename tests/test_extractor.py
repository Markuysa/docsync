from docsync.extractor import extract, normalize_target


def test_normalize_target():
    aliases = {"BillingService": "billing-service", "billing.payments.svc": "billing-service"}
    assert normalize_target("BillingService", aliases) == ("billing-service", 1.0)
    assert normalize_target("Billing", aliases) == ("billing-service", 0.9)
    assert normalize_target("billing.internal", aliases) == ("billing-service", 0.8)
    assert normalize_target("Unknown", aliases) == ("unresolved:Unknown", 0.5)


def test_extract_grpc_added(env):
    cs = extract(env["serviceRepo"], env["commits"]["v1"], env["commits"]["v2"], "payments/orders-service")
    types = {e["type"]: e for e in cs["events"]}
    assert "integration_added" in types and "datastore_changed" in types
    ev = types["integration_added"]
    assert ev["to"] == "billing-service" and ev["protocol"] == "grpc" and ev["interaction"] == "sync"
    assert set(ev["operations"]) == {"Charge", "Refund"}
    assert ev["confidence"] == 1.0
    assert any(v["path"].endswith("BillingClient.kt") for v in ev["evidence"])
    assert any(v["path"].endswith("application.yml") for v in ev["evidence"])


def test_extract_kafka_added(env):
    cs = extract(env["serviceRepo"], env["commits"]["v2"], env["commits"]["v3"], "payments/orders-service")
    assert [e["type"] for e in cs["events"]] == ["integration_added"]
    assert cs["events"][0]["to"] == "topic:orders.created" and cs["events"][0]["interaction"] == "async"


def test_extract_removed_is_symmetric(env):
    cs = extract(env["serviceRepo"], env["commits"]["v2"], env["commits"]["v1"], "payments/orders-service")
    assert {e["type"] for e in cs["events"]} == {"integration_removed"}


def test_extract_no_changes(env):
    cs = extract(env["serviceRepo"], env["commits"]["v2"], env["commits"]["v2"], "payments/orders-service")
    assert cs["skipReason"] == "no_relevant_files" and cs["events"] == []
