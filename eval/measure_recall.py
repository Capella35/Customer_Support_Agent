"""Measure Mem0 search options on the memory pairs before changing recall (Stage 10, decision 2b).

Read-only: it only searches. For each pair's ask, it searches the user's current memories with
each option set and prints the keyword memory's score and rank against the best other memory.
A cutoff can separate them only when the right one scores above every wrong one, on every pair.

  python -m eval.measure_recall
"""

import asyncio
import os

from dotenv import load_dotenv
from mem0 import AsyncMemoryClient

from eval import drive
from support.memory import TOP_K

VARIANTS: dict[str, dict] = {
    "plain": {},
    "rerank": {"rerank": True},
    "keyword": {"keyword_search": True},
    "rerank+keyword": {"rerank": True, "keyword_search": True},
}


async def main() -> None:
    load_dotenv(drive.ROOT / ".env")
    client = AsyncMemoryClient(api_key=os.environ["MEM0_API_KEY"])
    people = drive.users()
    print(f"{'pair':<5}{'variant':<16}{'right':>8}{'rank':>6}{'best wrong':>12}  gap")
    for pair in drive.gold("memory"):
        email = people[pair["user"]][0]
        for name, opts in VARIANTS.items():
            found = await client.search(pair["ask"], filters={"user_id": email}, top_k=TOP_K, **opts)
            results = found.get("results", [])
            right = [
                (i + 1, float(m.get("score") or 0))
                for i, m in enumerate(results)
                if pair["keyword"].lower() in (m.get("memory") or "").lower()
            ]
            wrong = [
                float(m.get("score") or 0)
                for m in results
                if pair["keyword"].lower() not in (m.get("memory") or "").lower()
            ]
            rank, score = right[0] if right else (None, None)
            best_wrong = max(wrong) if wrong else None
            gap = f"{score - best_wrong:+.4f}" if score is not None and best_wrong is not None else ""
            print(
                f"{pair['id']:<5}{name:<16}"
                f"{'-' if score is None else f'{score:.4f}':>8}{rank or '-':>6}"
                f"{'-' if best_wrong is None else f'{best_wrong:.4f}':>12}  {gap}"
            )
        print()


if __name__ == "__main__":
    asyncio.run(main())
