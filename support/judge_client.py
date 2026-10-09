"""The pipeline's side of the A2A call to the Security Judge (J-4).

Returns the verdict or raises. There is no default verdict: a Judge that cannot be reached,
times out, or answers with anything but {"verdict": "allow" | "block", "reason"} is an error
the pipeline must end the turn on (P-4).
"""
import os
import uuid

import httpx

JUDGE_URL = os.environ.get("JUDGE_URL", "http://127.0.0.1:10002")
TIMEOUT_S = 30  # T-BUD-WALL: a Judge slower than the whole turn's budget has already failed it


class JudgeError(Exception):
    pass


def judge_is_up() -> bool:
    try:
        return httpx.get(f"{JUDGE_URL}/.well-known/agent-card.json", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


async def ask_judge(message: str) -> dict:
    request = {
        "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "message/send",
        "params": {"message": {"role": "user", "messageId": uuid.uuid4().hex,
                               "parts": [{"kind": "text", "text": message}]}},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(f"{JUDGE_URL}/", json=request)
        body = response.json()
    except httpx.TimeoutException:
        raise JudgeError(f"Security Judge timed out after {TIMEOUT_S} s") from None
    except httpx.HTTPError as exc:
        raise JudgeError(f"Security Judge unreachable: {exc}") from None
    except ValueError:
        raise JudgeError(f"Security Judge sent non-JSON (HTTP {response.status_code})") from None
    if "error" in body:
        raise JudgeError(f"Security Judge error: {body['error'].get('message')}")
    try:
        (part,) = [p for p in body["result"]["parts"] if p.get("kind") == "data"]
        verdict = part["data"]
    except (KeyError, TypeError, ValueError):
        raise JudgeError("Security Judge answered without a verdict data part") from None
    if verdict.get("verdict") not in ("allow", "block") or not isinstance(verdict.get("reason"), str):
        raise JudgeError(f"unparseable verdict: {verdict!r}")
    return verdict
