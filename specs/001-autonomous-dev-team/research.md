# Research: Autonomous Multi-Agent Development Team (Increments 1 and 2)

**Date**: 2026-10-15 (R1–R11, Increment 1: Coder Agent); 2026-10-17 (R12–R19, Increment 2: PM Agent and handoff) | **Plan**: [plan.md](plan.md) | **Spec**: [spec.md](spec.md)

All Technical Context unknowns are resolved below. Choices fixed by the user (LangGraph,
Claude Sonnet 5.5, Chroma, sandboxed execution, 5-turn window, 10-step cap) are
recorded as decisions, with the remaining open details researched around them.

## R1. LLM access: Claude Sonnet 5.5 through the official Anthropic SDK

- **Decision**: Call `claude-sonnet-5-5` with the official `anthropic` Python SDK (1.x),
  credentials from `ANTHROPIC_API_KEY` (or an `ant auth login` profile). Model ID is
  configurable via `CODER_MODEL`, defaulting to `claude-sonnet-5-5`.
- **Rationale**: The user named Sonnet 5.5. The first-party SDK is the supported path for
  tool use, structured outputs, refusal handling, and typed errors; it auto-retries 408/409/
  429/5xx with backoff (covers the spec's "retry transient failures" requirement, FR-023,
  with `max_retries=3`).
- **Alternatives considered**:
  - *Reuse the existing `openai` client routed through Helicone/OpenRouter.* Avoids a new
    dependency, but OpenAI-compatible shims don't expose Claude-specific features
    (structured `output_config`, `stop_details`, thinking blocks, fallbacks) reliably.
  - *`langchain-anthropic` chat model.* Adds a LangChain layer on top of LangGraph with no
    capability we need; violates Principle III (overlapping dependency).

## R2. Request settings for Sonnet 5.5

- **Decision**:
  - `thinking={"type": "adaptive", "display": "summarized"}` so each step returns a readable
    reasoning summary, which becomes the ReAct **Thought** recorded in the trace.
  - `output_config={"effort": "medium"}` (configurable). Medium is the recommended starting
    point for agentic coding and multi-step tool use on this model.
  - `tool_choice` left at the default `auto`. Forced `any`/`tool` returns a 400 on Sonnet
    5.5; tools use `strict: true` for schema-valid arguments instead.
  - `max_tokens=16000` (non-streaming; keeps requests under SDK HTTP timeouts).
  - Server-side refusal fallback enabled by default:
    `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"],
    fallbacks="default")`. Can be switched off by config.
  - Always check `stop_reason` before reading content; `refusal` ends the run with status
    `refused` and the `stop_details` category in the result.
- **Rationale**: Matches current Sonnet 5.5 API behavior (adaptive thinking is the default;
  `disabled` thinking and `budget_tokens` are rejected; thinking display defaults to
  `omitted`, which would leave the Thought empty).
- **Alternatives considered**: `thinking={"type": "between_tools"}` (lowest thinking
  setting). Rejected for now: multi-step coding benefits from thinking, and adaptive at
  `medium` is the documented starting point. Can be revisited after measuring cost.

## R3. Conversation history must be append-only (preserved thinking)

- **Decision**: Within a run, the message list is **append-only**: the top-level `system`
  prompt and `tools` list are frozen for the run, every assistant turn is appended with its
  full `response.content` (including thinking blocks, unchanged), and nothing earlier is
  ever edited or removed.
- **Rationale**: Sonnet 5.5 binds thinking blocks to the exact earlier conversation; for
  accounts created on/after 2026-08-31 the API rejects requests whose `system`, `tools`, or
  earlier messages changed since a thinking block was produced. This rules out the Lab 1.1
  pattern of rebuilding the system prompt with fresh memories on every call.
- **Consequence**: retrieved memories are injected by *appending* (see R5), never by
  rewriting the system prompt.

## R4. Orchestration: LangGraph `StateGraph`

- **Decision**: Model the ReAct loop as a LangGraph `StateGraph` over a plain `TypedDict`
  state with four nodes and one conditional router:

  ```text
  START → recall → think ──(tool_use, step_count < 10)──→ act → recall → think …
                       ├──(end_turn)───────────────────→ finalize → END
                       └──(step_count == 10 / refusal / error)→ finalize → END
  ```

  - `recall`: query semantic memory + append any new memories (R5).
  - `think`: one Claude call → **Thought** (thinking summary + text) and **Action**
    (`tool_use` blocks); increments the step counter.
  - `act`: run every requested tool, append all `tool_result` blocks in **one** user message
    (**Observation**); store observations in semantic memory.
  - `finalize`: produce the structured result (R7).
  - The graph is compiled once per `CoderAgent`; LangGraph's own `recursion_limit` is set
    above the node budget (`4 × max_steps + 5`) so our counter, not LangGraph, decides
    when to stop.
- **Rationale**: User-mandated framework. The graph maps one-to-one onto Thought → Action →
  Observation and gives the later PM → Coder ⇄ QA orchestration a common substrate. Only
  `langgraph` core (`StateGraph`, `START`, `END`) is used — no LangChain model wrappers,
  prebuilt agents, or checkpointers.
- **Alternatives considered**: Plain `while stop_reason == "tool_use"` loop (simplest;
  preferred by Principle III but overruled by the user's explicit requirement — see plan
  Complexity Tracking); `langgraph.prebuilt.create_react_agent` (hides the loop counter
  and needs LangChain chat-model wrappers); SDK beta Tool Runner (no explicit node-level
  control over step counting and memory recall).

## R5. Two-tier memory

- **Decision**:
  - **Short-term**: `SlidingWindowBuffer(max_turns=5)` where one *turn* is one completed
    exchange (user task + agent's final explanation). It persists across `run()` calls on
    the same `CoderAgent`. At the start of a run, the window is rendered once into the
    run's first user message as "Recent conversation" — stable for the whole run (R3).
  - **Long-term**: Chroma collection `coder_memory` (cosine space) in a
    `chromadb.PersistentClient` at `.chroma/` (configurable; already git-ignored). Stored
    records: each task, each run's final explanation, and each tool observation (summarised
    to ≤ 2,000 chars), with metadata `{run_id, kind, created_at}`.
  - **Retrieval before every LLM call**: the `recall` node always runs before `think`. Query
    text = task + latest observation; `top_k=3`; filter `run_id != current run` (the current
    run is already in the message history). Memories not yet shown in this run are appended
    as a mid-conversation `{"role": "system"}` message (supported on Sonnet 5.5, no beta)
    after the latest user/tool-result message. Already-injected memory IDs are tracked in
    state so nothing is repeated.
- **Rationale**: Satisfies "embed and retrieve relevant past context before every LLM call"
  while keeping history append-only. The system-role channel carries operator authority and
  is not mistaken for user prompt injection the way text inside a `tool_result` can be.
- **Alternatives considered**: Rebuilding the system prompt per call (breaks R3); putting
  memories inside `tool_result` content (can be read as injection); counting a *turn* as a
  single message like Lab 1.1 (a 5-message window would start mid-exchange).

## R6. Embeddings: Chroma's built-in default embedding function

- **Decision**: Use Chroma's default embedding function (`all-MiniLM-L6-v2` via ONNX,
  bundled with `chromadb`; model downloaded once to `~/.cache/chroma` on first use).
- **Rationale**: Anthropic has no embeddings endpoint. The default function needs no extra
  API key, no extra package, and no network after the first download — the smallest
  dependency footprint (Principle III).
- **Alternatives considered**: OpenAI `text-embedding-3-small` via Helicone (as in Lab 1.1:
  keeps a second LLM provider and key in the critical path); Voyage AI (new dependency and
  key).

## R7. Structured output

- **Decision**: `finalize` makes one final Claude call (same frozen `system`/`tools`, history
  appended) with `output_config={"format": {"type": "json_schema", ...}}` requesting only
  what the model alone knows: `plan` (ordered list of steps actually taken), `explanation`,
  and `main_file`. The system then fills in facts from ground truth:
  - `code` ← contents of `main_file` read from the workspace;
  - `execution_result` ← the last `exec_python` observation for `main_file`; if the model
    never ran it, `finalize` runs it once itself.
  Result is a stdlib `dataclass` (`CoderResult`) with `to_dict()`; JSON-serialisable.
- **Rationale**: Guarantees the four required fields are present and valid; prevents the
  model from *claiming* an execution result that never happened. Raw JSON schema via
  `output_config` avoids importing `pydantic` directly (it is only a transitive dependency,
  and Principle III forbids undeclared imports).
- **Alternatives considered**: `client.messages.parse()` with a Pydantic model (needs a
  direct `pydantic` import); a forced `submit_result` tool (forced `tool_choice` is a 400
  on Sonnet 5.5); parsing free text (fragile).

## R8. Tools and sandboxing

- **Decision**:
  - `read_file(path)` / `write_file(path, content)`: paths are relative to the run's
    workspace directory `workspace/<run_id>/` (git-ignored). Absolute paths and any path
    resolving outside the workspace (`..`, symlinks) are rejected with an error result.
    Files are UTF-8; `read_file` caps at 200 KB and says so explicitly when truncated.
  - `exec_python(path)`: runs `python -s <path>` in a **new, throwaway Docker container per
    call** (`docker run --rm`), built from `python:3.12-slim` pinned by digest, with
    `--network none`, `--read-only` (plus a 64 MB `/tmp` tmpfs), `--cap-drop ALL`,
    `--security-opt no-new-privileges`, `--memory 512m --cpus 1 --pids-limit 128`, the host
    user's uid:gid, and only the run's workspace mounted at `/work`. The environment holds
    only `HOME=/tmp`, `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1`; no stdin. The
    timeout (`CoderConfig.exec_timeout_seconds`, default 30 s; env `CODER_EXEC_TIMEOUT`)
    includes container start-up; on timeout the container is killed by name and
    `timed_out=True` is returned. stdout/stderr are capped at 10,000 chars each, with a
    `[truncated]` marker.
  - Tool errors are returned to the model as `tool_result` with `is_error: true`, never
    raised out of the loop.
  - All three tool definitions use `strict: true` with `additionalProperties: false`.
- **Rationale**: Generated code is untrusted. A host subprocess — the original design — runs
  as the user, can read any of the user's files, has full network access, and can read the
  agent's environment (including `ANTHROPIC_API_KEY`) through `/proc/<pid>/environ` even
  when its own environment is stripped. On 2026-10-17 the user asked for the more secure
  option in every trade-off, so each run gets its own locked-down container.
- **Alternatives considered**: running the whole agent in one container with generated code
  as a subprocess (the API key is still readable through `/proc`, and the network is open);
  giving the agent container the Docker socket so it can start sandboxes (root-equivalent
  on the host); a long-lived runner container on an internal network (runs share a
  filesystem and processes). The agent therefore runs on the host, `ensure_sandbox()`
  refuses to start without Docker, and no container gets the Docker socket.
- **Spec alignment**: Matches FR-021 as amended on 2026-10-17 (run containers have no
  network). Package installs happen in a separate install container (FR-011a, R11).

## R9. Testing

- **Decision**: stdlib `unittest` (constitution, Python Standards), discoverable with
  `python -m unittest discover -s tests -t .` (pytest, already installed, can also run them).
  - Offline unit tests: tools (path confinement, timeout, error results) and memory (window
    eviction, store/retrieve with a temporary Chroma directory).
  - Offline graph test: a fake client that always requests a tool proves the 10-step
    cap ends the run with status `step_limit` and a complete structured result.
  - Live smoke test (the user's requirement 6): runs a short Python task against the real
    model and asserts the result has `code`, `explanation`, `plan`, `execution_result`
    with the right types. Skipped automatically when no Anthropic credentials are set.
- **Rationale**: Exercises every new behavior without network by default; the live test
  proves the real end-to-end path.

## R10. Dependencies

- **Decision**: Add `anthropic` (1.x) and `langgraph` (1.x) to `requirements.txt`; keep
  `chromadb` and `python-dotenv`; remove `openai`. Add `pysqlite3-binary` (Linux only):
  Chroma needs SQLite 3.35+, RHEL 8 ships 3.26, and the agent must run on the host to start
  sandbox containers; `agents/memory.py` swaps it in only when the system SQLite is too old. Pin exact versions at implementation
  time. The Lab 1.1 files (`agent.py`, `memory.py`, `test_memory.py`) are unfinished
  skeletons and the only code importing `openai`, so they are retired in the same change
  (recoverable from the initial commit).
- **Rationale**: Minimum set that satisfies the user's mandated stack, with no overlapping
  LLM clients (constitution v1.1.0, Principle III).

## R11. Package installs (decided 2026-10-17; not built in this increment)

- **Decision** (user chose option C): a separate install step runs before any code that
  needs third-party packages.
  - The Coder declares packages in `requirements.txt` in the run's workspace, with a
    one-line reason for each, recorded in the run record (FR-011).
  - The system runs `pip install --only-binary=:all: --target /work/.packages -r
    requirements.txt` in a **throwaway install container**: same pinned Python 3.12 image,
    network enabled, no credentials or host environment variables, only the run's
    workspace mounted, non-root, capabilities dropped, memory/CPU/time limits (FR-011a,
    FR-021a).
  - `--only-binary=:all:` means only wheels are installed. Wheels are unpacked, not
    executed, so neither package code nor generated code runs while the container has
    network. Source-only packages fail to install and are reported to the Coder as a
    finding.
  - The run sandbox (no network, R8) then adds `/work/.packages` to `PYTHONPATH`, so code
    and tests import the installed packages without any network access.
  - The delivered dependency file is the workspace `requirements.txt` with the versions
    that were actually installed (FR-011).
- **Rationale**: Keeps the clarified scope ("anything", any package with a reason) while
  generated and package code never runs with network access or secrets (FR-021, FR-021a).
- **Residual risk**: an unvetted or typosquatted package can run inside the sandbox; it
  cannot reach the network or secrets, but the user takes it on if they install the
  delivered code elsewhere. The run record lists every package and reason for review.
- **Alternatives considered**: standard library only (narrows FR-008's scope); a
  pre-built approved package set in the sandbox image (needs a maintained list, rejects
  other packages); an allowlist enforced at install time (more maintenance for little
  extra safety once code runs without network).
- **Open details for that increment's plan**: a host-side wheel cache shared across runs
  (read-only to the sandbox), the install container's time limit, whether the install
  container should reach only the package index (an egress proxy) rather than the whole
  internet, and pinning installed versions with hashes.


# Increment 2: PM Agent and PM → Coder Handoff (2026-10-17)

Fixed by the user: same framework (LangGraph), both agents in one orchestration graph, a
shared `TypedDict` state with the requirement, spec, task list, per-task status and
accumulated code, a JSON-serializable handoff carrying task ID, description, acceptance
criteria and priority, and a cap of 8 tasks enforced by consolidation. The open details are
settled below.

## R12. Shared state schema: `TypedDict` all the way down

- **Decision**: `TeamState` is a `typing.TypedDict` (LangGraph's native state type, as
  Increment 1's `AgentState`). Every structured value inside it is also a `TypedDict`
  (`TechnicalSpec`, `Task`, `TaskProgress`, `CodeOutput`, `Consolidation`, `TokenUsage`),
  with `Literal` types for closed value sets (priority, task status, run status). All of
  them live in `orchestration/state.py`. No `dict` without a declared shape enters the
  state; the only plain mapping, `task_progress`, is typed `dict[str, TaskProgress]`.
- **Rationale**: A `TypedDict` is a plain `dict` at runtime, so the state, and the
  handoff in particular, goes through `json.dumps` with no conversion step, which meets
  the user's "serializable to JSON" rule by construction. It is standard library
  (Principle III), and it matches the existing `AgentState`. Model output is already
  schema-checked by structured outputs (R15), and the remaining rules (non-empty lists,
  ≤ 8 tasks, unique IDs) are checked in code.
- **Alternatives considered**:
  - *Pydantic models.* They give runtime validation and could generate the JSON Schema.
    But Pydantic is only a transitive dependency, so using it means declaring and
    justifying a new direct dependency (Principle III; research R7 avoided it for the same
    reason). LangGraph would also hold model instances that need `model_dump()` before
    JSON.
  - *Dataclasses* (as `CoderResult`). These need `asdict()` before JSON and add a second
    style beside the `TypedDict` state. `CoderResult` stays a dataclass; the team state
    copies the fields it needs into `TaskProgress`.
- **Drift guard**: the PM's JSON Schema for structured output is written by hand next to
  the types. A unit test checks that its property names match the `Task` /
  `TechnicalSpec` keys, so the two cannot drift silently.

## R13. Orchestration: one `StateGraph`, the Coder's loop runs inside the `coder` node

- **Decision**: `orchestration/team.py` builds one `StateGraph(TeamState)` with five nodes
  and two conditional routers:

  ```text
  START → pm ─┬─(rejected / error)──────────────────────────────→ finish → END
              ├─(> 8 tasks)──→ consolidate ──┐
              └─(1–8 tasks)──────────────────┴→ dispatch → coder ─┬─(pending, no failure)→ dispatch
                                                                  └─(else)───────────────→ finish
  ```

  The `coder` node calls `CoderAgent.run(task_text, workspace)`. That runs Increment 1's
  compiled Coder graph in the same process and thread, as a function call inside the
  team graph's node. Node functions are public module-level functions with explicit
  arguments (`pm_node(state, pm)`, `coder_node(state, coder)`, …), bound with
  `functools.partial` when the graph is built.
- **Rationale**: This meets "both agents in the same orchestration graph, no separate
  processes" with no new LangGraph API (only `StateGraph`/`START`/`END`, as the
  constitution allows). The Coder keeps its own private message history and step
  counter, which should not leak into shared state (the shared state stays small and
  JSON-ready). Public node functions let the tests run "the pm node" directly and check
  the state, as the user asked.
- **Alternatives considered**:
  - *Flatten the Coder's `recall/think/act/finalize` nodes into the team graph.* This
    mixes the Coder's Claude message history (SDK objects, not JSON) into the shared
    state and needs a large rewrite of tested code.
  - *Add the compiled Coder graph as a subgraph node.* This needs the two state schemas
    to share keys, or a wrapping node anyway. It adds coupling for no gain.
  - *A `DevTeam` class holding the graph.* There is no state beyond the two agents, so
    `build_team_graph(pm, coder)` plus `run_team(...)` has fewer moving parts.
- **Recursion limit**: `invoke`/`stream` gets `recursion_limit = 2 × MAX_TASKS + 5`
  (21), above the worst case of pm + consolidate + 8 × (dispatch + coder) + finish = 19
  steps.

## R14. Handoff protocol: the `handoff` state key holds one `Task`

- **Decision**: The handoff is the state key `handoff: Task | None`. A `Task` has
  `task_id`, `title`, `description`, `acceptance_criteria` (non-empty `list[str]`) and
  `priority` (`"high" | "medium" | "low"`).
  - **Ready signal**: `handoff` is not `None`. Only the `dispatch` node (PM side) sets it:
    it takes the first task in `tasks` order whose progress is `pending`, copies it into
    `handoff`, and marks it `in_progress`.
  - **Consume and acknowledge**: only the `coder` node reads `handoff`. After the
    task it writes the task's `TaskProgress` (`completed` or `failed`), appends its
    `CodeOutput` entries, and sets `handoff` back to `None`. At most one task is in
    flight.
  - **IDs**: the system assigns `T1`…`Tn` by position after every PM output, so IDs are
    unique and stable within the run. The model never writes IDs.
  - **Wire format**: `json.dumps(state["handoff"])`. The JSON Schema is in
    [contracts/task-handoff.schema.json](contracts/task-handoff.schema.json).
- **Rationale**: A state key is the simplest structured channel inside one graph. Reusing
  `Task` for the handoff, rather than adding a separate message type with the same
  fields, avoids two types drifting apart (Principle II). With only one sender and one
  receiver, sender/recipient fields would never vary, so the message has none.
- **Priority semantics**: tasks run in list order, which the PM is told to set by
  dependency first and priority second. `priority` goes to the Coder in its prompt
  ("focus on the high-priority parts first") and is recorded in the result. It does not
  reorder tasks, because reordering by priority could run a task before the task it
  depends on.
- **Alternatives considered**: a queue of pending handoffs (more than one in flight is
  not needed and breaks "in order"); an envelope `{sender, recipient, task, sequence}`
  (constant fields); letting the Coder pick from `tasks` itself (no explicit "ready"
  signal, and the user asked for one).

## R15. PM Agent: one structured-output call, no tools

- **Decision**: `PMAgent.write_spec(requirement)` makes one
  `client.beta.messages.create` call on `claude-sonnet-5-5` (the project's model, R1) with:
  - a frozen PM system prompt;
  - `output_config={"effort": "high", "format": {"type": "json_schema", "schema": SPEC_SCHEMA}}`.
    `high` because breaking a requirement into tasks is judgment-heavy, it is one call per
    run, and the cost is small. Configurable as `PM_EFFORT`;
  - `thinking={"type": "adaptive"}` (display omitted: the PM's reasoning is not part of
    the record);
  - the same server-side refusal fallback as the Coder (`PM_USE_FALLBACKS`, default on).
    The settings both agents use (`EFFORT_LEVELS`, `MAX_TOKENS`, `FALLBACK_BETA`) and the
    refusal-message helper move from `agents/coder_agent.py` to a small `agents/llm.py`,
    so the PM does not import the Coder (which would also load Chroma and the tools)
    and the beta name lives in one place;
  - `max_tokens=16000`, non-streaming; no tools.

  `SPEC_SCHEMA` asks for `name`, `description`, `acceptance_criteria[]`, `assumptions[]`,
  `tasks[]` (each `title`, `description`, `acceptance_criteria[]`, `priority` enum) and
  `rejection_reason` (empty string when the requirement is buildable). The system then
  assigns task IDs.
- **What the PM prompt says**: write tasks for the Coder only, not test-writing tasks
  (FR-013: tests belong to QA); order tasks by dependency, then priority; aim for 8 or
  fewer; record every assumption instead of asking (FR-007); and work within the Coder's
  current limits (Python standard library, no network), recording an assumption when the
  requirement needs more. Set `rejection_reason` only for empty, nonsensical or
  non-software requirements (FR-004), never because a requirement is large (FR-008).
- **Validation in code**: if a spec is not rejected but has no tasks, or a task has no
  acceptance criteria, that is a PM error (`PMResult.error`), as are refusals, API errors
  and unparseable output. The run then ends with status `error` before any handoff.
- **Schema limits**: structured outputs support `enum` but not array-size constraints
  such as `maxItems`, so the 8-task cap cannot be left to the schema (R16).
- **Alternatives considered**: a ReAct loop with tools for the PM (it has nothing to look
  up or run); a `submit_spec` tool (forced `tool_choice` is a 400 on Sonnet 5.5);
  free-text parsing (fragile); `client.messages.parse()` with Pydantic (R12).

## R16. Loop prevention: cap of 8, one consolidation, a deterministic fallback

- **Decision**: `MAX_TASKS = 8` (a constant: the user fixed it). After `pm`, the router
  sends any plan with more than 8 tasks to `consolidate`, which:
  1. makes one PM call (`PMAgent.consolidate(spec, requirement)`) with the requirement and the current
     spec as JSON. It asks for at most 8 tasks that copy every acceptance criterion word
     for word and in order, and uses a tasks-only schema built from the same task item
     schema. `PMAgent.consolidate` accepts the reply only if it passes three checks:
     `validate_tasks` (≥ 1 task; every title, description and criteria list non-empty;
     valid priorities), at most 8 tasks, and `criteria_preserved`. That last check
     flattens the original and merged criteria in task order, normalizes letter case and
     spacing, and requires the original sequence to appear, in order, within the merged
     one. Otherwise the reply counts as an error;
  2. if that call fails or any check fails, falls back to `merge_tasks(original tasks, 8)`:
     a pure function that splits the ordered list into 8
     contiguous groups of near-equal size and merges each group. Titles are joined with
     " + ", descriptions are kept as numbered parts, acceptance criteria are
     concatenated, and the group's highest priority wins;
  3. reassigns IDs `T1`…`Tk`, rewrites `spec.tasks`, `tasks` and `task_progress`, and
     records `consolidation = {proposed_count, final_count, method: "pm" | "merge"}`.

  `consolidate` always leads to `dispatch`. No edge goes back to `pm` or `consolidate`.
- **Why the team loop always ends**: consolidation runs at most once, and after it
  `len(tasks) ≤ 8`. Each `dispatch` moves one task from `pending` to `in_progress`, and
  each `coder` moves it to `completed` or `failed`. Nothing moves a task back to
  `pending`, so there are at most 8 `coder` visits. The recursion limit (R13) is a
  backstop.
- **Rationale**: The user asked the PM to consolidate, so the model gets one chance to
  merge sensibly. FR-006a requires every criterion and the order to survive, and only
  code can guarantee that, so the PM's merge is checked rather than trusted. Merging the
  *original* tasks on failure means a bad PM reply never feeds the fallback. The
  deterministic fallback means the cap holds even when the model does not comply,
  without a retry loop that would itself need limiting.
- **Checking criteria**: comparing after normalizing case and spacing tolerates harmless
  reformatting, and allowing extra criteria tolerates the model adding a combined check.
  A reworded criterion fails the check on purpose: rewording can change what is tested,
  and the prompt asks for word-for-word copies, so a failure should be rare. Rejected:
  fuzzy matching (a threshold no one can test against) and exact equality (fails on
  whitespace).
- **Alternatives considered**: retry consolidation up to N times (yet another loop and
  more spend); reject the requirement (breaks FR-008); keep the first 8 tasks (drops
  scope silently).

## R17. One workspace per team run

- **Decision**: `CoderAgent.run(task, workspace=None)`. When `workspace` is given, the
  Coder uses that folder (created if missing) instead of making `workspace/<coder run
  id>/`. `run_team` creates `workspace/<team run id>/` once and passes it for every task.
  Each task still gets its own Coder run ID (for memory records), and
  `CoderResult.files_written` lists only that task's writes.
- **Rationale**: Later tasks extend earlier tasks' files. Separate folders per task would
  hide earlier code from the Coder. The change is additive: existing callers and the
  Coder's CLI behave as before.
- **Context for each task**: `coder_node` renders the Coder's task text from the
  `handoff` and the spec: the spec's name and description, "Task T2 of 5 (priority
  high): title", the description, the acceptance criteria as a checklist, and the files
  already in the workspace with a note to read them before changing them. The Coder's
  frozen system prompt is unchanged (R3), and its sliding window carries the earlier
  tasks' summaries.

## R18. Per-task status and failure policy

- **Decision**: Task status moves `pending → in_progress → completed | failed`, and
  `pending → skipped` at the end of a failed run. A task is `completed` only when the
  Coder's result status is `completed`; `step_limit`, `refused` and `error` make it
  `failed`, with the Coder's status kept in `coder_status`. After a failed task the
  router goes to `finish`, which marks the remaining tasks `skipped`. Run status ends as
  `completed` (every task completed), `failed` (a task failed), `rejected` (PM rejected
  the requirement), or `error` (PM error).
- **Accumulated code**: after each task, `coder_node` reads each file the task wrote from
  the workspace (ground truth, as R7) and appends one `CodeOutput{task_id, path, content}`
  per file. A later task that rewrites a file appends a newer entry. The run's final code
  is the workspace itself.
- **Rationale**: This is the spec's FR-009a (clarified 2026-10-17). Later tasks build on
  earlier ones, so continuing past a broken task wastes spend. Stopping early saves spend and gives an explicit failure result (SC-003).
- **Alternatives considered**: continue after a failure (cascading failures, wasted
  spend); retry the task (another loop; this is QA's job in the next increment).

## R19. Testing

- **Decision**: stdlib `unittest`, all offline:
  - `tests/test_pm_agent.py`: a fake client returns scripted structured outputs. Tests
    cover spec parsing and ID assignment, rejection, refusal, API error, malformed JSON,
    an empty task list or criteria counting as an error, the consolidation call, and
    `merge_tasks` (order kept, ≤ 8, criteria and highest priority kept).
  - `tests/test_team_handoff.py`: **the user's required test**. Run `pm_node` on a fresh
    `TeamState` with a fake PM client, then assert that `tasks` is non-empty, that every
    task has exactly the `Task` keys with the right types and a valid priority, that IDs
    are `T1…Tn`, that `tasks == spec["tasks"]`, and that every `task_progress` entry is
    `pending`. Further tests: `dispatch_node` publishes the first pending task and that
    `json.dumps(handoff)` round-trips with exactly the contract's keys and priority values
    (hard-coded in the test, because `specs/` is git-ignored and absent from a fresh clone); `coder_node` (fake Coder) records progress and code and
    clears `handoff`; the full graph runs tasks in order, with progress visible after
    each `coder` step (via `stream`); a 9+ task plan is consolidated before the first
    handoff, including the deterministic fallback; a failed task leaves the rest
    `skipped`; a rejection never reaches the Coder; and the final state passes
    `json.dumps`.
  - `tests/test_coder_agent_loop.py`: `run(task, workspace=…)` writes into the given
    folder, and two runs share it.
- **Rationale**: Covers every new behavior without network or Docker. The fake Coder is
  any object with `run(task, workspace) -> CoderResult`, the same duck-typed injection as
  the Coder's fake client. The live end-to-end check is a quickstart scenario, not a
  unit test, because it spends real tokens and needs Docker.
