"""Telegram bot: proposal messages with Approve/Reject buttons, long polling of the Bot API,
/pending and /status commands. Approvals go through the n8n approve webhook."""
from __future__ import annotations

import html
import logging
import threading
import time

import httpx

log = logging.getLogger("docsync.telegram")


class TelegramBot:
    def __init__(self, token: str, chat_id: str, allowed_users: set[str], approve_url: str, pipeline_factory, public_url: str = ""):
        self.token = token
        self.chat_id = str(chat_id)
        self.allowed_users = {u.lstrip("@").lower() for u in allowed_users if u}
        self.approve_url = approve_url              # template with {runId}; empty -> approve directly in the engine
        self.pipeline_factory = pipeline_factory    # callable -> Pipeline
        self.public_url = public_url
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=40)
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self.async_callbacks = True     # process button presses in a worker thread (tests set False)
    def api(self, method: str, **params) -> dict:
        r = self.http.post(f"/{method}", json=params)
        data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {r.status_code} {data.get('description', r.text[:200])}")
        return data["result"]

    def send(self, html_text: str, buttons: list[list[dict]] | None = None, chat_id: str | None = None) -> dict:
        params = {"chat_id": chat_id or self.chat_id, "text": html_text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.api("sendMessage", **params)

    @staticmethod
    def approval_keyboard(run_id: str, mr_url: str | None = None) -> list[list[dict]]:
        row = [{"text": "Approve", "callback_data": f"approve:{run_id}"}, {"text": "Reject", "callback_data": f"reject:{run_id}"}]
        rows = [row]
        if mr_url and mr_url.startswith("http"):
            rows.append([{"text": "Open MR", "url": mr_url}])
        return rows
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self.poll_forever, name="telegram-poller", daemon=True)
        self._thread.start()
        log.info("telegram poller started for chat %s", self.chat_id)

    def stop(self) -> None:
        self._stop.set()

    def poll_forever(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self.api("getUpdates", offset=self._offset, timeout=25, allowed_updates=["callback_query", "message"])
                for u in updates:
                    self._offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:
                        log.exception("telegram update failed")
            except Exception as e:
                log.warning("telegram polling error: %s", e)
                time.sleep(10)
    def _allowed(self, user: dict) -> bool:
        if not self.allowed_users:
            return True
        return (user.get("username") or "").lower() in self.allowed_users or str(user.get("id")) in self.allowed_users

    def handle_update(self, u: dict) -> dict | None:
        if "callback_query" in u:
            return self._on_callback(u["callback_query"])
        if "message" in u:
            return self._on_message(u["message"])
        return None

    def _on_message(self, m: dict) -> dict | None:
        if str((m.get("chat") or {}).get("id")) != self.chat_id:
            return None
        text = (m.get("text") or "").strip().split("@")[0]
        if text == "/pending":
            runs = [r for r in self.pipeline_factory().journal.list_runs() if r["status"] in ("PROPOSED", "NEEDS_REVIEW")]
            if not runs:
                return self.send("Nothing is waiting for approval.")
            for r in runs:
                body = (f" <b>{html.escape(r['service_id'])}</b> - {r['status']} (since {r.get('proposed_at')})\n"
                        f"MR: {html.escape(r.get('model_mr_url') or '-')}")
                self.send(body, self.approval_keyboard(r["run_id"], r.get("model_mr_url")))
            return {"pending": len(runs)}
        if text == "/status":
            k = self.pipeline_factory().journal.kpi()
            return self.send(f"DocSync is up. Runs by status: <code>{html.escape(str(k['runsByStatus']))}</code>")
        return None

    def _on_callback(self, cq: dict) -> dict:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id"))
        user = cq.get("from") or {}
        who = "@" + user["username"] if user.get("username") else user.get("first_name", "someone")
        if chat_id != self.chat_id:
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="This chat is not allowed")
            return {"ignored": "chat"}
        if not self._allowed(user):
            self.api("answerCallbackQuery", callback_query_id=cq["id"], text="You are not allowed to approve", show_alert=True)
            return {"ignored": "user"}
        action, _, run_id = data.partition(":")
        if action not in ("approve", "reject") or not run_id:
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
            return {"ignored": "data"}

        # --- instant feedback + dedupe: the first click freezes the message, later clicks only get a toast
        with self._lock:
            if run_id in self._inflight:
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Already in progress - please wait")
                return {"ignored": "inflight"}
            run = self.pipeline_factory().journal.get_run(run_id)
            if not run:
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Unknown run", show_alert=True)
                return {"ignored": "unknown"}
            if run["status"] not in ("PROPOSED", "NEEDS_REVIEW"):
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text=f"Already {run['status']}")
                self._freeze(chat_id, msg, f"ℹ Already {run['status']}")
                return {"ignored": run["status"]}
            self._inflight.add(run_id)
        self.api("answerCallbackQuery", callback_query_id=cq["id"], text="Accepted" if action == "approve" else "Rejecting")
        self._freeze(chat_id, msg, f"⏳ {'Approving' if action == 'approve' else 'Rejecting'} by {html.escape(who)}…")

        if self.async_callbacks:
            threading.Thread(target=self._process, args=(action, run_id, chat_id, msg, who), daemon=True).start()
            return {"action": action, "runId": run_id, "by": who, "async": True}
        return self._process(action, run_id, chat_id, msg, who)

    def _process(self, action: str, run_id: str, chat_id: str, msg: dict, who: str) -> dict:
        t0 = time.time()
        try:
            if action == "approve":
                result = self._approve(run_id)
                verdict = f"Approved by {html.escape(who)}"
            else:
                self.pipeline_factory().reject(run_id)
                result = {"text": "Run rejected, nothing published."}
                verdict = f"Rejected by {html.escape(who)}"
        except Exception as e:
            result, verdict = {"text": f"Error: {e}", "error": True}, f"Failed ({html.escape(who)})"
        finally:
            with self._lock:
                self._inflight.discard(run_id)
        self._freeze(chat_id, msg, f"{verdict} · {time.time() - t0:.0f}s")
        # when the approval went through n8n, n8n already sends the publish report to every sink -
        # posting it here again would duplicate it; only errors / direct-mode results are posted by the bot
        if result.get("text") and not (action == "approve" and self.approve_url and not result.get("error")):
            self.send(html.escape(str(result["text"]))[:3500])
        return {"action": action, "runId": run_id, "by": who}

    def _freeze(self, chat_id: str, msg: dict, status_line: str) -> None:
        """Remove the buttons and append a status line to the original proposal message."""
        base = (msg.get("text") or "").split("\n\n⏳")[0].split("\n\nℹ")[0]
        try:
            self.api("editMessageText", chat_id=chat_id, message_id=msg.get("message_id"),
                     text=(html.escape(base) + f"\n\n{status_line}")[:4000], parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.warning("editMessageText failed: %s", e)

    def _approve(self, run_id: str) -> dict:
        """Through n8n (approve webhook) when configured, so that audit + notifications run as usual."""
        if self.approve_url:
            r = httpx.get(self.approve_url.format(runId=run_id), timeout=600)
            try:
                data = r.json()
            except ValueError:
                data = {"text": r.text[:500]}
            if r.status_code >= 300:
                raise RuntimeError(data.get("error") or data.get("message") or f"HTTP {r.status_code}")
            return data if isinstance(data, dict) else {"text": str(data)}
        res = self.pipeline_factory().approve(run_id)
        pub = res.get("publish")
        if not pub:
            return {"text": f"ℹ {res.get('status')}: run is already {res['run']['status']}"}
        pages = "\n".join(f"{p['status']} {p['title']}" for p in pub["pages"])
        return {"text": f"Published architecture-model@{pub['modelSha'][:8]}\n{pages}"}
