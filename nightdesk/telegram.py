"""Telegram Bot API over plain HTTPS: approval prompts with buttons, and a few commands."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

ApproveHandler = Callable[[int, bool], Awaitable[str]]
CommandHandler = Callable[[str], Awaitable[str]]


def parse_callback(data: str) -> tuple[int, bool] | None:
    """'yes:12' -> (12, True); 'no:12' -> (12, False); anything else -> None."""
    try:
        verb, pid = data.split(":", 1)
        if verb not in ("yes", "no"):
            return None
        return int(pid), verb == "yes"
    except ValueError:
        return None


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.enabled = bool(token and chat_id)
        self.chat_id = str(chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.AsyncClient(timeout=40)
        self._offset = 0

    async def _call(self, method: str, **payload: Any) -> Any:
        resp = await self._http.post(f"{self._base}/{method}", json=payload)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]

    async def send(self, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        if not self.enabled:
            return None
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            msg = await self._call("sendMessage", **payload)
            return int(msg["message_id"])
        except Exception as e:  # never let a notification failure stop the desk
            log.error("Telegram send failed: %s", e)
            return None

    async def ask(self, pid: int, text: str) -> int | None:
        return await self.send(text, [[
            {"text": "Approve", "callback_data": f"yes:{pid}"},
            {"text": "Reject", "callback_data": f"no:{pid}"},
        ]])

    async def resolve(self, message_id: int | None, text: str) -> None:
        """Replace an approval prompt with its outcome, removing the buttons."""
        if not self.enabled or not message_id:
            return
        try:
            await self._call("editMessageText", chat_id=self.chat_id, message_id=message_id,
                             text=text, parse_mode="HTML")
        except Exception as e:
            log.warning("Telegram edit failed: %s", e)

    async def poll(self, on_decision: ApproveHandler, on_command: CommandHandler) -> None:
        """Long-poll for button taps and commands. Only the configured chat is obeyed."""
        if not self.enabled:
            return
        while True:
            try:
                updates = await self._call("getUpdates", offset=self._offset, timeout=30,
                                           allowed_updates=["message", "callback_query"])
            except Exception as e:
                log.warning("Telegram poll error: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                try:
                    await self._handle(u, on_decision, on_command)
                except Exception:
                    log.exception("Telegram update failed")

    async def _handle(self, u: dict, on_decision: ApproveHandler, on_command: CommandHandler) -> None:
        if cq := u.get("callback_query"):
            chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if chat != self.chat_id:
                return
            parsed = parse_callback(cq.get("data", ""))
            reply = "Unknown button" if parsed is None else await on_decision(*parsed)
            await self._call("answerCallbackQuery", callback_query_id=cq["id"], text=reply[:190])
        elif msg := u.get("message"):
            if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                return
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                await self.send(await on_command(text.split()[0].split("@")[0].lower()))

    async def close(self) -> None:
        await self._http.aclose()


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)
