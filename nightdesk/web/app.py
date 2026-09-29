"""Dashboard server: the pixel trading floor page plus a small token-protected JSON API."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from ..desk import Desk

PAGE = Path(__file__).with_name("floor.html")


def create_app(desk: Desk, token: str) -> FastAPI:
    app = FastAPI(title="Night Desk", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(x_desk_token: str | None) -> None:
        if not x_desk_token or not hmac.compare_digest(x_desk_token, token):
            raise HTTPException(status_code=401, detail="Wrong or missing dashboard token.")

    @app.get("/", response_class=HTMLResponse)
    async def floor() -> str:
        return PAGE.read_text()

    @app.get("/api/state")
    async def state(x_desk_token: str | None = Header(default=None)) -> dict:
        auth(x_desk_token)
        return desk.snapshot()

    @app.post("/api/proposals/{pid}/{verdict}")
    async def decide(pid: int, verdict: str, x_desk_token: str | None = Header(default=None)) -> dict:
        auth(x_desk_token)
        if verdict not in ("approve", "reject"):
            raise HTTPException(status_code=404)
        return {"message": await desk.decide(pid, verdict == "approve")}

    @app.post("/api/{action}")
    async def control(action: str, x_desk_token: str | None = Header(default=None)) -> dict:
        auth(x_desk_token)
        if action not in ("pause", "resume"):
            raise HTTPException(status_code=404)
        return {"message": await desk.command(f"/{action}")}

    @app.get("/healthz")
    async def health() -> dict:
        return {"ok": True}

    return app
