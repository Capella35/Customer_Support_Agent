"""Run with: python -m guards.test_patterns   (no model, no network)

Shows which attack-set items the patterns alone catch, and fails if any non-attack gold item
(legitimate, off-topic, probe, order, action, memory) matches a pattern.
"""
import json
import sys
from pathlib import Path

from guards.patterns import check_patterns
from guards.sanitizer import sanitize

GOLD = Path(__file__).resolve().parent.parent / "eval" / "gold"


def texts(name: str):
    for line in (GOLD / f"{name}.jsonl").read_text().splitlines():
        item = json.loads(line)
        for key in ("message", "plant", "ask"):
            if key in item:
                yield item["id"], item[key]


def verdict(text: str) -> str | None:
    blocked, reason = sanitize(text)
    if blocked:
        return f"sanitize: {reason}"
    found = check_patterns(text)
    return f"judge patterns: {found['kind']} ({found['matched']!r})" if found["match"] else None


caught = 0
for item_id, text in texts("attack"):
    hit = verdict(text)
    caught += hit is not None
    print(f"{item_id}  {'BLOCK' if hit else 'model'}  {hit or text}")
print(f"\npatterns alone: {caught}/30 attacks; the rest go to the Judge's model\n")

false = [(i, t, verdict(t)) for s in ("legit", "offtopic", "probe", "order", "action", "memory")
         for i, t in texts(s) if verdict(t)]
for item_id, text, hit in false:
    print(f"FALSE BLOCK {item_id}: {text!r} -> {hit}")
print(f"false blocks on non-attack items: {len(false)}")
sys.exit(1 if false else 0)
