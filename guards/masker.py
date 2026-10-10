"""The Data Masker: its own process on :10003, spoken to over A2A JSON-RPC like the Judge (K-1).

  python -m guards.masker

message/send takes the agent's final reply as a text part, and a data part with the logged-in
customer's email and this turn's message {"user_email": ..., "user_message": ...}. It answers
with one data part: {"masked_text": ..., "counts": {...}, "summary": "..."}.

What counts as PII here (the student's Stage 8 decision): emails other than the customer's own,
phone numbers other than one the customer typed in this turn's message, and card-like numbers.
Addresses are left alone. Deterministic, no model call: only the matched span is replaced, so
case and whitespace elsewhere never change (K-2, R-8).
"""
import re
import uuid

import uvicorn
from fastapi import FastAPI, Request

HOST, PORT = "127.0.0.1", 10003

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")              # 13-19 digits, spaces or dashes
PHONE = re.compile(r"(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\b\d{3})[ .-]?\d{3}[ .-]\d{4}\b"
                   r"|\b\d{3}-\d{4}\b")                      # 555-123-4567, (555) 123-4567, 555-0100
NOUN = {"email": ("email address", "email addresses"), "phone": ("phone number", "phone numbers"),
        "card": ("card number", "card numbers")}


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def mask(text: str, user_email: str, user_message: str = "") -> dict:
    counts = {"email": 0, "phone": 0, "card": 0}
    # A phone number the customer just gave is theirs to see again; any other is masked.
    own_phones = {_digits(m.group(0)) for m in PHONE.finditer(user_message)}

    def replace(kind: str):
        def sub(match: re.Match) -> str:
            if kind == "email" and match.group(0).lower() == user_email.lower():
                return match.group(0)                         # the customer's own email stays
            if kind == "phone" and _digits(match.group(0)) in own_phones:
                return match.group(0)                         # a number they gave this turn stays
            counts[kind] += 1
            return f"[{kind}]"
        return sub

    # Cards before phones, so a card number is never half-matched as a phone number.
    for kind, pattern in (("email", EMAIL), ("card", CARD), ("phone", PHONE)):
        text = pattern.sub(replace(kind), text)
    parts = [f"{n} {NOUN[k][n != 1]}" for k, n in counts.items() if n]
    return {"masked_text": text, "counts": counts,
            "summary": "masked " + ", ".join(parts) if parts else "nothing to mask"}


AGENT_CARD = {
    "protocolVersion": "0.3.0",
    "name": "Data Masker",
    "description": "Masks other people's emails, phone numbers and card numbers in a support reply.",
    "url": f"http://{HOST}:{PORT}/",
    "preferredTransport": "JSONRPC",
    "version": "1.0.0",
    "capabilities": {"streaming": False},
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [{
        "id": "mask-reply", "name": "Mask a reply",
        "description": 'Returns {"masked_text", "counts", "summary"} as a data part.',
        "tags": ["privacy", "guard"],
    }],
}

app = FastAPI(title="Data Masker")


@app.get("/.well-known/agent-card.json")
@app.get("/.well-known/agent.json")
async def agent_card() -> dict:
    return AGENT_CARD


@app.post("/")
async def jsonrpc(request: Request) -> dict:
    def error(code: int, message: str, req_id=None) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    try:
        req = await request.json()
    except ValueError:
        return error(-32700, "parse error")
    req_id = req.get("id")
    if req.get("method") != "message/send":
        return error(-32601, f"method not found: {req.get('method')}", req_id)
    parts = (req.get("params") or {}).get("message", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if p.get("kind") == "text")
    data: dict = next((p["data"] for p in parts if p.get("kind") == "data"), {})
    if not data.get("user_email"):
        return error(-32602, "missing data part with user_email", req_id)
    return {"jsonrpc": "2.0", "id": req_id, "result": {
        "kind": "message", "messageId": uuid.uuid4().hex, "role": "agent",
        "parts": [{"kind": "data",
                   "data": mask(text, data["user_email"], data.get("user_message", ""))}],
    }}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
