"""The CLI: log in, then render each turn's pipeline events (P-1). No pipeline logic lives here.

  python -m support.cli                       prompts for email and password (C-1)
  python -m support.cli --user EMAIL          asks only for the password (or SUPPORT_PASSWORD)
  python -m support.cli --user EMAIL --events raw NDJSON on stdout and nothing else (C-3)

Messages are read one per line from stdin, so `echo "…" | python -m support.cli …` works.
"""
import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import urllib.request
import warnings

from dotenv import load_dotenv
from toolbox_core import ToolboxClient

from support.agent import TOOLBOX_URL, build_runner, login
from support.pipeline import run_turn


def toolbox_is_up() -> bool:
    # Ask for the auth toolset by name: macOS AirPlay also listens on :5000 and answers 403.
    try:
        with urllib.request.urlopen(f"{TOOLBOX_URL}/api/toolset/auth", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def render(event: dict) -> str | None:
    """One readable line (or a few) per event, in the layout sketched in BUILD_LOG stage 4."""
    kind = event["type"]
    if kind == "trace":
        return f"trace    {event['turn_id']}" + (f"  {event['url']}" if event.get("url") else "")
    if kind == "stage":
        return None  # its step line reports the outcome
    if kind == "step":
        mark = "✓ passed" if event["status"] == "passed" else "✗ BLOCKED"
        line = f"step     {event['key']} → {mark}: {event.get('detail', '')}  ({event['ms']} ms)"
        if "memories" in event:
            mems = event["memories"]
            inserted = sum(m["inserted"] for m in mems)
            line = (f"step     {event['key']} → {inserted} inserted, {len(mems) - inserted} skipped"
                    f"  ({event['ms']} ms)")
            for m in mems:
                if not m["inserted"]:
                    line += f"\n           skipped: \"{m['memory'][:30]}…\" — {m['score']}, {m['reason']}"
        return line
    if kind == "llm":
        return (f"llm      {event['model']} → {event['decision']}"
                f"  ({event['tokens_in']} in / {event['tokens_out']} out, {event['ms']} ms)")
    if kind == "tool_call":
        args = ", ".join(f"{k}={json.dumps(v)}" for k, v in event["args"].items())
        return f"tool     {event['name']}({args}) [{event['info'].get('access', '?')}]"
    if kind == "tool_result":
        if not event["ok"]:
            return f"         ↩ ✗ failed: {event['error']}  ({event['ms']} ms)"
        rows = len(event["result"])
        return f"         ↩ ok, {rows} row{'' if rows == 1 else 's'}  ({event['ms']} ms)"
    if kind == "final":
        return (f"final    {event['terminated']}  ({event['wall_clock_ms']} ms,"
                f" {event['tokens']['in']} in / {event['tokens']['out']} out)\n"
                f"Agent: {event['response']}")
    if kind == "error":
        return f"error    {event['step']} → ✗ {event['error']}"
    return json.dumps(event)


def read_line(prompt: str, show: bool) -> str | None:
    """Read one line, or None at end of input (piped stdin, Ctrl-D)."""
    if show:
        print(prompt, end="", flush=True, file=sys.stderr)
    line = sys.stdin.readline()
    return line.strip() if line else None


async def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m support.cli")
    parser.add_argument("--user", help="log in as this email instead of being asked for it")
    parser.add_argument("--events", action="store_true", help="print raw NDJSON events only")
    args = parser.parse_args()
    load_dotenv()
    # The pipeline reports a failed turn as an error event; ADK's own tracebacks would bury it.
    logging.getLogger("google_adk").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")

    if not toolbox_is_up():
        sys.exit(
            f"The MCP Toolbox is not running at {TOOLBOX_URL}.\n"
            "Start it in another terminal with: ./run.sh toolbox"
        )
    interactive = sys.stdin.isatty()
    async with ToolboxClient(TOOLBOX_URL) as client:
        email = args.user or read_line("Email: ", True)
        if not email:
            return
        # getpass reads from the terminal even when stdin is piped, and hides the input (C-1).
        password = os.environ.get("SUPPORT_PASSWORD") or getpass.getpass("Password: ")
        user = await login(client, email, password)
        if user is None:
            sys.exit("Invalid email or password.")
        print(f"Logged in as {user['full_name']}.", file=sys.stderr)

        runner, session_id = await build_runner(client, user["email"])
        while (text := read_line("You: ", interactive and not args.events)) is not None:
            if text.lower() in {"quit", "exit", "q"}:
                break
            if not text:
                continue
            async for event in run_turn(runner, session_id, user["email"], text):
                line = json.dumps(event) if args.events else render(event)
                if line is not None:
                    print(line, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
