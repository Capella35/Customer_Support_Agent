"""Model pool for Gemini's free tier: 20 requests per model per day (seen 2026-10-09).

Each layer lists models in order of preference. A call that gets 429 moves to the next model and
rests the one that refused: for a minute if it was the per-minute limit, for the rest of this
process if it was the per-day limit. A 503 (overloaded) moves on without resting it. Only
when every model fails does the call raise, and the pipeline then ends the turn with an error
(P-4), never a silent pass. Built-in retries are off: they hid a 119 s wait
behind one 503 in Stage 4, and the next model in the list is a better retry.

The names map to five separate daily quotas: gemini-3.5-flash is served as gemini-3.6-flash and
gemini-3.7-flash as gemini-3.8-flash. Strong models come first for the support agent; the
guards start on the Lite models so the agent's quota lasts longer.
"""
import time
from collections.abc import AsyncGenerator

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from google.genai.errors import ClientError, ServerError

AGENT = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.7-flash",
         "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
GUARDRAIL = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
             "gemini-3.7-flash", "gemini-3.5-flash"]
JUDGE = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3-flash-preview",
         "gemini-3.7-flash", "gemini-3.5-flash"]

_resting: dict[str, float] = {}  # model -> monotonic time it may be tried again; quota is per model
DAY = float("inf")


class PooledGemini(Gemini):
    fallbacks: list[str] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        failures = []
        for name in [self.model, *self.fallbacks]:
            if _resting.get(name, 0.0) > time.monotonic():
                continue
            llm_request.model = name
            try:
                async for response in super().generate_content_async(llm_request, stream):
                    yield response
                return
            except ClientError as exc:
                if exc.code != 429:
                    raise
                per_day = "PerDay" in str(exc)
                _resting[name] = DAY if per_day else time.monotonic() + 60
                failures.append(f"{name}: 429 {'daily' if per_day else 'per-minute'} quota")
            except ServerError as exc:
                failures.append(f"{name}: {exc.code}")
        raise RuntimeError("every model in the pool failed: " + "; ".join(failures or ["all resting"]))


def pooled(order: list[str]) -> PooledGemini:
    return PooledGemini(model=order[0], fallbacks=order[1:],
                        retry_options=types.HttpRetryOptions(attempts=1))
