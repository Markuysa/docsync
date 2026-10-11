"""LLM client for ChangeSet enrichment and the audit of hand-written sections.
Providers: none, anthropic (SDK), openai-compatible (gateway)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

PROMPTS = Path(__file__).resolve().parent / "prompts"

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": {
        "type": "object",
        "properties": {"eventId": {"type": "string"}, "to": {"type": "string"},
                       "confidence": {"type": "number"}, "description": {"type": "string"}},
        "required": ["eventId", "to", "confidence", "description"], "additionalProperties": False}}},
    "required": ["events"], "additionalProperties": False,
}
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {"pageId": {"type": "string"}, "quote": {"type": "string"}, "reason": {"type": "string"},
                       "suggestion": {"type": "string"}, "confidence": {"type": "number"}},
        "required": ["pageId", "quote", "reason", "suggestion", "confidence"], "additionalProperties": False}}},
    "required": ["findings"], "additionalProperties": False,
}


def template_description(ev: dict, name_of) -> str:
    """Description without an LLM - an honest template (in Russian, the language of the target space); the analyst refines it in the MR."""
    frm, to = name_of(ev["from"]), name_of(ev.get("to", ""))
    kind = "асинхронно" if ev.get("interaction") == "async" else "синхронно"
    ops = ", ".join(ev.get("operations", [])[:4])
    verb = "публикует/читает" if ev.get("protocol") == "kafka" else "вызывает"
    text = f"{frm} {kind} {verb} {to} по {ev.get('protocol', '?')}"
    return text + (f" (операции: {ops})." if ops else ". Назначение уточнить у автора MR.")


class LLM:
    def __init__(self, provider: str, model: str, api_key: str = "", base_url: str = ""):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @property
    def enabled(self) -> bool:
        return self.provider in ("anthropic", "openai-compatible")
    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        if self.provider == "anthropic":
            return self._anthropic(system, user, schema)
        if self.provider == "openai-compatible":
            return self._openai_compatible(system, user, schema)
        raise RuntimeError("LLM is disabled (LLM_PROVIDER=none)")

    def _anthropic(self, system: str, user: str, schema: dict) -> dict:
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key or None, base_url=self.base_url or None)
        resp = client.messages.create(
            model=self.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("LLM refused the request (stop_reason=refusal)")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    def _openai_compatible(self, system: str, user: str, schema: dict) -> dict:
        url = f"{self.base_url or 'https://api.openai.com'}/v1/chat/completions"
        body = {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_schema", "json_schema": {"name": "docsync", "schema": schema, "strict": True}},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=120)
        if r.status_code >= 300:
            raise RuntimeError(f"LLM gateway -> {r.status_code}: {r.text[:400]}")
        return json.loads(r.json()["choices"][0]["message"]["content"])
    def enrich(self, cs: dict, known_elements: list[str], name_of) -> dict:
        """Enriches events: maps unresolved targets and writes descriptions. Without an LLM - templated descriptions."""
        if not self.enabled:
            for ev in cs["events"]:
                if ev["type"] in ("integration_added", "integration_changed") and not ev.get("description"):
                    ev["description"] = template_description(ev, name_of)
            return cs
        system = (PROMPTS / "enrich-changeset.md").read_text(encoding="utf-8")
        user = json.dumps({"service": cs["service"], "mr": cs.get("mr"), "knownElements": known_elements,
                           "events": cs["events"]}, ensure_ascii=False)
        try:
            out = self.complete_json(system, user, ENRICH_SCHEMA)
        except Exception as e:  # degrade gracefully: keep rule-based events only
            cs.setdefault("warnings", []).append(f"LLM enrich failed: {e}")
            return self.__class__("none", "").enrich(cs, known_elements, name_of)
        by_id = {e["eventId"]: e for e in out.get("events", [])}
        for ev in cs["events"]:
            x = by_id.get(ev["eventId"])
            if not x:
                continue
            if ev.get("to", "").startswith("unresolved:") and x["to"] in known_elements:
                ev["to"], ev["source"], ev["confidence"] = x["to"], "llm", min(float(x.get("confidence", 0.6)), 0.9)
            if x.get("description"):
                ev["description"] = x["description"]
        return cs

    def audit(self, changes: list[dict], pages: list[dict]) -> list[dict]:
        """pages: [{pageId, title, text}] - hand-written text without autoblocks. Returns findings."""
        if not self.enabled:
            return []
        system = (PROMPTS / "audit-findings.md").read_text(encoding="utf-8")
        user = json.dumps({"changes": changes, "pages": pages}, ensure_ascii=False)
        return self.complete_json(system, user, FINDINGS_SCHEMA).get("findings", [])
