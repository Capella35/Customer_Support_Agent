"""Memory through Mem0 cloud, as pipeline steps only: the agent has no memory tool (R-1, R-6).

recall() searches this customer's memories with their message and decides, for every candidate,
whether it may reach the agent (R-2, R-5). save() stores the customer's message and nothing else
(R-4); the pipeline calls it only after the Masker, on turns that were not blocked.
"""
import os

import httpx
from mem0 import AsyncMemoryClient

TOP_K = 5             # T-MEM-TOPK
# T-MEM-MINSCORE is 0.25. Stage 8 lowered it to 0.20; Stage 10's eval showed plain scores
# overlap (right memories as low as 0.1731, wrong ones up to 0.2758), so no cutoff separates
# them. With Mem0's rerank, eval/measure_recall.py gave right >= 0.5471 and wrong <= 0.4153
# (one related delivery note at 0.5679). 0.48 is the student's Stage 10 choice from that gap.
MIN_SCORE = 0.48
RERANK = True
MAX_CHARS = 500       # T-MEM-MAXCHARS
HEADER = "Relevant memories about this customer (from Mem0):"  # R-3

_client: AsyncMemoryClient | None = None


def _mem0() -> AsyncMemoryClient:
    global _client
    if _client is None:
        _client = AsyncMemoryClient(api_key=os.environ["MEM0_API_KEY"])
    return _client


async def recall(user: str, message: str) -> list[dict]:
    """Every candidate with its score, whether it was inserted, and why not (R-5)."""
    found = await _mem0().search(message, filters={"user_id": user}, top_k=TOP_K, rerank=RERANK)
    candidates = []
    for m in found.get("results", []):
        text, score = m.get("memory") or "", float(m.get("score") or 0.0)
        if score < MIN_SCORE:
            reason = f"below cutoff ({score:.4f} < {MIN_SCORE})"
        elif len(text) > MAX_CHARS:
            reason = f"too long ({len(text)} > {MAX_CHARS} chars)"
        else:
            reason = None
        candidates.append({"memory": text, "score": round(score, 4),
                           "inserted": reason is None, "reason": reason})
    return candidates


def with_memories(message: str, candidates: list[dict]) -> str:
    """The agent's input: inserted memories on top under a fixed header, then the message (R-3)."""
    kept = [c["memory"] for c in candidates if c["inserted"]]
    if not kept:
        return message
    lines = "\n".join(f"- {m}" for m in kept)
    return f"{HEADER}\n{lines}\n\nCustomer message: {message}"


async def save(user: str, message: str) -> str:
    """Store the customer's message only, never the reply (R-4). Mem0 extracts facts later."""
    result = await _mem0().add([{"role": "user", "content": message}], user_id=user)
    return str(result.get("status", "sent"))


async def mem0_ok() -> str:
    """For /health: Mem0's ping checks the key without reading or writing any memory."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.mem0.ai/v1/ping/",
                                 headers={"Authorization": f"Token {os.environ['MEM0_API_KEY']}"})
        return "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
    except (httpx.HTTPError, KeyError) as exc:
        return f"{type(exc).__name__}: {exc}"
