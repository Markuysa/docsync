#!/usr/bin/env python3
"""Generator of importable n8n workflow JSON for DocSync. Edit this file, not the JSON.

    python n8n/build_workflows.py   ->  n8n/workflows/*.json

n8n is a thin orchestrator here: it receives GitLab webhooks, calls the docsync engine over HTTP,
sends notifications and provides the "approve" link. All domain logic lives in the engine.
n8n environment variables ($env.*): DOCSYNC_ENGINE_URL, DOCSYNC_API_TOKEN, GITLAB_WEBHOOK_SECRET, DOCSYNC_NOTIFY_URL.
"""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent / "workflows"
OUT.mkdir(exist_ok=True)

WEBHOOK, HTTP, CODE, IF, RESPOND, ERROR = ("n8n-nodes-base.webhook", 2), ("n8n-nodes-base.httpRequest", 4.2), \
    ("n8n-nodes-base.code", 2), ("n8n-nodes-base.if", 2), ("n8n-nodes-base.respondToWebhook", 1.1), ("n8n-nodes-base.errorTrigger", 1)


class WF:
    def __init__(self, name: str, wf_id: str):
        self.name, self.id, self.nodes, self.conn = name, wf_id, [], {}
        self.col = 0

    def node(self, name, type_, params, pos=None, **extra):
        self.col += 1
        n = {"parameters": params, "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, self.name + name)), "name": name,
             "type": type_[0], "typeVersion": type_[1], "position": pos or [self.col * 240, 300]}
        if type_ is WEBHOOK:   # without webhookId n8n registers the path as <workflowId>/<node>/<path>
            n["webhookId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, self.name + name))
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, a, b, out=0):
        lst = self.conn.setdefault(a, {}).setdefault("main", [])
        while len(lst) <= out:
            lst.append([])
        lst[out].append({"node": b, "type": "main", "index": 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def save(self, fname: str, note: str):
        wf = {"id": self.id, "name": self.name, "nodes": self.nodes, "connections": self.conn, "active": False,
              "settings": {"executionOrder": "v1", "errorWorkflow": "docsync-wf-99"}, "meta": {"docsync": note}, "pinData": {}}
        (OUT / fname).write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", fname)


def engine_call(method: str, path_expr: str, body_expr: str | None = None) -> dict:
    p = {"method": method, "url": f"={{{{ $env.DOCSYNC_ENGINE_URL }}}}{path_expr}",
         "sendHeaders": True, "headerParameters": {"parameters": [{"name": "X-DocSync-Token", "value": "={{ $env.DOCSYNC_API_TOKEN || '' }}"}]},
         "options": {"timeout": 600000}}
    if body_expr is not None:
        p.update({"sendBody": True, "specifyBody": "json", "jsonBody": body_expr})
    return p


def notify(text_expr: str, level: str = "info", kind: str = "info", extra: str = "") -> dict:
    """POST to DOCSYNC_NOTIFY_URL: {text, level, kind, ...extra}. The engine fans it out to log / Slack / Telegram
    (a `proposed` notification with runId gets Approve/Reject buttons in Telegram). A Slack Incoming Webhook URL also works directly."""
    extra = (", " + extra) if extra else ""
    return {"method": "POST", "url": "={{ $env.DOCSYNC_NOTIFY_URL }}", "sendBody": True, "specifyBody": "json",
            "jsonBody": f"={{{{ JSON.stringify({{ text: {text_expr}, level: '{level}', kind: '{kind}', source: 'docsync'{extra} }}) }}}}",
            "options": {"timeout": 30000}}


def if_expr(expr: str) -> dict:
    return {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                           "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_OID, expr)), "leftValue": expr, "rightValue": "",
                                           "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                           "combinator": "and"}, "options": {}}


VERIFY_GITLAB = r"""// GitLab secret check and event filtering. Requires N8N_BLOCK_ENV_ACCESS_IN_NODE=false.
const item = $input.first().json;
const token = (item.headers || {})['x-gitlab-token'] || '';
if ($env.GITLAB_WEBHOOK_SECRET && token !== $env.GITLAB_WEBHOOK_SECRET) {
  throw new Error('DocSync: invalid X-Gitlab-Token');
}
const body = item.body || {};
%s
return [{ json: body }];"""
w = WF("DocSync 01 · Ingest (GitLab push → model proposal)", "docsync-wf-01")
w.node("GitLab push webhook", WEBHOOK, {"httpMethod": "POST", "path": "docsync/gitlab", "responseMode": "onReceived", "options": {}})
w.node("Verify & filter", CODE, {"jsCode": VERIFY_GITLAB % r"""const def = (body.project || {}).default_branch || 'main';
if (body.object_kind !== 'push' || body.ref !== `refs/heads/${def}`) return [];   // only merges to main are interesting"""})
w.node("Engine: ingest", HTTP, engine_call("POST", "/api/v1/ingest", "={{ JSON.stringify($json) }}"))
w.node("Proposed?", IF, if_expr("={{ ['PROPOSED','NEEDS_REVIEW'].includes($json.status) }}"))
w.node("Notify: review needed", HTTP, notify(
    r"""`DocSync: ${$json.service_id} - ${($json.changeset.events||[]).length} architecture change(s) (${$json.status}).\n`
      + `Model MR: ${$json.model_mr_url}\nPages: ${($json.preview.pages||[]).map(p => p.title).join('; ')}\n`
      + `Approve without GitLab: ${$json.preview.approveUrl}`""",
    kind="proposed", extra="runId: $json.run_id, service: $json.service_id, mrUrl: $json.model_mr_url, approveUrl: $json.preview.approveUrl"))
w.node("Failed?", IF, if_expr("={{ $json.status === 'FAILED' }}"), pos=[1200, 520])
w.node("Notify: failed", HTTP, notify(r"""`DocSync ingest ${$json.project_path}@${($json.merge_sha||'').slice(0,8)}: ${$json.error}`""", "error", "failed"), pos=[1440, 520])
w.chain("GitLab push webhook", "Verify & filter", "Engine: ingest", "Proposed?")
w.link("Proposed?", "Notify: review needed", 0)
w.link("Proposed?", "Failed?", 1)
w.link("Failed?", "Notify: failed", 0)
w.save("WF-01-ingest.json", "Push to main of a service → the engine extracts changes, opens an MR in architecture-model, notifies the analyst")
SUMMARIZE_JS = r"""// input: publish report (from Engine: publish or Unwrap publish)
const pub = $input.first().json || {};
if (pub.noop) return [{ json: { text: pub.text, runId: null } }];
const pages = (pub.pages || []).map(p => `${p.status} ${p.title}${p.url ? ' ' + p.url : ''}`).join('\n');
const conflicts = (pub.conflicts || []).map(c => ` ${c.title}: block ${c.blockId} ${c.status}`).join('\n');
return [{ json: { ...pub, text: `DocSync published architecture-model@${(pub.modelSha||'').slice(0,8)}${pub.dryRun ? ' (DRY RUN)' : ''}\n${pages}${conflicts ? '\n' + conflicts : ''}` } }];"""

w = WF("DocSync 02 · Publish (model merge / approve → Confluence)", "docsync-wf-02")
# --- path A: GitLab webhook of the model repository (MR merged / push to main)
w.node("GitLab model webhook", WEBHOOK, {"httpMethod": "POST", "path": "docsync/model", "responseMode": "onReceived", "options": {}}, pos=[240, 200])
w.node("Verify model event", CODE, {"jsCode": VERIFY_GITLAB % r"""const kind = body.object_kind;
const a = body.object_attributes || {};
if (kind === 'merge_request' && a.action !== 'merge') return [];
if (kind === 'push' && body.ref !== `refs/heads/${(body.project || {}).default_branch || 'main'}`) return [];
if (!['merge_request', 'push'].includes(kind)) return [];"""}, pos=[480, 200])
w.node("Engine: publish", HTTP, engine_call("POST", "/api/v1/publish", "={{ JSON.stringify($json) }}"), pos=[720, 200])
w.node("Summarize", CODE, {"jsCode": SUMMARIZE_JS}, pos=[960, 200])
# --- path B: approve link / Telegram button -> respond as soon as the publish report is ready
w.node("Approve webhook", WEBHOOK, {"httpMethod": "GET", "path": "docsync/approve", "responseMode": "responseNode", "options": {}}, pos=[240, 480])
w.node("Engine: approve", HTTP, engine_call("POST", "/api/v1/runs/{{ $json.query.run }}/approve"), pos=[480, 480])
w.node("Unwrap publish", CODE, {"jsCode": r"""const r = $input.first().json;
if (!r.publish) return [{ json: { noop: true, text: `ℹ ${r.status}: run is already ${r.run?.status}`, pages: [], conflicts: [] } }];
return [{ json: r.publish }];"""}, pos=[720, 480])
w.node("Summarize (approve)", CODE, {"jsCode": SUMMARIZE_JS}, pos=[960, 480])
w.node("Respond", RESPOND, {"respondWith": "json", "responseBody": "={{ JSON.stringify($json) }}", "options": {}}, pos=[1200, 480])
# --- shared tail: notify immediately, then the (slow) LLM audit as a separate message
w.node("Notify: published", HTTP, notify("$json.text", kind="published", extra="runId: $json.runId || null, modelSha: $json.modelSha || null"), pos=[1440, 340])
w.node("Has run?", IF, if_expr("={{ !!$('Notify: published').item.json.runId }}"), pos=[1680, 340])
w.node("Engine: audit", HTTP, engine_call("POST", "/api/v1/audit", "={{ JSON.stringify({ run_id: $('Notify: published').item.json.runId }) }}"),
       pos=[1920, 340], onError="continueRegularOutput")
w.node("Summarize audit", CODE, {"jsCode": r"""const a = $input.first().json || {};
if (a.status !== 'DONE') return [];
const open = (a.findings || []).filter(f => f.resolution === 'open');
const rest = (a.findings || []).length - open.length;
const text = open.length ? `DocSync audit: ${open.length} inline comment(s) on stale hand-written text\n` + open.map(f => `• ${f.reason}`).join('\n')
                         : `DocSync audit: hand-written sections look consistent (${rest} low-confidence finding(s) kept in the report)`;
return [{ json: { text, pagesChecked: a.pagesChecked } }];"""}, pos=[2160, 340])
w.node("Notify: audit", HTTP, notify("$json.text", kind="audit"), pos=[2400, 340])
w.chain("GitLab model webhook", "Verify model event", "Engine: publish", "Summarize", "Notify: published")
w.chain("Approve webhook", "Engine: approve", "Unwrap publish", "Summarize (approve)", "Respond", "Notify: published")
w.chain("Notify: published", "Has run?")
w.link("Has run?", "Engine: audit", 0)
w.chain("Engine: audit", "Summarize audit", "Notify: audit")
w.save("WF-02-publish.json", "Model MR merge (or approve link / Telegram button) → the engine generates, renders and publishes autoblocks; "
                            "the approve reply is sent right after publishing; the LLM audit follows as a separate notification")
w = WF("DocSync 03 · Reminders about unapproved runs", "docsync-wf-03")
w.node("Every weekday 10:00", ("n8n-nodes-base.scheduleTrigger", 1.2),
       {"rule": {"interval": [{"field": "cronExpression", "expression": "0 10 * * 1-5"}]}})
w.node("Engine: open runs", HTTP, {**engine_call("GET", "/api/v1/runs?status=PROPOSED"), "options": {"timeout": 30000}})
w.node("Overdue", CODE, {"jsCode": r"""const now = Date.now();
const runs = $input.all().map(i => i.json).flat().filter(r => r && r.run_id && now - Date.parse(r.proposed_at) > 24*3600*1000);
if (!runs.length) return [];
return [{ json: { text: '⏰ DocSync: waiting for approval for more than 1 day:\n' + runs.map(r => `• ${r.service_id} ${r.model_mr_url} (since ${r.proposed_at})`).join('\n') } }];"""})
w.node("Notify: reminder", HTTP, notify("$json.text", "warn", "reminder"))
w.chain("Every weekday 10:00", "Engine: open runs", "Overdue", "Notify: reminder")
w.save("WF-03-reminders.json", "Daily reminder to analysts about unapproved model MRs")
w = WF("DocSync 99 · Errors", "docsync-wf-99")
w.node("On error", ERROR, {})
w.node("Notify: error", HTTP, notify(r"""`DocSync ${$json.workflow.name}: ${$json.execution.error.message}\n${$json.execution.url || ''}`""", "error", "error"))
w.chain("On error", "Notify: error")
w.save("WF-99-errors.json", "Shared error handler of all DocSync workflows")
