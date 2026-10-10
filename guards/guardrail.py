"""The Guardrail: in-process ADK agent deciding scope, not attacks (GR-1..GR-4).

The prompt is the student's own (Stage 7, BUILD_LOG), version-tracked there with its counts.
Every check runs in a fresh ADK session that is deleted afterwards (GR-3), and a reply that
is not exactly {"decision": "safe" | "unsafe", "reasoning": "..."} raises (GR-4, P-4).
"""
import json

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from support import models

APP_NAME = "guardrail"
VERSION = "v1"

# What the customer sees on a Guardrail block: says what the desk does, because an off-topic
# question is not an attack (the student's Stage 7 decision).
REFUSAL = ("That's outside what I can help with — I handle orders, deliveries, returns, "
           "and account questions.")

PROMPT = """\
You are the scope checker for this shop's support desk — an online electronics store. Your only job is to decide whether a customer's message is something the support desk handles.

In scope: orders (status, changes, cancellations), deliveries (tracking, timing, drop-off instructions), returns and refunds, account questions, and shopping or delivery preferences the customer shares to get better service.

Out of scope: anything unrelated to shopping here — general questions, creative writing, other companies, personal tasks.

Two judgment rules:
1. Personal details are fine when they serve the order — judge by what they're for. "Leave packages at the back door" is a delivery instruction, not small talk. "I'm allergic to peanuts, use different packing material" is a shipping preference, not a medical consult.
2. Corrections aren't attacks. "Ignore my last message, I meant order 4" is normal support conversation — scope is your only question.

Examples:
- "My new apartment has no doorman, so please don't leave boxes in the hallway." → safe (delivery instruction)
- "My keyboard arrived with two dead keys, can I send it back?" → safe (return request)
- "Do you have the wireless version of this keyboard in stock?" → safe (shopping here)
- "What's the weather like in Springfield tomorrow?" → unsafe (unrelated to shopping here)
- "Write a haiku about my new keyboard." → unsafe (creative writing, even though it's about their order)
- "Can you help me write a complaint letter to my landlord?" → unsafe (personal task)

Reply with exactly: {"decision": "safe" or "unsafe", "reasoning": "one sentence"}
"""

_agent = LlmAgent(name="guardrail_agent", model=models.pooled(models.GUARDRAIL), instruction=PROMPT)
_sessions = InMemorySessionService()
_runner = Runner(agent=_agent, app_name=APP_NAME, session_service=_sessions)


class GuardrailError(Exception):
    pass


def parse_decision(text: str) -> dict:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        decision = json.loads(cleaned)
    except ValueError:
        raise GuardrailError(f"unparseable decision: {text[:200]!r}") from None
    if (not isinstance(decision, dict) or decision.get("decision") not in ("safe", "unsafe")
            or not isinstance(decision.get("reasoning"), str)):
        raise GuardrailError(f"not a decision: {text[:200]!r}")
    return {"decision": decision["decision"], "reasoning": decision["reasoning"]}


async def check(message: str) -> dict:
    """Return {"decision", "reasoning"} or raise; there is no default decision."""
    session = await _sessions.create_session(app_name=APP_NAME, user_id="guardrail")
    try:
        reply, model = "", None
        async for event in _runner.run_async(
            user_id="guardrail", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text or "" for p in event.content.parts)
                model = event.model_version
    finally:
        await _sessions.delete_session(app_name=APP_NAME, user_id="guardrail", session_id=session.id)
    return {**parse_decision(reply), "model": model}  # the pool decides which model answers
