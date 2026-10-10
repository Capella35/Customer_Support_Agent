"""The web front end: SPEC §7.2 routes on :8000 and the single-page UI (W-1).

  python -m support.web

POST /api/chat streams the very events the CLI prints with --events, from the same
pipeline.run_turn, as application/x-ndjson (P-1, W-2). Like the CLI, it holds no pipeline logic.
"""
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from toolbox_core import ToolboxClient

from guards.sanitizer import MAX_CHARS
from support import memory, telemetry
from support.agent import MODEL, TOOLBOX_URL, build_runner, login
from support.cli import toolbox_is_up
from support.guard_clients import judge_is_up, masker_is_up
from support.pipeline import run_turn

HOST, PORT = "127.0.0.1", 8000
PAGE = Path(__file__).resolve().parent / "static" / "index.html"

state: dict = {"toolbox": None, "sessions": {}}  # user_id -> {"runner", "session_id", "full_name"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    telemetry.setup()  # before any agent is built, so ADK's spans join each turn's trace
    async with ToolboxClient(TOOLBOX_URL) as client:
        state["toolbox"] = client
        yield


app = FastAPI(title="Customer support", lifespan=lifespan)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.get("/")
async def page() -> FileResponse:
    return FileResponse(PAGE, media_type="text/html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    email, password = str(body.get("email", "")).strip(), str(body.get("password", ""))
    user = await login(state["toolbox"], email, password) if email and password else None
    if user is None:
        return error(401, "Invalid email or password.")
    runner, session_id = await build_runner(state["toolbox"], user["email"])
    state["sessions"][user["email"]] = {"runner": runner, "session_id": session_id}
    return {"user_id": user["email"], "full_name": user["full_name"],
            "is_premium": bool(user.get("is_premium_customer"))}


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.json()
    user_id, message = str(body.get("user_id", "")), str(body.get("message", ""))
    if not message.strip():
        return error(400, "message is empty")
    session = state["sessions"].get(user_id)
    if session is None:
        return error(401, "Not logged in.")
    if len(message) > MAX_CHARS:
        return error(413, "message too long")  # before streaming, as SPEC §7.2 asks

    async def ndjson():
        async for event in run_turn(session["runner"], session["session_id"], user_id, message):
            yield json.dumps(event) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/logout")
async def api_logout(request: Request):
    body = await request.json()
    state["sessions"].pop(str(body.get("user_id", "")), None)
    return {"ok": True}


def _port_open(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=2):
            return "ok"
    except OSError as exc:
        return f"unreachable: {exc}"


@app.get("/health")
async def health():
    checks = {
        "status": "ok", "model": MODEL,
        "db": _port_open("127.0.0.1", 5432),
        "toolbox": "ok" if toolbox_is_up() else f"unreachable at {TOOLBOX_URL}",
        "judge": "ok" if judge_is_up() else "unreachable",
        "masker": "ok" if masker_is_up() else "unreachable",
        "mem0": await memory.mem0_ok(),
        "phoenix": "ok" if telemetry.phoenix_is_up() else "unreachable",
    }
    failing = [k for k, v in checks.items() if k not in ("status", "model") and v != "ok"]
    if failing:
        checks["status"] = "degraded: " + ", ".join(failing)
    return JSONResponse(checks, status_code=503 if failing else 200)


if __name__ == "__main__":
    load_dotenv()
    uvicorn.run(app, host=HOST, port=int(os.environ.get("WEB_PORT", PORT)), log_level="warning")
