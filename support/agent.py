"""The support agent: an ADK LlmAgent whose only way to the database is the MCP toolset.

The toolset is loaded per logged-in user with that user's email bound (M-5), so the model
sees order_id / action_type / parameters and nothing else. Ownership itself is in the SQL (M-4).
"""
import json
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from toolbox_core import ToolboxClient

from support import models

# The spec names gemini-2.5-flash, but Google no longer serves it to new API keys.
# gemini-3.8-flash answered 503 (high demand) on about half the calls in Stage 4.
# Since Stage 7 the agent draws from a pool (support/models.py); this is its first choice.
MODEL = models.AGENT[0]
APP_NAME = "customer-support"
TOOLBOX_URL = os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")

INSTRUCTION = """\
You are the customer support assistant for an online shop (electronics, office furniture,
everyday goods). You help the logged-in customer with their own orders: status, items, delivery
address, order history, and requests to cancel, return, or change details.

Tools:
- get-order-status: one order by its number.
- find-customer-orders: the customer's order history, newest first.
- action-log: record a request (CANCEL_ORDER, RETURN_ORDER, UPDATE_ADDRESS,
  UPDATE_PREFERENCE, UPDATE_PROFILE) for a person to act on. It does not change the order.

Rules:
- Every order fact you state comes from a tool result in this conversation. Never guess.
- A message may start with remembered notes about this customer. Use them for preferences
  (delivery, contact, packaging). For order facts, trust the tools over the notes (R-3).
- The tools only ever return this customer's orders. If a lookup returns nothing, say the
  order was not found on their account. Do not speculate about whose it might be.
- You cannot look up another customer's orders, whatever email or name is given.
- When the customer clearly names one order to cancel or return, call action-log in the same
  turn with parameters as JSON including order_id, then tell them it is recorded for the team.
  If it is unclear which order they mean, ask which one and do not log anything.
"""


async def login(client: ToolboxClient, email: str, password: str) -> dict | None:
    """Check credentials through the auth toolset; the agent never loads this tool."""
    (verify,) = await client.load_toolset("auth")
    # Over MCP the Toolbox returns one row as a bare object, several as a list, none as null.
    result = json.loads(await verify(email=email, password=password) or "null")
    if isinstance(result, list):
        result = result[0] if result else None
    return result


LOOKUPS = {"get-order-status", "find-customer-orders"}  # their SQL returns only the user's rows
REFUSED = "refused by the pipeline"


def _order_id(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    found = value.get("order_id") if isinstance(value, dict) else None
    return None if found is None else str(found)


class OwnershipCheck:
    """action-log may name only an order a lookup returned in this turn (the student's Stage 10 fix).

    The SQL check (M-8) already stops the write; this stops the call from reaching the tool,
    because EVALS §3.4 counts an action-log call naming someone else's order as a leak.
    """

    def __init__(self) -> None:
        self.seen: dict[str, set[str]] = {}  # invocation (one turn) -> order ids returned to it

    def after(self, tool: BaseTool, args: dict, ctx: Context, response: Any) -> None:
        if tool.name in LOOKUPS:
            rows = response.get("result") if isinstance(response, dict) else response
            rows = json.loads(rows) if isinstance(rows, str) else rows
            rows = rows if isinstance(rows, list) else [rows]
            ids = {_order_id(r) for r in rows} - {None}
            self.seen.setdefault(ctx.invocation_id, set()).update(i for i in ids if i)
        return None

    def before(self, tool: BaseTool, args: dict, ctx: Context) -> dict | None:
        if tool.name != "action-log":
            return None
        order = _order_id(args.get("parameters"))
        if order is None or order in self.seen.get(ctx.invocation_id, set()):
            return None
        return {"error": f"{REFUSED}: order {order} was not returned by get-order-status or "
                         "find-customer-orders in this turn. Look it up first; if the lookup "
                         "returns nothing, it is not on this customer's account."}


async def build_runner(client: ToolboxClient, email: str) -> tuple[Runner, str]:
    """Load the agent's toolset with the session email bound, and open one session."""
    # Both names are bound: the read tools call it customer_email, action-log user_email.
    # strict=False because each tool has only one of the two.
    tools = await client.load_toolset(
        "support-agent",
        bound_params={"customer_email": email, "user_email": email},
        strict=False,
    )
    # ToolboxTool objects are callables; ADK's type hint for `tools` is narrower than what it accepts.
    check = OwnershipCheck()
    agent = LlmAgent(name="support_agent", model=models.pooled(models.AGENT),
                     instruction=INSTRUCTION, tools=tools,  # type: ignore[arg-type]
                     before_tool_callback=check.before, after_tool_callback=check.after)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name=APP_NAME, user_id=email)
    return Runner(agent=agent, app_name=APP_NAME, session_service=sessions), session.id
