# DESIGN

> Copy this file to `DESIGN.md` and answer it **before you write code**. Revise it as you
> learn, but keep the first version in git history. A stranger grades it (`G-DESIGN`), so
> write for someone who has never seen your code. The five headings are fixed; keep them.

## Components

What are the moving parts, and which of them are separate processes? Name each one and its
port. Why are the Judge and Masker separate services while the Guardrail runs in-process?
Answer in your own words, not SPEC's.

The moving parts:

- PostgreSQL (:5432) — separate process. It holds all the customer data: users, orders, the actions log.
- MCP Toolbox (:5000) — separate process serving the database tools.
- CLI — separate process, no port. Today it imports the agent directly; from Stage 4 it will run the pipeline itself by importing the same pipeline module (P-1). It's the only front end so far — ./run.sh cli in Stage 3 never touched :8000.
- Web server (:8000) — not built yet; in Stage 9 it will serve the browser.
- Judge (:10002) — separate A2A service.
- Masker (:10003) — separate A2A service.
- Phoenix (:6006) — separate process collecting traces; every pipeline step is a span in one trace.
- Mem0 cloud — external memory service; recall and save call it.
- In-process, no port: the sanitizer, the guardrail, and the agent itself.

Why the split:

The ownership reason stands — checks that are identical everywhere belong to central security as shared, versioned services; the shop's scope belongs to the product team. But there's more, from experience. Isolation: the Judge reads text that might be an attack, so it's safer in its own process with its own model session — the suspicious message gets examined alone, never sitting in the same context as the agent it's trying to manipulate. Cost: every Judge and Masker call is a network hop, adding latency to each turn, and it's a new way to fail — when the Judge is down the pipeline has to fail the turn loudly, and I felt a small version of that when the Toolbox wasn't running and I got a confusing 403. The other side: making the Guardrail a service too would buy nothing — it changes with the product, so I'd be versioning and deploying a separate service for a check that's inherently local, paying network latency and deployment coordination for process isolation it doesn't need. GR-3 already gives it a fresh model session per check, which is context isolation inside the process.

## Responsibilities

Which component decides that a customer may only see their own orders, and where exactly is
that line of code or SQL? Which component may hold an API key? Which one decides a turn is
over budget? For every rule in SPEC §11, say whether it is enforced **in code**, **in a
prompt**, or **both**, and why. This table is the core of `G-ENFORCE`.

1. Who decides a customer sees only their own orders, and where exactly?

Two halves, both in code.

Half one is the SQL in mcp_toolbox/tools.yaml, in get-order-status:
WHERE order_id = $1 AND customer_email = $2;
("Ownership lives here, not in the prompt.") find-customer-orders: WHERE customer_email = $1. That line is what keeps Alice out of Bob's order — the database itself won't return a row unless the order id and the email both match.

action-log enforces ownership too (M-8): its INSERT only fires WHERE ... EXISTS (SELECT 1 FROM customer_orders WHERE order_id = ... AND customer_email = $1) — so it refuses to log a cancel for someone else's order.

Half two is the binding in support/agent.py, in build_runner:
bound_params={"customer_email": email, "user_email": email}
The email comes from the login session and is plugged in when the toolset loads. The model never sees either parameter.

Why both: each half alone leaves a hole. The SQL filter without the binding is useless if the model fills in the email itself — it could pass Bob's address and the filter would return Bob's rows. The binding without the SQL filter is equally empty — a bound email changes nothing if the query were just WHERE order_id = $1. The binding guarantees the value is the logged-in user's and untouchable; the SQL guarantees the database only returns that user's rows.

Also: the agent's toolset holds only those three tools — nothing that can change customer_orders (R-3) — and verify-login sits in a separate auth toolset the agent never loads.

2. Which component may hold an API key?

My .env holds three keys (names only): GOOGLE_API_KEY, MEM0_API_KEY, and TOOLBOX_DB_PASSWORD, referenced in mcp_toolbox/tools.yaml as ${TOOLBOX_DB_PASSWORD}.

-  GOOGLE_API_KEY is held by the pipeline process for every ADK agent call, and by the Judge in its own process, since the Judge runs an ADK agent (pattern tool plus the model's own reading). The Masker never touches GOOGLE_API_KEY — it doesn't call a model at all; it masks with deterministic patterns, which is also why the mask step finishes in tens of milliseconds.
- MEM0_API_KEY — the pipeline process, for the recall and save steps.
- TOOLBOX_DB_PASSWORD — the MCP Toolbox process (:5000), to log in to Postgres as the least-privilege support_toolbox role; and ./run.sh reset, which sets that password on the role (run.sh:29).

What must never see a key: the model (never in a prompt, tool schema, or conversation), the browser (keys stay server-side), and the run logs and traces (a key in runs/<turn_id>.json or a Phoenix span would be persisted and visible). .env is in .gitignore.

3. Which component decides a turn is over budget?

Not built yet — it arrives with the pipeline in Stage 4. The limits are T-BUD-TOOLS (≤ 6 tool calls), T-BUD-TOKENS (≤ 30,000 tokens across all LLM layers), and T-BUD-WALL (≤ 30 s wall-clock; a turn past it ends with terminated: "cap").

The pipeline enforces them, not the model. Per R-2, anything that must happen every turn is a pipeline step, not a prompt instruction — and the model is the thing being bounded. It can't count its own tokens, can't watch a clock, and could be talked into "just one more tool call." So the pipeline counts calls and tokens and watches the wall clock itself, and ends the turn when a cap is hit.

4. Every SPEC §11 rule: whether it is enforced in code, in a prompt, or both, and why

| Rule | Enforced in | Where | Why |
|---|---|---|---|
| R-1 | Code | mcp_toolbox/tools.yaml get-order-status (WHERE order_id = $1 AND customer_email = $2); support/agent.py build_runner (bound_params={...}) | The precedent is "the prompt said X, the model didn't do X" — Alice got Bob's laptop under a prompt that said "only this user." |
| R-2 | Code | Planned: support/pipeline.py (Stage 4) | The prompt said "ALWAYS use search_memory" and Gemini skipped it — anything that must happen every turn goes in the pipeline. |
| R-3 | Code | mcp_toolbox/tools.yaml support-agent toolset (only the three tools; nothing mutating customer_orders) | The reference relied on "must NOT call this directly" — a prompt forbidding a loaded tool is not a control. |
| R-4 | Code | Planned: support/pipeline.py — a guard error ends the turn with an error event naming the step (P-4) | Live Translate served English for weeks because the error handler returned the input; the reference Guardrail returned "safe" on parse failure. |
| R-5 | Code | Planned: pipeline save step — writes only the user's message, never the agent's reply | Bob's case: the agent trusted its own old refusal over his stated preference. |
| R-6 | Both | Prompt: the guardrail's ADK prompt with this shop's examples (planned). Code: GR-3's fresh model session per check; GR-4's "unparseable decision is an error" | The scope itself can only live in the prompt — the incident was a copied prompt with the wrong examples. But the contract around it is code. |
| R-7 | Code | Planned: length check against T-MEM-MAXCHARS before a memory reaches the prompt | The 30,100-char loop — a prompt can't measure characters; code checks len(). |
| R-8 | Code | Built: only matched PII spans are substituted — the PII test reported "masked 1 email address" while the delivery address and everything else came through untouched | The .lower() bug survived because no check covered the text that wasn't supposed to change. |
| R-9 | Code | Planned: Judge returns an explicit verdict field; anything else is an error, never inferred "safe" | The Judge signaled "safe" by echoing the input unchanged — meaning must never be inferred from an echo. |
| R-10 | Code (architecture) | Phoenix as a separate process (:6006) with persistent storage | In-process in-memory Phoenix lost every trace across three restarts. |
| R-11 | Code | db/seed.sql CHECK constraint on actions_log.action_type (verified Stage 1: actions_log_action_type_check rejected RETURN_ITEM) | One afternoon logged the same intent three ways; only the schema stops that. |
| Grounded or nothing | Prompt (for now) | The agent's instruction: "Every order fact you state comes from a tool result in this conversation. Never guess." | Honest answer: today nothing in code forces a tool call before a fact — it's one sentence in the instruction. Every other row says Code; this is the one that doesn't — yet. |
| Fail loud | Code | Planned: support/pipeline.py (Stage 4), same mechanism as R-4 | Same lesson: Live Translate served English for weeks because the error handler returned the input instead of failing. |
| Bounded and honest | Code | Planned: support/pipeline.py enforces T-BUD-TOOLS / T-BUD-TOKENS / T-BUD-WALL (part 3) | The model is the thing being bounded — it can't count its own tokens or watch a clock. |
| Evidence over vibes | Code | Planned: The eval runner "(Stage 10)" — every graded number comes from a measured run | BUILD_LOG predictions are checked against pasted terminal output; thresholds are measured, not asserted. |

## Communication

How does a message travel from the CLI or browser to the database and back? Name each hop's
protocol (function call, MCP, A2A JSON-RPC, HTTP, NDJSON). Which A2A method did you
implement, and what does a verdict look like on the wire?

Part 1: one message, hop by hop

- CLI or browser → pipeline. CLI: same process — a plain function call into the pipeline module, no network. Browser: HTTP POST to :8000/api/chat. (CLI built; browser planned, Stage 9.)
- pipeline → Judge. A2A JSON-RPC over HTTP to :10002. (Planned, Stage 6.)
- pipeline → Guardrail. In-process function call — no network, no port; it's just the pipeline calling the guardrail directly. (Planned, Stage 7.)
- pipeline → Mem0 (recall). HTTPS API call to Mem0 cloud. (Planned, Stage 8.)
- agent → Gemini. HTTPS to Google's Gemini API, authenticated with GOOGLE_API_KEY. (Built.)
- agent → Toolbox. MCP over HTTP to :5000 via toolbox-core (the traceback showed mcp_transport) — the same Toolbox process curl hit: curl used its plain HTTP tool API, toolbox-core speaks MCP to the same port. (Built.)
- Toolbox → Postgres. Postgres wire protocol over TCP to 127.0.0.1:5432, logged in as support_toolbox. (Built.)
- pipeline → Masker. A2A JSON-RPC over HTTP to :10003, same pattern as the Judge. (Planned, Stage 8.)
- pipeline → Mem0 (save). HTTPS API call to Mem0 cloud, same as recall. (Planned, Stage 8.)
- pipeline → back to CLI or browser. CLI: same process — per P-1 the pipeline yields events one at a time and the CLI loops over them, printing each as it arrives (Stage 3's CLI already does this with runner.run_async(...) events). Same streaming idea as NDJSON, just no network in between. Browser: HTTP streaming response as application/x-ndjson (SPEC W-2) — newline-delimited JSON, one event per line, so the browser renders each event as it arrives instead of waiting for the whole turn. (CLI built; browser planned, Stage 9.)
- every step → Phoenix. OTLP over HTTP to localhost:6006/v1/traces — each pipeline step becomes a span. (Planned, Stage 5.)

Part 2: which A2A method?

Planned: message/send. It's the current A2A method, and these are simple request/response calls — send the text, get back a verdict — which is what message/send is for; tasks/send is the older draft. Final confirmation in Stage 6 when the services get built.

Part 3: what does a verdict look like on the wire?

The Judge's verdict rides inside a JSON-RPC response. Planned sketch of the envelope, assembled from the three pieces (Stage 6 will replace this with a real captured response):

```json
{
  "jsonrpc": "2.0",
  "id": "…",
  "result": {
    "role": "agent",
    "parts": [
      { "kind": "data", "data": { "verdict": "allow", "reason": "…" } }
    ]
  }
}
```

I chose a data part over a text part: a text part would be JSON inside a string — the same "JSON string inside JSON" I had to unwrap in Stage 2 — and every parse step is another chance at an unparseable verdict (P-4). A data part arrives as a real object, so there is nothing to parse.

The verdict itself is the explicit structured object { "verdict": "allow" | "block", "reason": "..." } (J-3) — never an echo of the input (R-9). The Masker's reply carries the masked text plus its report of what it changed, e.g. "masked 1 phone number" or "nothing to mask" (K-3). Anything else — unparseable, or no response at all — is an error that ends the turn loudly (P-4), never a silent pass.

## State

What is stored, where, and for how long: sessions, memories, traces, run logs, the action
log? Which of them contain customer data, and who can read each one? What happens to
memory when a customer's order changes after the memory was saved?

Sessions. Live in memory, inside the CLI process — support/agent.py uses InMemorySessionService, one session per logged-in user. They last until the CLI exits; then they're gone, nothing is written to disk. They hold the conversation (customer messages, tool calls), so yes, customer data. Who can read them: only the running process itself.

Action log. Lives in Postgres, in the actions_log table. It lasts until ./run.sh reset drops the tables — the reset wipes it along with everything else and reseeds from scratch. It holds customer data: the email, the action type, and the parameters JSON (which can include an order_id and a reason). Who can read it: a human with database access — but not the agent's own role. Per db/seed.sql:73-74, support_toolbox can INSERT into actions_log but can only read the id and action_type columns (so RETURNING works); it cannot read the email or parameters. The agent's database role can write a request for a human but can't read back other people's requests.

Memories. Live on Mem0's cloud servers — not on my machine. They last until deleted; effectively persistent. They hold customer data, but only what's allowed: per R-4, the save step stores only the user's message, never the agent's reply, and only from turns that weren't blocked. Who can read them: anyone holding MEM0_API_KEY — and Mem0 itself, as the host.

Traces. Live in Phoenix (:6006), a separate process with persistent storage (R-10) — they survive restarts. A span records each pipeline step: its name, timing, inputs and outputs. Yes, that can include an order's address — the tool-result spans carry whatever the database returned. Who can read them: anyone who can reach the Phoenix UI or API.

Run logs. Live on disk as runs/<turn_id>.json. They last until deleted — .gitignore keeps them out of git, but they stay on the machine, with one exception: SUBMISSION.md:15 requires submitting runs/failing/ with the deliberate failing turn, so that file does leave the machine. Yes, they contain the user's message and the turn's events, including tool results. Who can read them: anyone with filesystem access to the machine — and, for runs/failing/, the grader. I'll use a seed account (fake data) for the failing turn.

What happens when a memory goes stale? Alice says "my order 4 is processing," Mem0 saves it, then the order ships. Next week, the database is what's true — the memory is only a snapshot of what she said once. What stops the agent from trusting it: Grounded or nothing — every order fact has to come from a tool call in that turn, which the instruction already says ("Every order fact you state comes from a tool result in this conversation"). The stronger rule — trust tools over memories for order facts — is planned (SPEC R-3, Stage 8): it's not in the instruction today, and recall doesn't exist yet. That's consistent with the Responsibilities table, which honestly marks Grounded or nothing as Prompt (for now). Memories are for things like preferences ("leave packages at the back door"), never for order state. Order state always comes fresh from the database.

The Masker only cleans the reply the customer sees. The raw address is still sitting in five places: the tool-call event in the stream (visible to anyone watching --events or the web UI); the run log, since runs/<turn_id>.json stores the customer's message, email included (readable by anyone with filesystem access); actions_log.parameters in Postgres (readable by whoever holds the database credentials — the agent's own role can only read back id and action_type); the Phoenix trace, which captured the unmasked text in the agent and tool spans before masking (readable by anyone who can reach the Phoenix UI or API on localhost); and Mem0, since only the user's raw message is saved (readable by anyone holding MEM0_API_KEY, plus Mem0 itself). So the Masker is a display-layer guard, not a storage-layer one — it stops the leak the customer would see, not the copies the system keeps.

## Trade-offs

What did each guard cost you in latency (from your traces), and was it worth it? Did you
make the Guardrail fail closed, and what does that cost when Gemini is slow? If you changed
`T-MEM-MINSCORE` or argued against any threshold, give the evidence here, and keep the
original gate in your report.
The cutoff change:
I lowered T-MEM-MINSCORE from 0.25 to 0.20 after a genuine back-door preference scored 0.2487 and was skipped at the original cutoff, while an unrelated memory at 0.1208 was skipped under both. I accept the trade: memories scoring between 0.20 and 0.25 now reach the agent, adding noise and tokens to those prompts — most turns are unaffected, since most memories score nowhere near the boundary. I admit the evidence is weaker than it first looked — the same memory later scored 0.2771, which would have passed at 0.25 too — and I'll keep the original 0.25 gate in the Stage 10 report alongside the 0.20 value in use.
GR-4's cost:
When the Guardrail's reply can't be parsed, I end the turn in an error — and honestly, what the customer sees today is the raw error line, something like error guardrail → ✗ unparseable decision: '...', with no friendly message. That's a real cost, and a friendlier error message is something I'd add. I saw no parse failures across the 45 trial runs on the free-tier Lite model, but I accept the risk rises with model flakiness and timeouts. I still prefer this to the reference build, which passed on parse failure: a silent pass lets attacks through unnoticed, while a visible error is measurable, debuggable, and never mistaken for approval.