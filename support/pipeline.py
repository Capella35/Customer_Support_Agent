"""One turn, as a stream of SPEC §7.1 events (P-1). The CLI and the web server only render them.

Steps, in P-2 order: sanitize -> judge -> guardrail -> recall -> agent -> mask -> save. A block ends the turn at once (P-3); a guard that fails
ends it with an error naming the step, never with an allow (P-4). The whole turn runs inside
one agent.turn span, so every step's span and ADK's invoke_agent are its children (SPEC §10).
"""
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import yaml
from google.adk.runners import Runner
from google.genai import types
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from guards import guardrail
from guards.sanitizer import sanitize
from support import memory, telemetry
from support.agent import MODEL
from support.guard_clients import ask_judge, ask_masker

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
REFUSAL = "Sorry, I can't help with that request."
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
    tracer = trace.get_tracer("support.pipeline")
    # Full message and reply go on the span: the student's Stage 5 decision (BUILD_LOG).
    with tracer.start_as_current_span("agent.turn", attributes={
        "openinference.span.kind": "CHAIN", "input.value": message,
        "user.id": user, "session.id": session_id, "turn_id": turn_id,
    }) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        async for event in _turn(runner, session_id, user, message, turn_id, trace_id):
            if event["type"] == "final":
                span.set_attribute("output.value", event["response"])
            elif event["type"] == "error":
                span.set_status(Status(StatusCode.ERROR, event["error"]))
            yield event
    telemetry.flush()


async def _turn(runner: Runner, session_id: str, user: str, message: str,
                turn_id: str, trace_id: str) -> AsyncIterator[dict]:
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

    yield {"type": "trace", "turn_id": turn_id, "trace_id": trace_id,
           "url": telemetry.trace_url(trace_id)}

    tracer = trace.get_tracer("support.pipeline")

    def blocked(key: str, response: str = REFUSAL) -> dict:
        log["blocked_at"] = key
        return finish({"type": "final", "blocked": True, "blocked_at": key, "response": response,
                       "terminated": "blocked", "wall_clock_ms": None, "tokens": dict(log["tokens"])})

    # sanitize: in-process, no model call (S-1)
    yield {"type": "stage", "key": "sanitize", "label": "Sanitizer"}
    t = time.monotonic()
    with tracer.start_as_current_span("security.sanitize", attributes={
        "openinference.span.kind": "GUARDRAIL", "input.value": message,
    }) as span:
        is_blocked, reason = sanitize(message)
        span.set_attribute("output.value", f"{'block' if is_blocked else 'allow'}: {reason}")
    status = "blocked" if is_blocked else "passed"
    log["steps"].append({"key": "sanitize", "status": status, "ms": ms_since(t)})
    yield {"type": "step", "key": "sanitize", "status": status, "detail": reason,
           "ms": ms_since(t), "span": "security.sanitize", "kind": "Python fn"}
    if is_blocked:
        yield blocked("sanitize")
        return

    # judge: the A2A Security Judge (J-4)
    yield {"type": "stage", "key": "judge", "label": "A2A Security Judge"}
    t = time.monotonic()
    with tracer.start_as_current_span("security.a2a_judge", attributes={
        "openinference.span.kind": "GUARDRAIL", "input.value": message,
    }) as span:
        try:
            verdict = await ask_judge(message)
        except Exception as exc:  # fail loud: an error event, never an allow (P-4)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            log["steps"].append({"key": "judge", "status": "error", "ms": ms_since(t),
                                 "error": str(exc)})
            yield finish({"type": "error", "step": "judge", "status": 502, "error": str(exc),
                          "terminated": "error"})
            return
        span.set_attribute("output.value", json.dumps(verdict))
    status = "blocked" if verdict["verdict"] == "block" else "passed"
    log["steps"].append({"key": "judge", "status": status, "ms": ms_since(t)})
    yield {"type": "step", "key": "judge", "status": status,
           "detail": f"{verdict['verdict']}: {verdict['reason']}", "ms": ms_since(t),
           "span": "security.a2a_judge", "kind": "A2A"}
    if status == "blocked":
        yield blocked("judge")
        return

    # guardrail: in-process scope check, fresh session per message (GR-1..GR-4)
    yield {"type": "stage", "key": "guardrail", "label": "Guardrail (scope)"}
    t = time.monotonic()
    with tracer.start_as_current_span("guardrail.check", attributes={
        "openinference.span.kind": "GUARDRAIL", "input.value": message,
        "guardrail.prompt_version": guardrail.VERSION,
    }) as span:
        try:
            decision = await guardrail.check(message)
        except Exception as exc:  # fail loud: an unparseable decision is an error, not a pass
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            log["steps"].append({"key": "guardrail", "status": "error", "ms": ms_since(t),
                                 "error": str(exc)})
            yield finish({"type": "error", "step": "guardrail", "status": 502,
                          "error": str(exc), "terminated": "error"})
            return
        span.set_attribute("output.value", json.dumps(decision))
    status = "blocked" if decision["decision"] == "unsafe" else "passed"
    log["steps"].append({"key": "guardrail", "status": status, "ms": ms_since(t)})
    yield {"type": "step", "key": "guardrail", "status": status,
           "detail": f"{decision['decision']}: {decision['reasoning']}", "ms": ms_since(t),
           "span": "guardrail.check", "kind": "in-process"}
    if status == "blocked":
        yield blocked("guardrail", guardrail.REFUSAL)
        return

    # recall: a pipeline step, not a tool; every candidate is listed with its score (R-1, R-5)
    yield {"type": "stage", "key": "recall", "label": "Memory recall (Mem0)"}
    t = time.monotonic()
    with tracer.start_as_current_span("memory.recall", attributes={
        "openinference.span.kind": "RETRIEVER", "input.value": message,
    }) as span:
        try:
            candidates = await memory.recall(user, message)
        except Exception as exc:  # fail loud: a broken recall is an error, not "no memories"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            log["steps"].append({"key": "recall", "status": "error", "ms": ms_since(t),
                                 "error": str(exc)})
            yield finish({"type": "error", "step": "recall", "status": 502, "error": str(exc),
                          "terminated": "error"})
            return
        for n, c in enumerate(candidates):
            span.set_attribute(f"retrieval.documents.{n}.document.content", c["memory"])
            span.set_attribute(f"retrieval.documents.{n}.document.score", c["score"])
    inserted = sum(c["inserted"] for c in candidates)
    log["steps"].append({"key": "recall", "status": "passed", "ms": ms_since(t),
                         "inserted": inserted, "skipped": len(candidates) - inserted})
    yield {"type": "step", "key": "recall", "status": "passed",
           "detail": f"{inserted} inserted, {len(candidates) - inserted} skipped",
           "ms": ms_since(t), "span": "memory.recall", "kind": "Python fn", "memories": candidates}
    agent_input = memory.with_memories(message, candidates)

    yield {"type": "stage", "key": "agent", "label": "Support agent (ADK)"}
    agent_started = last_event = time.monotonic()
    pending: dict[str, tuple[int, str, float]] = {}  # ADK call id -> (our id, tool name, start)
    reply = ""
    try:
        async for event in runner.run_async(
            user_id=user, session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=agent_input)]),
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
                    "type": "llm", "model": event.model_version or MODEL,  # the model that answered
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

    # mask: the A2A Data Masker sees the reply before the customer does (K-1..K-3)
    yield {"type": "stage", "key": "mask", "label": "A2A Data Masker"}
    t = time.monotonic()
    with tracer.start_as_current_span("security.a2a_mask", attributes={
        "openinference.span.kind": "GUARDRAIL", "input.value": reply,
    }) as span:
        try:
            masked = await ask_masker(reply, user, message)
        except Exception as exc:  # fail loud: never send the unmasked reply (P-4)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            log["steps"].append({"key": "mask", "status": "error", "ms": ms_since(t),
                                 "error": str(exc)})
            yield finish({"type": "error", "step": "mask", "status": 502, "error": str(exc),
                          "terminated": "error"})
            return
        span.set_attribute("output.value", masked["masked_text"])
        span.set_attribute("masker.summary", masked["summary"])
    log["steps"].append({"key": "mask", "status": "passed", "ms": ms_since(t)})
    yield {"type": "step", "key": "mask", "status": "passed", "detail": masked["summary"],
           "ms": ms_since(t), "span": "security.a2a_mask", "kind": "A2A"}

    # save: only the customer's message, only on a turn that was not blocked (R-4, P-3)
    yield {"type": "stage", "key": "save", "label": "Memory save (Mem0)"}
    t = time.monotonic()
    with tracer.start_as_current_span("memory.save", attributes={
        "openinference.span.kind": "TOOL", "input.value": message,
    }) as span:
        try:
            status = await memory.save(user, message)
        except Exception as exc:  # fail loud: the turn says the save failed
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            log["steps"].append({"key": "save", "status": "error", "ms": ms_since(t),
                                 "error": str(exc)})
            yield finish({"type": "error", "step": "save", "status": 502, "error": str(exc),
                          "terminated": "error"})
            return
        span.set_attribute("output.value", status)
    log["steps"].append({"key": "save", "status": "passed", "ms": ms_since(t)})
    yield {"type": "step", "key": "save", "status": "passed",
           "detail": f"saved your message only ({status})", "ms": ms_since(t),
           "span": "memory.save", "kind": "Python fn"}

    yield finish({
        "type": "final", "blocked": False, "blocked_at": None, "response": masked["masked_text"],
        "terminated": "done", "wall_clock_ms": None, "tokens": dict(log["tokens"]),
    })
