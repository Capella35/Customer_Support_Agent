"""The pipeline's side of A2A: one message/send call that returns the reply's data part, or raises.

Used for the Security Judge and the Data Masker. There is no default answer: a service that
cannot be reached, times out, returns a JSON-RPC error, or replies without exactly one data
part raises A2AError, and the pipeline ends the turn naming that step (P-4).
"""
import uuid

import httpx

TIMEOUT_S = 30  # T-BUD-WALL: a guard slower than the whole turn's budget has already failed it


class A2AError(Exception):
    pass


def is_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/.well-known/agent-card.json", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


async def send(url: str, service: str, parts: list[dict]) -> dict:
    request = {
        "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
        "params": {"message": {"role": "user", "messageId": uuid.uuid4().hex, "parts": parts}},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(f"{url}/", json=request)
        body = response.json()
    except httpx.TimeoutException:
        raise A2AError(f"{service} timed out after {TIMEOUT_S} s") from None
    except httpx.HTTPError as exc:
        raise A2AError(f"{service} unreachable: {exc}") from None
    except ValueError:
        raise A2AError(f"{service} sent non-JSON (HTTP {response.status_code})") from None
    if "error" in body:
        raise A2AError(f"{service} error: {body['error'].get('message')}")
    try:
        (part,) = [p for p in body["result"]["parts"] if p.get("kind") == "data"]
        return part["data"]
    except (KeyError, TypeError, ValueError):
        raise A2AError(f"{service} answered without a data part") from None
