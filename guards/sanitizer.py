"""The Sanitizer: first guard, in-process, no model call (S-1). Cheap checks only.

Allowed: letters, digits, spaces, punctuation and symbols in any script, so apostrophes, #, $,
:, ;, @ and emoji pass (S-2). Rejected: control and invisible "format" characters (zero-width
spaces, bidi overrides), which have no place in a support message and are used to hide text.
"""
import unicodedata

# Not in THRESHOLDS.md (no row exists for it). The longest gold message is 80 characters;
# 2 000 leaves room for a long complaint and stops a pasted document.
MAX_CHARS = 2000

# The cheapest obvious patterns; everything subtler is the Judge's job.
OBVIOUS = ("<script", "drop table", "union select", "; --", "';")


def sanitize(message: str) -> tuple[bool, str]:
    """Return (blocked, reason)."""
    if len(message) > MAX_CHARS:
        return True, f"message too long ({len(message)} > {MAX_CHARS} characters)"
    for ch in message:
        if unicodedata.category(ch).startswith("C") and ch not in "\n\t":
            return True, f"disallowed character U+{ord(ch):04X}"
    lowered = message.lower()
    for pattern in OBVIOUS:
        if pattern in lowered:
            return True, f"obvious attack pattern {pattern!r}"
    return False, "ok"
