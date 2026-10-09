"""Tracing: every turn is one trace in Phoenix (O-1..O-4, SPEC §10).

setup() must run before any agent is built, so ADK's invoke_agent / call_llm / execute_tool
spans land under the pipeline's agent.turn span instead of starting traces of their own.
"""
import json
import os
import urllib.request

from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PHOENIX_URL = os.environ.get("PHOENIX_URL", "http://localhost:6006")
PROJECT = "customer-support"

_provider: TracerProvider | None = None
_project_id: str | None = None


def setup() -> None:
    global _provider
    if _provider is not None:
        return
    _provider = TracerProvider(resource=Resource({"openinference.project.name": PROJECT}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(f"{PHOENIX_URL}/v1/traces")))
    trace.set_tracer_provider(_provider)
    GoogleGenAIInstrumentor().instrument(tracer_provider=_provider)


def flush() -> None:
    """Send buffered spans now, so a trace is complete in Phoenix when its URL is printed."""
    if _provider is not None:
        _provider.force_flush()


def phoenix_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{PHOENIX_URL}/v1/projects", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def _api(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{PHOENIX_URL}{path}", data=data,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.load(resp)


def trace_url(trace_id: str) -> str:
    """Phoenix addresses a trace by its project's id; create the project on a fresh database."""
    global _project_id
    if _project_id is None:
        projects = _api("/v1/projects")["data"]
        found = next((p["id"] for p in projects if p["name"] == PROJECT), None)
        _project_id = found or _api("/v1/projects", {"name": PROJECT})["data"]["id"]
    return f"{PHOENIX_URL}/projects/{_project_id}/traces/{trace_id}"
