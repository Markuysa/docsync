"""Local demo: sample repositories (model template and a service with several commits), end-to-end run,
GitLab push hook simulation."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import settings

TEMPLATE = Path(__file__).resolve().parent.parent / "architecture-model"

FILES_V1 = {
    "docsync.yaml": """service: orders-service
aliases:
  BillingService: billing-service
  Billing: billing-service
  billing.payments.svc: billing-service
  billing: billing-service
""",
    "src/main/kotlin/OrderService.kt": """package payments.orders

class OrderService(private val repo: OrderRepository) {
    fun place(order: Order): Order = repo.save(order.copy(status = "NEW"))
}
""",
    "src/main/resources/application.yml": """spring:
  application:
    name: orders-service
server:
  port: 8080
""",
    "db/migration/V1__init.sql": "create table orders (id uuid primary key, status text not null);\n",
}
FILES_V2 = {
    "src/main/kotlin/BillingClient.kt": """package payments.orders

import io.grpc.ManagedChannel
import payments.billing.BillingServiceGrpc
import payments.billing.ChargeRequest
import payments.billing.RefundRequest

class BillingClient(channel: ManagedChannel) {
    private val stub = BillingServiceGrpc.newBlockingStub(channel)

    fun charge(orderId: String, amount: Long) =
        stub.charge(ChargeRequest.newBuilder().setOrderId(orderId).setAmount(amount).build())

    fun refund(orderId: String) =
        stub.refund(RefundRequest.newBuilder().setOrderId(orderId).build())
}
""",
    "src/main/resources/application.yml": """spring:
  application:
    name: orders-service
server:
  port: 8080
grpc:
  client:
    billing:
      address: dns:///billing.payments.svc:9090
      negotiationType: plaintext
""",
    "db/migration/V2__order_payment.sql": "alter table orders add column payment_id text;\n",
}
FILES_V3 = {
    "src/main/kotlin/OrderEvents.kt": """package payments.orders

import org.springframework.kafka.core.KafkaTemplate

class OrderEvents(private val kafkaTemplate: KafkaTemplate<String, String>) {
    fun orderCreated(orderId: String) {
        kafkaTemplate.send("orders.created", orderId, "{\\"orderId\\": \\"$orderId\\"}")
    }
}
""",
}


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "demo@local")
    _git(repo, "config", "user.name", "Demo")


def seed(data_dir: Path | None = None) -> dict:
    s = settings()
    data = data_dir or s.data_dir
    repos = data / "repos"
    model_repo, svc_repo = repos / "architecture-model", repos / "orders-service"
    for r in (model_repo, svc_repo):
        shutil.rmtree(r, ignore_errors=True)

    shutil.copytree(TEMPLATE, model_repo, ignore=shutil.ignore_patterns("generated", "build", "__pycache__"))
    _init(model_repo)
    _git(model_repo, "add", "-A")
    _git(model_repo, "commit", "-q", "-m", "architecture-model: initial")

    _init(svc_repo)
    v1 = _commit_files(svc_repo, FILES_V1, "feat: orders service skeleton")
    v2 = _commit_files(svc_repo, FILES_V2, "feat: charge orders via Billing gRPC (PAY-128)")
    v3 = _commit_files(svc_repo, FILES_V3, "feat: publish orders.created to Kafka (PAY-131)")
    info = {"modelRepo": str(model_repo), "serviceRepo": str(svc_repo), "projectPath": "payments/orders-service",
            "commits": {"v1": v1, "v2": v2, "v3": v3}}
    (data / "demo.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def run() -> int:
    from .pipeline import Pipeline
    s = settings()
    info = json.loads((s.data_dir / "demo.json").read_text(encoding="utf-8"))
    s.service_repos.setdefault(info["projectPath"], Path(info["serviceRepo"]))
    pl = Pipeline(s)

    print("== 0. Confluence pages (bootstrap) ==")
    for p in pl.bootstrap_pages():
        print(f"   {p['status']:8} {p['url']}")

    for label, before, after in (("Scenario 1: Orders → Billing via gRPC", "v1", "v2"),
                                 ("Scenario 2: Orders publishes orders.created to Kafka", "v2", "v3")):
        print(f"\n== {label} ==")
        run = pl.ingest(info["projectPath"], info["commits"][before], info["commits"][after])
        print(f"   ingest  -> {run.get('status')} run={run.get('run_id', '')[:8]}")
        for e in (run.get("changeset") or {}).get("events", []):
            print(f"      event {e['type']}: {e['from']} → {e.get('to')} [{e.get('protocol')}] ops={e.get('operations')}")
        if run.get("status") not in ("PROPOSED", "NEEDS_REVIEW"):
            print("   nothing to approve:", run.get("error") or run.get("skip_reason"))
            continue
        print(f"   model branch: {run['branch']}  →  pages: {[p['title'] for p in run['preview']['pages']]}")
        res = pl.approve(run["run_id"])
        rep = res["publish"]
        print(f"   approve -> {res['run']['status']} (model {rep['modelSha'][:8]}, dryRun={rep['dryRun']})")
        for p in rep["pages"]:
            print(f"      {p['status']:10} {p['title']}: " + ", ".join(f"{b['blockId']}={b['status']}" for b in p["blocks"]) + f"  {p.get('url', '')}")
        if pl.llm.enabled:
            a = pl.audit(run["run_id"])
            print(f"   audit   -> {a['status']}: {len(a.get('findings', []))} findings")
        else:
            print("   audit   -> skipped (LLM_PROVIDER=none)")
    print("\nJournal:", json.dumps(pl.journal.kpi(), ensure_ascii=False))
    print(f"Pages: {s.public_url}/mock-confluence/  (when `docsync serve` is running)")
    return 0


def push(step: str, n8n_url: str) -> dict:
    """Simulates a GitLab Push Hook to n8n for step v1→v2 ("grpc") or v2→v3 ("kafka")."""
    import httpx
    s = settings()
    info = json.loads((s.data_dir / "demo.json").read_text(encoding="utf-8"))
    before, after = {"grpc": ("v1", "v2"), "kafka": ("v2", "v3")}[step]
    payload = {
        "object_kind": "push", "event_name": "push", "ref": "refs/heads/main",
        "before": info["commits"][before], "after": info["commits"][after], "checkout_sha": info["commits"][after],
        "user_username": "dev", "project_id": 42,
        "project": {"id": 42, "name": "orders-service", "path_with_namespace": info["projectPath"], "default_branch": "main",
                    "web_url": "https://gitlab.example.com/" + info["projectPath"]},
        "commits": [{"id": info["commits"][after], "message": "demo", "url": ""}], "total_commits_count": 1,
    }
    r = httpx.post(n8n_url, json=payload, headers={"X-Gitlab-Token": s.gitlab_webhook_secret or "demo-secret"}, timeout=30)
    return {"url": n8n_url, "status": r.status_code, "response": r.text[:300]}


def main(action: str, step: str = "grpc", n8n_url: str = "http://localhost:5678/webhook/docsync/gitlab") -> int:
    s = settings()
    if action == "push":
        print(json.dumps(push(step, n8n_url), ensure_ascii=False, indent=2))
        return 0
    if action == "reset":
        shutil.rmtree(s.data_dir, ignore_errors=True)
        print("data/ removed")
        return 0
    if action == "seed":
        info = seed()
        print(json.dumps(info, indent=2))
        return 0
    return run()
