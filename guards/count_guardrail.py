"""Count the Guardrail's decisions on the legitimate and off-topic gold sets (Stage 7 Prove it).

  python -m guards.count_guardrail

Calls guardrail.check directly, so the numbers are the Guardrail's alone: no Sanitizer, no
Judge, no agent. Each answer is saved to runs/guardrail_<version>.jsonl as it arrives, and a
rerun skips the items already answered: on the free tier (20 requests per model per day) one
count can take more than a day. Errors are shown and retried on the next run, never counted as
a pass. Calls are spaced 12.5 s apart (5 requests per minute per model).
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from guards import guardrail

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "gold"
RESULTS = ROOT / "runs" / f"guardrail_{guardrail.VERSION}.jsonl"
PACE_S = 12.5
SETS = {"legit": "unsafe", "offtopic": "safe"}  # set -> the decision that counts as wrong


def items(name: str) -> list[dict]:
    return [json.loads(line) for line in (GOLD / f"{name}.jsonl").read_text().splitlines()]


def saved() -> dict[str, dict]:
    if not RESULTS.exists():
        return {}
    return {r["id"]: r for r in map(json.loads, RESULTS.read_text().splitlines())}


async def main() -> None:
    load_dotenv()
    logging.getLogger("google_adk").setLevel(logging.CRITICAL)
    RESULTS.parent.mkdir(exist_ok=True)
    done = saved()
    print(f"Guardrail prompt {guardrail.VERSION}: {len(done)} answers already saved in {RESULTS.name}\n")
    last = 0.0
    for name in SETS:
        for item in items(name):
            if item["id"] in done:
                continue
            await asyncio.sleep(max(0.0, last + PACE_S - time.monotonic()))
            last = time.monotonic()
            try:
                d = await guardrail.check(item["message"])
            except Exception as exc:
                reason = str(exc).strip().splitlines()[-1][:100] if str(exc).strip() else ""
                print(f"{item['id']}  ERROR  {type(exc).__name__}: {reason}  (rerun later)")
                if "every model in the pool failed" in str(exc):
                    print("\nAll models are out of quota for today; rerun tomorrow to continue.")
                    break
                continue
            done[item["id"]] = {"id": item["id"], "set": name, **d}
            with RESULTS.open("a") as f:
                f.write(json.dumps(done[item["id"]]) + "\n")
            mark = "WRONG" if d["decision"] == SETS[name] else "ok   "
            print(f"{item['id']}  {mark} {d['decision']:<6} {item['message'][:50]:<50}  "
                  f"[{d['model']}]  {d['reasoning']}")
        else:
            continue
        break

    for name, bad in SETS.items():
        answered = [r for r in done.values() if r["set"] == name]
        wrong = sum(r["decision"] == bad for r in answered)
        total = len(items(name))
        label = "legit false blocks" if name == "legit" else "off-topic blocked "
        value = wrong if name == "legit" else len(answered) - wrong
        print(f"{label}: {value}/{len(answered)} answered of {total}")


if __name__ == "__main__":
    asyncio.run(main())
