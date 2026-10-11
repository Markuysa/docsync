"""Build a ChangeSet by comparing the integration facts of a service in two git revisions.

Relevant files are scanned in both revisions and the sets of facts are compared, so renames and
formatting changes produce no events. Per-service settings come from docsync.yaml in the repository root
(service id, target aliases, include globs)."""
from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

import yaml

EXTRACTOR_VERSION = "1.0.0"

RELEVANT_GLOBS = [
    "*.proto", "**/*.proto",
    "**/application*.yml", "**/application*.yaml", "**/application*.properties",
    "**/values*.yaml", "**/config*.yaml", "**/config*.yml",
    "**/openapi*.yaml", "**/openapi*.yml", "**/openapi*.json",
    "**/*.java", "**/*.kt", "**/*.go", "**/*.cs", "**/*.py", "**/*.ts",
    "db/migration/**", "**/db/migration/**", "**/db/changelog/**", "**/migrations/**",
]
EXCLUDE_GLOBS = ["**/test/**", "**/tests/**", "**/*_test.go", "**/*Test.java", "**/*Test.kt", "**/testdata/**",
                 "**/node_modules/**", "**/vendor/**", "**/build/**", "**/target/**"]

# Rule: (id, protocol, interaction, regex, group holding the target name)
CODE_RULES = [
    # Java/Kotlin gRPC: BillingServiceGrpc.newBlockingStub(channel)
    ("grpc-java-stub", "grpc", "sync", re.compile(r"\b(\w+?)(?:Service)?Grpc\.new(?:Blocking|Future)?Stub\s*\("), 1),
    # Go gRPC: billingpb.NewBillingServiceClient(conn)
    ("grpc-go-client", "grpc", "sync", re.compile(r"\b\w+\.New(\w+?)(?:Service)?Client\s*\(\s*\w*[cC]onn"), 1),
    # Python gRPC: billing_pb2_grpc.BillingServiceStub(channel)
    ("grpc-py-stub", "grpc", "sync", re.compile(r"\b\w+_pb2_grpc\.(\w+?)(?:Service)?Stub\s*\("), 1),
    # Kafka: @KafkaListener(topics = "orders.created") / kafkaTemplate.send("orders.created", ...)
    ("kafka-listener", "kafka", "async", re.compile(r"@KafkaListener\([^)]*topics\s*=\s*[\[{]?\s*\"([\w.\-]+)\""), 1),
    ("kafka-send", "kafka", "async", re.compile(r"kafkaTemplate\.send\(\s*\"([\w.\-]+)\""), 1),
    # Go/Python kafka producers: Topic: "orders.created" / topic="orders.created"
    ("kafka-topic-literal", "kafka", "async", re.compile(r"\b[Tt]opic\s*[:=]\s*\"([\w.\-]+)\""), 1),
    # HTTP clients with a base URL in code: WebClient.create("https://host") / baseUrl("https://host")
    ("http-code-url", "http", "sync", re.compile(r"(?:baseUrl|create|url)\(\s*\"https?://([\w.\-]+)"), 1),
]
CONFIG_RULES = [
    # grpc-spring-boot-starter: grpc.client.<name>.address
    ("grpc-config-address", "grpc", "sync", re.compile(r"^\s*grpc\.client\.([\w\-]+)\.address\s*[=:]\s*(\S+)", re.M), 1),
    # gRPC clients in a YAML tree: billing: { address: dns:///billing.payments.svc:9090 }
    ("grpc-config-yaml", "grpc", "sync", re.compile(r"^\s*address\s*:\s*[\"']?(?:dns:///|static://)?([\w.\-]+):\d+", re.M), 1),
    # HTTP clients: *.base-url / baseUrl / url: https://host
    ("http-config-url", "http", "sync", re.compile(r"(?:base-?url|baseUrl|endpoint|url)\s*[:=]\s*[\"']?https?://([\w.\-]+)", re.I), 1),
    # Kafka topics in config
    ("kafka-config-topic", "kafka", "async", re.compile(r"^\s*(?:topic|topic-name|topicName)\s*[:=]\s*[\"']?([\w.\-]+)", re.M), 1),
]
# gRPC operations: stub.charge(request) - heuristic
GRPC_CALL_RULE = re.compile(r"\.(\w+)\(\s*\w*[Rr]equest")
# HTTP operations: .get("/v2/payments") / .post("/v1/x") - path only
HTTP_CALL_RULE = re.compile(r"\.(get|post|put|delete|patch)\(\s*\"(/[\w/\-{}.]*)\"", re.I)


@dataclass(frozen=True)
class Fact:
    rule: str
    protocol: str
    interaction: str
    target_raw: str


@dataclass
class FactHit:
    fact: Fact
    evidence: list[dict] = field(default_factory=list)
    operations: set[str] = field(default_factory=set)


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def match_any(path: str, globs: list[str]) -> bool:
    p = PurePosixPath(path)
    return any(fnmatch.fnmatch(path, g) or p.match(g) for g in globs)


def list_files(repo: str, sha: str, include: list[str]) -> list[str]:
    files = git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    return [f for f in files if match_any(f, include) and not match_any(f, EXCLUDE_GLOBS)]


def read_file(repo: str, sha: str, path: str) -> str:
    try:
        return git(repo, "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        return ""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan(repo: str, sha: str, include: list[str]) -> dict[Fact, FactHit]:
    """Collects facts (integrations) from all relevant files of a revision."""
    hits: dict[Fact, FactHit] = {}
    for path in list_files(repo, sha, include):
        text = read_file(repo, sha, path)
        if not text:
            continue
        is_config = path.endswith((".yml", ".yaml", ".properties"))
        rules = CONFIG_RULES if is_config else CODE_RULES
        lines = text.splitlines()
        for rule_id, proto, inter, rx, g_target in rules:
            for m in rx.finditer(text):
                fact = Fact(rule_id, proto, inter, m.group(g_target))
                hit = hits.setdefault(fact, FactHit(fact))
                ln = _line_of(text, m.start())
                hit.evidence.append({"path": path, "lines": str(ln), "sha": sha, "snippet": lines[ln - 1].strip()[:400]})
                if not is_config and proto == "grpc":
                    for op in GRPC_CALL_RULE.findall(text):
                        hit.operations.add(op[0].upper() + op[1:])
                if not is_config and proto == "http":
                    for verb, p in HTTP_CALL_RULE.findall(text):
                        hit.operations.add(f"{verb.upper()} {p}")
    return hits


def normalize_target(raw: str, aliases: dict[str, str]) -> tuple[str, float]:
    """Maps a raw target name (class, host) to a model element ID. Returns (id, confidence)."""
    if raw in aliases:
        return aliases[raw], 1.0
    kebab = re.sub(r"(?<!^)(?=[A-Z])", "-", raw).lower()          # BillingService -> billing-service
    for candidate in (kebab, raw.lower(), f"{kebab}-service", f"{raw.lower()}-service"):
        if candidate in aliases.values():
            return candidate, 0.9
    host_head = raw.split(".")[0].lower()                            # billing.payments.svc -> billing
    for candidate in (host_head, f"{host_head}-service"):
        if candidate in aliases.values():
            return candidate, 0.8
    return f"unresolved:{raw}", 0.5


def event_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _group(hits: dict[Fact, FactHit], aliases: dict[str, str]) -> dict[tuple[str, str], dict]:
    """Groups facts by (target, protocol): one relationship = one event, evidence from all rules."""
    out: dict[tuple[str, str], dict] = {}
    for fact, hit in hits.items():
        if fact.protocol == "kafka":
            to, conf = f"topic:{fact.target_raw}", 1.0
        else:
            to, conf = normalize_target(fact.target_raw, aliases)
        g = out.setdefault((to, fact.protocol), {"to": to, "protocol": fact.protocol, "interaction": fact.interaction,
                                                 "confidence": conf, "evidence": [], "operations": set(), "rules": set()})
        g["confidence"] = max(g["confidence"], conf)
        g["evidence"].extend(hit.evidence[:3])
        g["operations"] |= hit.operations
        g["rules"].add(fact.rule)
    return out


def _event(etype: str, service: str, g: dict) -> dict:
    return {
        "eventId": event_id(etype, service, g["to"], g["protocol"]),
        "type": etype,
        "from": service,
        "to": g["to"],
        "protocol": g["protocol"],
        "interaction": g["interaction"],
        "operations": sorted(g["operations"]),
        "evidence": g["evidence"][:5],
        "source": "rule",
        "confidence": g["confidence"],
        "rule": ",".join(sorted(g["rules"])),
    }


def build_events(service: str, before: dict[Fact, FactHit], after: dict[Fact, FactHit], aliases: dict[str, str]) -> list[dict]:
    b, a = _group(before, aliases), _group(after, aliases)
    events: list[dict] = []
    for key in sorted(a.keys() - b.keys()):
        events.append(_event("integration_added", service, a[key]))
    for key in sorted(b.keys() - a.keys()):
        events.append(_event("integration_removed", service, b[key]))
    for key in sorted(a.keys() & b.keys()):
        if a[key]["operations"] != b[key]["operations"]:
            ev = _event("integration_changed", service, a[key])
            ev["attributes"] = {"operationsBefore": sorted(b[key]["operations"]), "operationsAfter": sorted(a[key]["operations"])}
            events.append(ev)
    return events


def migration_events(repo: str, base: str, head: str, service: str) -> list[dict]:
    changed = git(repo, "diff", "--name-status", base, head).splitlines()
    files = [l.split("\t")[-1] for l in changed if l.startswith(("A", "M"))]
    mig = [f for f in files if match_any(f, ["db/migration/**", "**/db/migration/**", "**/db/changelog/**", "**/migrations/**"])]
    if not mig:
        return []
    return [{"eventId": event_id("datastore_changed", service, head), "type": "datastore_changed", "from": service,
             "evidence": [{"path": p, "sha": head} for p in mig[:5]], "source": "rule", "confidence": 1.0, "rule": "db-migration"}]


def extract(repo: str, base: str, head: str, project_path: str, project_id: int = 0) -> dict:
    """Main function: a ChangeSet (schemas/change-set.schema.json) from two repository revisions."""
    cfg_text = read_file(repo, head, "docsync.yaml")
    cfg = (yaml.safe_load(cfg_text) if cfg_text else {}) or {}
    service = cfg.get("service") or project_path.split("/")[-1]
    aliases = cfg.get("aliases", {}) or {}
    include = cfg.get("include") or RELEVANT_GLOBS

    if not base or set(base) == {"0"}:  # first push of the branch: compare with the root commit
        base = git(repo, "rev-list", "--max-parents=0", head).split()[0]

    changed = [l.split("\t")[-1] for l in git(repo, "diff", "--name-status", base, head).splitlines()]
    relevant = [f for f in changed if match_any(f, include) and not match_any(f, EXCLUDE_GLOBS)]

    cs = {
        "schemaVersion": "1.0",
        "project": {"id": project_id, "path": project_path},
        "service": service,
        "baseSha": base,
        "mergeSha": head,
        "relevantFiles": relevant,
        "events": [],
        "skipReason": None,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "extractorVersion": EXTRACTOR_VERSION,
    }
    if not relevant:
        cs["skipReason"] = "no_relevant_files"
        return cs
    before, after = scan(repo, base, include), scan(repo, head, include)
    cs["events"] = build_events(service, before, after, aliases) + migration_events(repo, base, head, service)
    if not cs["events"]:
        cs["skipReason"] = "no_events"
    return cs
