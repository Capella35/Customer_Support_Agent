"""The pipeline's calls to the two A2A guards: the Security Judge (J-4) and the Data Masker (K-1).

Each returns a checked answer or raises; there is no default verdict and no unmasked fallback.
"""
import os

from support.a2a import A2AError, is_up, send

JUDGE_URL = os.environ.get("JUDGE_URL", "http://127.0.0.1:10002")
MASKER_URL = os.environ.get("MASKER_URL", "http://127.0.0.1:10003")


def judge_is_up() -> bool:
    return is_up(JUDGE_URL)


def masker_is_up() -> bool:
    return is_up(MASKER_URL)


async def ask_judge(message: str) -> dict:
    verdict = await send(JUDGE_URL, "Security Judge", [{"kind": "text", "text": message}])
    if verdict.get("verdict") not in ("allow", "block") or not isinstance(verdict.get("reason"), str):
        raise A2AError(f"unparseable verdict: {verdict!r}")
    return verdict


async def ask_masker(reply: str, user_email: str, user_message: str) -> dict:
    result = await send(MASKER_URL, "Data Masker", [
        {"kind": "text", "text": reply},
        {"kind": "data", "data": {"user_email": user_email, "user_message": user_message}},
    ])
    if not isinstance(result.get("masked_text"), str) or not isinstance(result.get("summary"), str):
        raise A2AError(f"unparseable masker reply: {str(result)[:200]}")
    return result
