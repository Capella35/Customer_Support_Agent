"""Memory through Mem0 cloud, as pipeline steps only: the agent has no memory tool (R-1, R-6).

recall() searches this customer's memories with their message and decides, for every candidate,
whether it may reach the agent (R-2, R-5). save() stores the customer's message and nothing else
(R-4); the pipeline calls it only after the Masker, on turns that were not blocked.
"""
import os

from mem0 import AsyncMemoryClient

TOP_K = 5             # T-MEM-TOPK
# T-MEM-MINSCORE is 0.25. Lowered to 0.20 (the student's Stage 8 decision, with evidence in
# BUILD_LOG): diana's planted "back door" preference scored 0.2487 on the question that needed
# it, while the unrelated "works from home" scored 0.1208. Reports keep the original 0.25 gate.
MIN_SCORE = 0.20
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
    found = await _mem0().search(message, filters={"user_id": user}, top_k=TOP_K)
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
