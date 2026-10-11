"""Architecture model: YAML cards under model/, docs-registry.yaml, workspace.dsl and templates.
Loading, applying a ChangeSet and validation."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
KINDS = ("systems", "services", "external", "integrations", "flows")


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


class Model:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.model_dir = self.root / "model"
        self.data: dict[str, dict[str, dict]] = {k: {} for k in KINDS}
        self.load()
    def load(self) -> "Model":
        for kind in KINDS:
            d = self.model_dir / kind
            self.data[kind] = {}
            for p in sorted(d.glob("*.yaml")) if d.exists() else []:
                card = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                key = card.get("id") or p.stem
                self.data[kind][key] = {**card, "_file": str(p.relative_to(self.root))}
        return self

    def __getitem__(self, kind: str) -> dict[str, dict]:
        return self.data[kind]

    @property
    def registry(self) -> dict:
        return yaml.safe_load((self.root / "docs-registry.yaml").read_text(encoding="utf-8"))

    def save_registry(self, registry: dict) -> None:
        (self.root / "docs-registry.yaml").write_text(
            yaml.safe_dump(registry, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def dump(self, kind: str, key: str, card: dict) -> Path:
        clean = {k: v for k, v in card.items() if not k.startswith("_")}
        path = self.model_dir / kind / f"{key}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def name_of(self, element_id: str) -> str:
        for kind in ("services", "external", "systems"):
            if element_id in self.data[kind]:
                return self.data[kind][element_id].get("name", element_id)
        return element_id
    def scope_elements(self, scope: str) -> set[str]:
        """scope = service:<id> | system:<id> | flow:<id> | integration:<key>"""
        kind, _, val = scope.partition(":")
        if kind == "service":
            return {val}
        if kind == "system":
            return {s for s, v in self["services"].items() if v.get("system") == val}
        if kind == "flow":
            return set(self["flows"].get(val, {}).get("participants", []))
        if kind == "integration":
            c = self["integrations"].get(val, {})
            return {c.get("from"), c.get("to")} - {None}
        return set()

    def integrations_for(self, elements: set[str]) -> list[dict]:
        rows = [c for c in self["integrations"].values() if c.get("from") in elements or c.get("to") in elements]
        return sorted(rows, key=lambda c: (c["from"], c["to"], c["protocol"]))

    def neighbours(self, elements: set[str]) -> set[str]:
        rows = self.integrations_for(elements)
        return {c["from"] for c in rows} | {c["to"] for c in rows}
    @staticmethod
    def integration_key(frm: str, to: str, protocol: str) -> str:
        return f"{frm}__{to.replace(':', '-')}__{protocol}"

    def apply_changeset(self, cs: dict) -> dict:
        """Deterministically applies ChangeSet events to YAML cards.

        Handles integration_added/changed/removed and service_added; everything else goes to info/needsReview.
        The `manual` field of cards is never changed.
        """
        mr = cs.get("mr") or {}
        mr_ref = f"{cs['project']['path']}!{mr['iid']}" if mr.get("iid") else f"{cs['project']['path']}@{cs['mergeSha'][:8]}"
        report: dict = {"applied": [], "needsReview": [], "info": [], "touched": []}
        for ev in cs.get("events", []):
            t = ev["type"]
            if t in ("integration_added", "integration_changed", "integration_removed"):
                to = ev.get("to", "")
                if to.startswith("unresolved:"):
                    report["needsReview"].append({"eventId": ev["eventId"], "reason": f"target not mapped to the model: {to}"})
                    continue
                if to.startswith("topic:") and to not in self["external"] and to not in self["services"]:
                    # register Kafka topics as external queue elements so that referential integrity holds
                    self.dump("external", to.replace(":", "-"),
                              {"id": to, "name": to.split(":", 1)[1], "kind": "queue", "description": "Kafka topic"})
                    self.load()
                key = self.integration_key(ev["from"], to, ev["protocol"])
                card = self["integrations"].get(key, {})
                if t == "integration_removed":
                    if not card:
                        continue
                    card["status"] = "deprecated"
                else:
                    card.setdefault("from", ev["from"])
                    card.setdefault("to", to)
                    card.setdefault("protocol", ev["protocol"])
                    card["interaction"] = ev.get("interaction", card.get("interaction", "sync"))
                    ops = set(card.get("operations", [])) | set(ev.get("operations", []))
                    if t == "integration_changed" and "operationsAfter" in ev.get("attributes", {}):
                        ops = set(ev["attributes"]["operationsAfter"])
                    card["operations"] = sorted(ops)
                    if ev.get("description") and not card.get("description"):
                        card["description"] = ev["description"]   # draft (LLM or template), edited by the analyst
                    card.setdefault("description", "")
                    card["status"] = "active" if ev.get("confidence", 1) >= 0.7 else "needs_review"
                    card.setdefault("manual", {"notes": ""})
                card["provenance"] = {"mr": mr_ref, "sha": cs["mergeSha"][:12], "source": ev["source"],
                                      "confidence": ev["confidence"], "runId": cs.get("runId", "")}
                path = self.dump("integrations", key, card)
                report["applied"].append({"eventId": ev["eventId"], "file": str(path.relative_to(self.root))})
                report["touched"] += [ev["from"], to]
            elif t == "service_added" and ev["from"] not in self["services"]:
                path = self.dump("services", ev["from"], {"id": ev["from"], "system": "unassigned", "name": ev["from"],
                                                          "technology": "", "repo": cs["project"]["path"],
                                                          "description": "", "datastores": [], "tags": []})
                report["applied"].append({"eventId": ev["eventId"], "file": str(path.relative_to(self.root))})
                report["touched"].append(ev["from"])
            else:
                # datastore_changed and others: not applied to the model, but shown to the analyst in the MR
                report["info"].append({"eventId": ev["eventId"], "type": t,
                                       "reason": f"type {t}: the model is not changed, check datastore/contract descriptions by hand"})
        report["touched"] = sorted(set(report["touched"]))
        self.load()
        return report
    def validate(self) -> list[str]:
        errors: list[str] = []
        integ_schema = load_schema("integration")
        reg_schema = load_schema("docs-registry")
        known = set(self["services"]) | set(self["external"])
        for key, card in self["integrations"].items():
            body = {k: v for k, v in card.items() if not k.startswith("_")}
            for e in jsonschema.Draft202012Validator(integ_schema).iter_errors(body):
                errors.append(f"{card['_file']}: {e.message}")
            for side in ("from", "to"):
                if card.get(side, "") not in known:
                    errors.append(f"{card['_file']}: {side}='{card.get(side)}' is not in the model")
        for sid, svc in self["services"].items():
            if svc.get("system") not in self["systems"] and svc.get("system") != "unassigned":
                errors.append(f"{svc['_file']}: system='{svc.get('system')}' is not in the model")
        for fid, flow in self["flows"].items():
            for p in flow.get("participants", []):
                if p not in known:
                    errors.append(f"{flow['_file']}: participant '{p}' is not in the model")
            for st in flow.get("steps", []):
                card = self["integrations"].get(st["integration"])
                if not card:
                    errors.append(f"{flow['_file']}: step references a missing integration {st['integration']}")
                elif card.get("status") == "deprecated":
                    errors.append(f"{flow['_file']}: integration {st['integration']} is deprecated - the scenario needs editing")
        try:
            reg = self.registry
            for e in jsonschema.Draft202012Validator(reg_schema).iter_errors(reg):
                errors.append(f"docs-registry.yaml: {e.message}")
            views = self.workspace_view_keys()
            for page in reg.get("pages", []):
                for b in page.get("blocks", []):
                    if b["template"] == "c4-view" and views and b.get("view") not in views:
                        errors.append(f"docs-registry.yaml: block {b['blockId']} references view '{b.get('view')}' which is not in workspace.dsl")
                    kind, _, val = b["scope"].partition(":")
                    pool = {"service": self["services"], "system": self["systems"], "flow": self["flows"],
                            "integration": self["integrations"]}.get(kind, {})
                    if val not in pool:
                        errors.append(f"docs-registry.yaml: block {b['blockId']}: scope '{b['scope']}' is not in the model")
        except FileNotFoundError:
            errors.append("docs-registry.yaml is missing")
        return errors

    def workspace_view_keys(self) -> set[str]:
        """View keys from workspace.dsl (systemLandscape/systemContext/container/component "key")."""
        import re
        ws = self.root / "workspace.dsl"
        if not ws.exists():
            return set()
        text = ws.read_text(encoding="utf-8")
        keys = set(re.findall(r'^\s*(?:systemLandscape|systemContext|container|component|deployment|dynamic)\s+(?:[\w.-]+\s+)?"([\w-]+)"', text, re.M))
        return keys
