"""Throwaway stage-3 CLI: log in, then send each line to the agent and print its final text.

Stage 4 replaces the body of the loop with the pipeline's events (P-1).
"""
import asyncio
import getpass
import sys
import urllib.request

from dotenv import load_dotenv
from google.genai import types
from toolbox_core import ToolboxClient

from support.agent import TOOLBOX_URL, build_runner, login


def toolbox_is_up() -> bool:
    # Ask for the auth toolset by name: macOS AirPlay also listens on :5000 and answers 403.
    try:
        with urllib.request.urlopen(f"{TOOLBOX_URL}/api/toolset/auth", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


async def main() -> None:
    load_dotenv()
    if not toolbox_is_up():
        sys.exit(
            f"The MCP Toolbox is not running at {TOOLBOX_URL}.\n"
            "Start it in another terminal with: ./run.sh toolbox"
        )
    async with ToolboxClient(TOOLBOX_URL) as client:
        email = input("Email: ").strip()
        user = await login(client, email, getpass.getpass("Password: "))  # hidden input (C-1)
        if user is None:
            print("Invalid email or password.")
            return
        print(f"Logged in as {user['full_name']}.")

        runner, session_id = await build_runner(client, user["email"])
        while True:
            text = input("You: ").strip()
            if text.lower() in {"quit", "exit", "q"}:
                break
            if not text:
                continue
            message = types.Content(role="user", parts=[types.Part(text=text)])
            reply = ""
            async for event in runner.run_async(
                user_id=user["email"], session_id=session_id, new_message=message
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    reply = "".join(p.text or "" for p in event.content.parts)
            print(f"Agent: {reply}")


if __name__ == "__main__":
    asyncio.run(main())
