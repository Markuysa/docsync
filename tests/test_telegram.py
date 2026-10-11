"""Telegram approvals with a fake Bot API: buttons, chat/user guards, approve via n8n URL or directly, reject, /pending."""
import pytest

from docsync import render
from docsync.notify import Notifier
from docsync.pipeline import Pipeline
from docsync.telegram import TelegramBot


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            return {"message_id": len(self.calls), "text": params["text"]}
        return {}


@pytest.fixture
def pl(env, monkeypatch):
    from docsync.config import settings
    p = Pipeline(settings())
    if not p.kroki.healthy():
        monkeypatch.setattr(render.Kroki, "healthy", lambda self: True)
        monkeypatch.setattr(render.Kroki, "render", lambda self, kind, src, options=None, fmt="svg": b"<svg/>")
    p.bootstrap_pages()
    return p


def make_bot(pl, approve_url="", allowed=()):
    bot = TelegramBot("token", "-100", set(allowed), approve_url, lambda: pl)
    bot.api = FakeApi()
    bot.async_callbacks = False
    return bot


def callback(run_id, action="approve", chat="-100", username="analyst"):
    return {"update_id": 1, "callback_query": {"id": "cq1", "data": f"{action}:{run_id}", "from": {"id": 7, "username": username},
                                               "message": {"message_id": 42, "chat": {"id": int(chat)}, "text": "proposal"}}}


def test_proposed_notification_has_buttons(pl, env):
    bot = make_bot(pl)
    n = Notifier(pl.s, bot)
    out = n.send({"text": "DocSync: orders-service", "kind": "proposed", "runId": "r1", "mrUrl": "https://gitlab/mr/1"})
    method, params = bot.api.calls[-1]
    assert method == "sendMessage" and out["telegram"] == 1
    kb = params["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == "approve:r1" and kb[0][1]["callback_data"] == "reject:r1" and kb[1][0]["url"] == "https://gitlab/mr/1"
    n.send({"text": "published", "kind": "published"})
    assert "reply_markup" not in bot.api.calls[-1][1]


def test_approve_button_publishes(pl, env):
    run = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    bot = make_bot(pl)
    res = bot.handle_update(callback(run["run_id"]))
    assert res["action"] == "approve" and res["by"] == "@analyst"
    assert pl.journal.get_run(run["run_id"])["status"] == "PUBLISHED"
    methods = [m for m, _ in bot.api.calls]
    assert methods[:2] == ["answerCallbackQuery", "editMessageText"] and methods[-1] == "sendMessage"
    assert "Approving by @analyst" in bot.api.calls[1][1]["text"]          # instant freeze
    assert "Approved by @analyst" in bot.api.calls[-2][1]["text"]          # final verdict
    # a second click on the same run is a no-op with a toast, no error, no second publish
    res2 = bot.handle_update(callback(run["run_id"]))
    assert res2 == {"ignored": "PUBLISHED"}
    assert pl.approve(run["run_id"])["status"] == "ALREADY_APPROVED"


def test_approve_goes_through_n8n_when_configured(pl, env, monkeypatch):
    import httpx
    run = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    seen = {}

    real_get = httpx.get

    def fake_get(url, timeout=0):
        if "approve" not in url:                 # Kroki health check etc.
            return real_get(url, timeout=timeout)
        seen["url"] = url
        pl.approve(run["run_id"])
        return httpx.Response(200, json={"text": "published via n8n"})

    monkeypatch.setattr(httpx, "get", fake_get)
    bot = make_bot(pl, approve_url="http://n8n:5678/webhook/docsync/approve?run={runId}")
    bot.handle_update(callback(run["run_id"]))
    assert seen["url"].endswith(run["run_id"]) and pl.journal.get_run(run["run_id"])["status"] == "PUBLISHED"
    # n8n delivers the report itself: the bot must not post it a second time
    assert [m for m, _ in bot.api.calls] == ["answerCallbackQuery", "editMessageText", "editMessageText"]
    assert "Approved by @analyst" in bot.api.calls[-1][1]["text"]


def test_reject_and_guards(pl, env):
    run = pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    bot = make_bot(pl, allowed=("@lead",))
    assert bot.handle_update(callback(run["run_id"], username="intruder")) == {"ignored": "user"}
    assert bot.handle_update(callback(run["run_id"], chat="-999", username="lead")) == {"ignored": "chat"}
    assert pl.journal.get_run(run["run_id"])["status"] == "PROPOSED"
    res = bot.handle_update(callback(run["run_id"], action="reject", username="lead"))
    assert res["action"] == "reject" and pl.journal.get_run(run["run_id"])["status"] == "REJECTED"


def test_pending_command(pl, env):
    pl.ingest("payments/orders-service", env["commits"]["v1"], env["commits"]["v2"])
    bot = make_bot(pl)
    res = bot.handle_update({"update_id": 2, "message": {"chat": {"id": -100}, "text": "/pending@docsync_bot"}})
    assert res == {"pending": 1} and bot.api.calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve"
