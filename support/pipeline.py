"""One turn, as a stream of SPEC §7.1 events (P-1). The CLI and the web server only render them.

Stage 4 runs only the agent stage: trace (placeholder id), stage/step agent, llm, tool_call,
tool_result, final. The guards, recall, mask and save slot in around it in later stages (P-2).
"""
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import yaml
from google.adk.runners import Runner
from google.genai import types

from support.agent import MODEL

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
BOUND = {"customer_email", "user_email"}  # bound from the session in agent.build_runner (M-5)


def _tool_info() -> dict[str, dict]:
    """What each tool runs, read from tools.yaml for display only; Python never executes it."""
    config = yaml.safe_load((ROOT / "mcp_toolbox" / "tools.yaml").read_text())
    info = {}
    for name, tool in config["tools"].items():
        statement = " ".join(tool["statement"].split())
        params = [p["name"] for p in tool["parameters"]]
        info[name] = {
            "kind": "MCP",
            "access": "READ" if statement.upper().startswith("SELECT") else "WRITE",
            "statement": statement,
            "params": params,
            "bound": [p for p in params if p in BOUND],
        }
    return info


TOOL_INFO = _tool_info()


def _decode(value):
    """Decode a JSON string into the object it holds; leave anything else alone."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _unwrap(response: dict | None) -> tuple[bool, list | str]:
    """Turn ADK's {"result": "<JSON string>"} into rows. Over MCP one row is a bare object, none is null."""
    response = response or {}
    if "error" in response:
        return False, str(response["error"])
    rows = _decode(response.get("result"))
    if rows is None:
        return True, []
    return True, rows if isinstance(rows, list) else [rows]


async def run_turn(runner: Runner, session_id: str, user: str, message: str) -> AsyncIterator[dict]:
    turn_id = f"turn_{uuid.uuid4().hex}"
    trace_id = "0" * 32  # placeholder until Stage 5 opens a real trace
    started = time.monotonic()
    log = {
        "turn_id": turn_id, "trace_id": trace_id, "user": user, "message": message,
        "terminated": None, "blocked_at": None, "wall_clock_ms": None,
        "tokens": {"in": 0, "out": 0}, "steps": [], "tool_calls": [], "llm_calls": 0,
    }

    def ms_since(t: float) -> int:
        return round((time.monotonic() - t) * 1000)

    def finish(terminal: dict) -> dict:
        """Write runs/<turn_id>.json before the terminal event goes out (P-5)."""
        log["terminated"] = terminal["terminated"]
        log["wall_clock_ms"] = ms_since(started)
        if terminal["type"] == "final":
            terminal["wall_clock_ms"] = log["wall_clock_ms"]
        RUNS_DIR.mkdir(exist_ok=True)
        (RUNS_DIR / f"{turn_id}.json").write_text(json.dumps(log, indent=2))
        return terminal

    yield {"type": "trace", "turn_id": turn_id, "trace_id": trace_id, "url": None}

    yield {"type": "stage", "key": "agent", "label": "Support agent (ADK)"}
    agent_started = last_event = time.monotonic()
    pending: dict[str, tuple[int, str, float]] = {}  # ADK call id -> (our id, tool name, start)
    reply = ""
    try:
        async for event in runner.run_async(
            user_id=user, session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            calls, responses = event.get_function_calls(), event.get_function_responses()
            # A model response; ADK also emits a content-less event when the model call fails.
            if event.author != "user" and event.content and not responses:
                usage = event.usage_metadata
                tokens_in = (usage and usage.prompt_token_count) or 0
                tokens_out = (usage and usage.candidates_token_count) or 0
                log["llm_calls"] += 1
                log["tokens"]["in"] += tokens_in
                log["tokens"]["out"] += tokens_out
                yield {
                    "type": "llm", "model": MODEL,
                    "decision": f"call {calls[0].name}" if calls else "final answer",
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "ms": ms_since(last_event), "span": "call_llm",
                }
            for call in calls:
                call_id = len(log["tool_calls"]) + len(pending) + 1
                pending[call.id or call.name] = (call_id, call.name, time.monotonic())
                yield {
                    "type": "tool_call", "id": call_id, "name": call.name,
                    "args": {k: _decode(v) for k, v in (call.args or {}).items()},
                    "info": TOOL_INFO.get(call.name, {"kind": "MCP"}),
                    "span": f"execute_tool {call.name}",
                }
            for response in responses:
                call_id, name, call_started = pending.pop(response.id or response.name)
                ok, result = _unwrap(response.response)
                took = ms_since(call_started)
                entry = {"name": name, "ok": ok, "ms": took}
                if not ok:
                    entry["error"] = result
                log["tool_calls"].append(entry)
                yield {"type": "tool_result", "id": call_id, "name": name, "ok": ok, "ms": took,
                       **({"result": result} if ok else {"error": result})}
            if event.is_final_response() and event.content and event.content.parts:
                reply = "".join(p.text or "" for p in event.content.parts)
            last_event = time.monotonic()
    except Exception as exc:  # fail loud: the turn ends with an error naming the step (P-4)
        error = f"{type(exc).__name__}: {exc}"
        log["steps"].append({"key": "agent", "status": "error", "ms": ms_since(agent_started),
                             "error": error})
        yield finish({"type": "error", "step": "agent", "status": 502, "error": error,
                      "terminated": "error"})
        return

    took = ms_since(agent_started)
    log["steps"].append({"key": "agent", "status": "passed", "ms": took})
    calls = len(log["tool_calls"])
    yield {
        "type": "step", "key": "agent", "status": "passed", "ms": took, "kind": "in-process",
        "detail": f"{log['llm_calls']} llm call(s), {calls} tool call(s)", "span": "invoke_agent",
    }
    yield finish({
        "type": "final", "blocked": False, "blocked_at": None, "response": reply,
        "terminated": "done", "wall_clock_ms": None, "tokens": dict(log["tokens"]),
    })
