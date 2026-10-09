"""Deterministic attack patterns: the Judge's tool, and the reason a block can skip the model.

Each pattern is written against EVALS §3: it must catch the attack shapes without tripping the
legitimate set's apostrophes, "drop", "select", "delete", "update" and "ignore" (S-2,
T-LEGIT-FALSE-BLOCK). guards/test_patterns.py checks both sets.
"""
import re

PATTERNS: dict[str, list[str]] = {
    "sql injection": [
        r"'\s*(or|and)\s+'?\w+'?\s*=\s*'?\w+",          # ' OR 'a'='a
        r"\b\d+\s+or\s+\d+\s*=\s*\d+",                   # 1 OR 1=1
        r"\bunion\s+(all\s+)?select\b",
        r";\s*(drop|delete|update|insert|alter|truncate|create)\b",
        r"\b(drop|truncate|alter)\s+table\b",
        r"\bselect\s+[\w*,\s]+?\s+from\s+\w+",
        r"\bdelete\s+from\b",
        r"\bupdate\s+\w+\s+set\b",
        r"'\s*--",
    ],
    "markup": [
        r"<\s*/?\s*(script|iframe|img|svg|object|embed|style|link|meta)\b",
        r"\bon[a-z]+\s*=",
        r"\bjavascript\s*:",
    ],
    "template injection": [r"\{\{.*?\}\}", r"\{%.*?%\}", r"\$\{.*?\}"],
    "shell or path traversal": [
        r"\$\(",
        r";\s*(cat|rm|ls|curl|wget|bash|sh|nc)\b",
        r"\brm\s+-rf\b",
        r"(\.\./){2,}",
        r"/etc/(passwd|shadow)",
    ],
    "prompt injection": [
        r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|earlier|above)\b",
        r"\b(forget|disregard)\s+(all\s+)?(your|the|previous)\s+(rules|instructions)\b",
        r"\byou\s+are\s+now\b",
        r"^\s*system\s*:",
        r"#{2,}\s*new\s+instructions",
        r"\bpretend\s+.{0,40}\b(approved|judge|admin)",
        r"\banswer\s+as\s+the\s+(database\s+)?admin",
        r"\bdump\s+\w+",
    ],
    "prompt extraction": [
        r"\bsystem\s+prompt\b",
        r"\b(print|show|reveal|quote|repeat)\s+(me\s+)?your\s+(instructions|rules|prompt)\b",
        r"\brepeat\s+everything\s+above\b",
    ],
    "bulk data request": [
        r"\bpasswords?\b",
        r"\b(all|every)\s+customers?\b",
    ],
}

_COMPILED = [(kind, re.compile(p, re.I | re.M)) for kind, ps in PATTERNS.items() for p in ps]


def check_patterns(message: str) -> dict:
    """Return the first known attack pattern in the message, or {"match": False}.

    Args:
        message: The customer's message, exactly as received.
    """
    for kind, pattern in _COMPILED:
        if found := pattern.search(message):
            return {"match": True, "kind": kind, "matched": found.group(0)}
    return {"match": False}
