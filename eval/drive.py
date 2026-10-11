"""Drive the gold sets through the running web server and save every turn as it finishes.

Each item is one turn: log in as its user (a fresh session), send the message, read the event
stream to its terminal event (EVALS §3). Results go to reports/progress.jsonl, one line per
turn, so a run stopped by the free tier's daily quota resumes where it left off.

Memory pairs (the student's Stage 10 decision, option B): one task per user, each waiting its
own T-MEM-WAIT between plant and ask, all in parallel with the other sets. Every turn, from any
task, passes through one shared pace so the model quotas hold.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mem0 import AsyncMemoryClient

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "gold"
RUNS = ROOT / "runs"
PROGRESS = ROOT / "reports" / "progress.jsonl"
WEB = os.environ.get("WEB_URL", "http://127.0.0.1:8000")
PSQL = Path(os.environ.get("PG_BIN", "/usr/local/opt/postgresql@16/bin")) / "psql"

MEM_WAIT_S = 120  # T-MEM-WAIT
PACE_S = 20  # one turn start every 20 s: up to ~4 model calls per turn on 5-per-minute quotas
SETS = ["order", "action", "attack", "legit", "offtopic", "probe"]  # order and action right after reset


def users() -> dict[str, tuple[str, str]]:
    """First name -> (email, password), from the seed the database was reset with."""
    seed = (ROOT / "db" / "seed.sql").read_text()
    found = re.findall(r"\('([^']+@[^']+)',\s*'([^']+)',\s*(?:TRUE|FALSE),\s*\d+,\s*'([^']+)'\)", seed)
    return {password: (email, password) for email, _name, password in found}


def gold(name: str) -> list[dict]:
    return [json.loads(line) for line in (GOLD / f"{name}.jsonl").read_text().splitlines()]


def key(r: dict) -> str:
    return f"{r['set']}:{r['id']}:{r.get('phase', '')}"


def load_progress() -> tuple[dict, dict[str, dict]]:
    """The run's meta line and the latest result per item (a redone item overrides)."""
    if not PROGRESS.exists():
        return {}, {}
    meta, results = {}, {}
    for line in PROGRESS.read_text().splitlines():
        r = json.loads(line)
        if "meta" in r:
            meta.update(r["meta"])
        else:
            results[key(r)] = r
    return meta, results


def append(record: dict) -> None:
    PROGRESS.parent.mkdir(exist_ok=True)
    with PROGRESS.open("a") as f:
        f.write(json.dumps(record) + "\n")


def orders_hash() -> str:
    """A byte-level fingerprint of customer_orders, for T-MUTATE (EVALS §3.6)."""
    out = subprocess.run(
        [str(PSQL), "-d", "support", "-Atc", "SELECT * FROM customer_orders ORDER BY order_id"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return hashlib.sha256(out.encode()).hexdigest()


async def clear_memories(emails: list[str]) -> None:
    client = AsyncMemoryClient(api_key=os.environ["MEM0_API_KEY"])
    for email in emails:
        await client.delete_all(user_id=email)


async def start_fresh(memory_users: list[str]) -> None:
    """Archive old run logs, reset Postgres, clear Mem0 for the memory-pair users."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    old = list(RUNS.glob("*.json"))
    if old:
        archive = RUNS / "archive" / stamp
        archive.mkdir(parents=True)
        for f in old:
            shutil.move(str(f), archive / f.name)
        print(f"archived {len(old)} older run logs to {archive.relative_to(ROOT)}")
    if PROGRESS.exists():
        PROGRESS.rename(PROGRESS.with_name(f"progress-{stamp}.jsonl"))
    subprocess.run([str(ROOT / "run.sh"), "reset"], check=True)
    await clear_memories(memory_users)
    append(
        {
            "meta": {
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "orders_before": orders_hash(),
            }
        }
    )


def forget(sets: list[str]) -> None:
    """Drop these sets' results so the next drive runs them again; the old file is kept."""
    if not PROGRESS.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = PROGRESS.read_text().splitlines()
    shutil.copy(PROGRESS, PROGRESS.with_name(f"progress-before-redo-{stamp}.jsonl"))
    kept = [line for line in lines if json.loads(line).get("set") not in sets]
    PROGRESS.write_text("".join(f"{line}\n" for line in kept))
    append({"meta": {f"redo_{stamp}": sets}})
    print(f"redo: dropped {len(lines) - len(kept)} results from {', '.join(sets)}")


class QuotaExhausted(Exception):
    """Every model in the pool is out of daily quota: stop, and resume tomorrow."""


def check_quota(r: dict) -> None:
    last = (r.get("events") or [{}])[-1]
    if (
        last.get("type") == "error"
        and "every model in the pool failed" in last.get("error", "")
        and "per-minute" not in last.get("error", "")
    ):
        raise QuotaExhausted(last["error"])


class Pace:
    """Every turn from every task starts at least PACE_S after the previous one."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        async with self.lock:
            await asyncio.sleep(max(0.0, self.last + PACE_S - time.monotonic()))
            self.last = time.monotonic()


async def turn(http: httpx.AsyncClient, pace: Pace, email: str, password: str, message: str) -> dict:
    await pace.wait()
    login = await http.post("/api/login", json={"email": email, "password": password})
    if login.status_code != 200:
        return {"http": login.status_code, "error": f"login failed: {login.text}", "events": []}
    started, events = time.monotonic(), []
    async with http.stream("POST", "/api/chat", json={"user_id": email, "message": message}) as r:
        if r.status_code != 200:
            return {"http": r.status_code, "error": (await r.aread()).decode(), "events": []}
        async for line in r.aiter_lines():
            if line.strip():
                events.append(json.loads(line))
    await http.post("/api/logout", json={"user_id": email})
    return {"http": 200, "ms": round((time.monotonic() - started) * 1000), "events": events}


def outcome(r: dict) -> str:
    last = (r.get("events") or [{}])[-1]
    where = last.get("blocked_at") or last.get("step")
    return f"{last.get('terminated', 'http ' + str(r.get('http')))}" + (f" at {where}" if where else "")


async def run_sets(http, pace, people, done, limit) -> None:
    for name in SETS:
        for item in gold(name)[:limit]:
            if f"{name}:{item['id']}:" in done:
                continue
            email, password = people[item["user"]]
            r = {
                "set": name,
                "id": item["id"],
                "user": email,
                "message": item["message"],
                **await turn(http, pace, email, password, item["message"]),
            }
            check_quota(r)  # not recorded: the item runs again on resume
            append(r)
            print(f"{name:<9}{item['id']}  {outcome(r)}")
        if name == "action" and limit is None:
            append({"meta": {"orders_after_action": orders_hash()}})


async def run_memory_user(http, pace, email, password, pairs) -> None:
    for pair in pairs:
        await clear_memories([email])  # EVALS §3.7: clear that user's memories before each pair
        for phase, text in (("plant", pair["plant"]), ("ask", pair["ask"])):
            r = {
                "set": "memory",
                "id": pair["id"],
                "phase": phase,
                "user": email,
                "message": text,
                "keyword": pair["keyword"],
                **await turn(http, pace, email, password, text),
            }
            check_quota(r)
            append(r)
            print(f"memory   {pair['id']} {phase:<5} {outcome(r)}")
            if phase == "plant":
                await asyncio.sleep(MEM_WAIT_S)


async def drive(fresh: bool, limit: int | None = None, memory: bool = True) -> None:
    """Run every unfinished item; raises QuotaExhausted if the day's quota runs out."""
    people = users()
    pairs_by_user: dict[str, list[dict]] = defaultdict(list)
    for pair in gold("memory")[:limit]:
        pairs_by_user[people[pair["user"]][0]].append(pair)
    if fresh:
        await start_fresh(list(pairs_by_user))
    _meta, done = load_progress()
    # A user's pairs run together: if any is unfinished, clear that user and redo them all.
    redo = (
        {
            email: pairs
            for email, pairs in pairs_by_user.items()
            if any(f"memory:{p['id']}:ask" not in done for p in pairs)
        }
        if memory
        else {}
    )
    if redo and not fresh:
        await clear_memories(list(redo))
    pace = Pace()
    async with httpx.AsyncClient(base_url=WEB, timeout=120) as http:
        tasks = [run_sets(http, pace, people, done, limit)]
        tasks += [
            run_memory_user(http, pace, email, people_pw(people, email), pairs)
            for email, pairs in redo.items()
        ]
        await asyncio.gather(*tasks)


def people_pw(people: dict[str, tuple[str, str]], email: str) -> str:
    return next(pw for e, pw in people.values() if e == email)
