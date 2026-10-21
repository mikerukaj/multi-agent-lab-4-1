# Feature Specification: Production Hardening of the Multi-Agent Development Team

**Feature Branch**: `dev` (spec directory: `003-production-hardening`)

**Created**: 2026-10-19

**Status**: Draft (amended 2026-10-19 with the planning directives, see "Session
2026-10-19 (planning directives)")

**Input**: User description: "Harden this repo/system into a production-grade application. Add comprehensive observability, robust error handling, and recovery, cost optimization, a full test suite, and Docker-based deployment. No new agents are added, the focus is making the existing system enterprise-ready"

**Builds on**: [001-autonomous-dev-team](../001-autonomous-dev-team/spec.md) (PM Agent, Coder
Agent, PM → Coder handoff) and [002-qa-feedback-loop-mcp](../002-qa-feedback-loop-mcp/spec.md)
(QA & Debugger Agent, Coder ⇄ QA loop, MCP file tools, A2A broker). This feature changes
no agent's role and adds no agent. Where this spec says nothing, 001 and 002 still apply.

## Clarifications

### Session 2026-10-19

- Q: Should an interrupted or crashed run be resumable, or is ending cleanly with
  everything preserved enough? → A: Resumable. A run can be resumed from its last
  completed stage (the PM plan, a finished build task, the Coder's reflection, a finished
  QA review, or a finished fix round), keeping the spend already made.
- Q: Is the deployed application a one-shot command-line job or a long-running service?
  → A: A one-shot command-line job per requirement, as today. A network service interface
  is out of scope.
- Q: How does the containerized application reach a container engine to start sandboxes?
  → A: It uses the host's container engine, as a documented trust exception. Sandbox
  containers themselves never get engine access.
- Q: Besides re-using repeated context, may cost optimization move model calls to a
  cheaper model? → A: Yes, by agent. The PM Agent keeps the larger model it uses today;
  the Coder and QA agents switch by default to the smaller, faster model tier (Claude
  Haiku 5.5 or the current equivalent).
- Q: When two runs on one machine share the Coder's long-term memory store at the same
  time, must both run safely or should the second be refused? → A: Both run. Concurrent
  runs are allowed, and the shared memory store must stay consistent while several runs
  read and write it at once.
- Q: Should resumable runs exit with their own exit code, separate from runs that failed
  for good? → A: Yes. 0 completed; 1 failed for good; 2 usage error or refused resume; 3
  resumable (`dependency_unavailable`, `a2a_failed`, `budget_exceeded`); 130 (interrupt)
  or 143 (terminate) for `interrupted` by a signal.
- Q: Should this feature deliver a continuous-integration pipeline, or only make the
  suite ready for one? → A: No pipeline. The suite is made CI-ready (one command,
  non-interactive, clear exit status), but no CI configuration is added.
- Q: Which values must be scrubbed from logs, run records and reports? → A: Every
  environment value whose variable name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or
  `CREDENTIAL` (case-insensitive), plus any text shaped like an Anthropic API key, even
  if it is not in the environment.

### Session 2026-10-19 (planning directives)

The user's planning directives for `/speckit-plan` change or extend the answers above.
Where they conflict with an earlier line of this spec, the directive wins, and the
requirements below have been amended to match (plan.md, "Spec deltas" D1–D10):

- Distributed tracing is **in scope**, through LangSmith. Every agent invocation is a
  child span of one root trace per run attempt, with the metadata `agent_name`, `token_count`,
  `tool_calls` and `duration_ms` (D1, FR-214a).
- Every model call and every tool call is retried **at most 3 times** (not 4), with
  exponential backoff and jitter (D2, D3).
- A **circuit breaker** per dependency opens after 3 consecutive failures and half-opens
  after a configurable cool-down. While it is open, a caller gets a cached result or a
  structured error, and the run never crashes (D4, FR-209a–c).
- **Prompt caching has a configurable TTL** (D5, FR-225).
- Every run writes a **JSON cost report** to `docs/cost_reports/` with per-agent token
  use, per-agent cost and the total cost (D6, FR-222a).
- The deployment defines an agent service and a **Chroma service**. The agent waits for
  Chroma's health check, and `docker compose up` from a clean state runs the pipeline end
  to end (D7, FR-227a, FR-230a).
- LangSmith trace contents are redacted like every other output (D8, FR-219).
- The test suite has **unit tests per agent with fully mocked model responses**,
  **integration tests for PM + Coder and Coder + QA**, and **one end-to-end test** whose
  output must pass a supplied test fixture (D9, FR-235a–c).
- **Every model output is validated** against the project's `TypedDict` schemas before it
  is acted on (D10, FR-219a).

### Session 2026-10-20 (analysis follow-up)

- A permanent failure (refusal, invalid request, bad credentials) keeps the agent's
  existing end reason and exits 1. Only a temporary failure that ran out of retries, or a
  call refused by an open circuit, ends `dependency_unavailable` and can be resumed
  (FR-205, from `/speckit-analyze` finding I1).
- File-tool server restarts follow D3: each restart is one retry under FR-204, at most 3
  by default. US1 scenario 4 and the matching edge case were reworded to match (finding
  I2).
- QA's final report makes no model call, so it is always written, even at the budget cap.
  FR-223, FR-225a and US3 no longer speak of its cost "fitting" (finding I5).
- Tracing has one root per run *attempt*: a resumed run adds a root with the same run ID.
  FR-214a and US2 scenario 6 now match SC-214 and the tracing contract (finding I6).

## User Scenarios & Testing *(mandatory)*

The people in these stories are:

- **Operator**: deploys and runs the team, watches its runs, and is paged when they fail.
- **Requester**: submits a requirement and receives the program, tests and QA report.
- **Maintainer**: changes the code and relies on the test suite to catch regressions.
- **Budget owner**: is accountable for the model spend.

### User Story 1 - Runs Survive Transient Failures and End Cleanly (Priority: P1)

Today a single failed model call, a crashed file-tool server or a sandbox container that
won't start ends the whole run, and a run that is killed leaves no final record. After
this feature, the team tells temporary failures (rate limits, overloaded or briefly
unreachable model service, a file-tool server that dies, a sandbox container that fails to
start) apart from permanent ones (refusals, invalid credentials, bad requests). It retries
temporary failures a bounded number of times with growing waits, and fails permanent ones
at once with a clear cause. Whatever happens, including the operator stopping the run, the
run ends with a final record, its workspace intact, and no sandbox containers left behind.
The operator can then resume the run from its last completed stage, so the work and spend
before the interruption are not lost.

**Why this priority**: Every other improvement assumes runs reach an end the operator can
trust. Today one hiccup in the model service throws away a run's full spend.

**Independent Test**: Inject failures into the model service, the file-tool server and the
sandbox, one kind at a time, using offline fakes. Check that temporary failures are retried
and the run reaches the same outcome as an undisturbed run, that permanent failures end
the run immediately with the right end reason, and that stopping a run mid-way still writes
a final record and leaves no containers running. Then interrupt scripted runs inside each
stage, resume them, and check that each reaches the same end reason as an uninterrupted
run, redoing only the stage that was interrupted.

**Acceptance Scenarios**:

1. **Given** the model service returns a rate-limit or overload error on one call, **When**
   the agent makes that call, **Then** the call is retried after a wait (honoring any wait
   the service asks for), the run continues, and the retry is recorded.
2. **Given** a temporary failure persists past the retry limit, **When** the last retry
   fails, **Then** the run ends with a distinct end reason naming the failing dependency,
   the record states the cause and the number of attempts, the workspace is kept, and the
   command exits with code 3 (resumable).
3. **Given** the model service rejects a call as invalid or unauthorized, or the model
   refuses, **When** the error arrives, **Then** it is not retried and the run ends with
   the existing end reason for that agent and a message naming the cause.
4. **Given** the file-tool server process dies during a Coder or QA session, **When** the
   agent's next file call is made, **Then** a new server is started for the same workspace
   with the same protections and the call is made again. Each restart counts as one retry
   under FR-204, so there are at most 3 restarts for one call by default.
5. **Given** a run is in progress, **When** the operator sends a stop signal, **Then** any
   running sandbox container is stopped, pending A2A requests are cancelled, a final record
   with end reason `interrupted` is written, and the process exits within 30 seconds with
   exit code 130 (interrupt) or 143 (terminate).
6. **Given** a previous run was killed without warning, **When** the team next starts,
   **Then** sandbox containers left over from that run are found and removed.
7. **Given** a run that ended `interrupted`, `dependency_unavailable` or `a2a_failed`, or
   was killed without a final record, **When** the operator resumes it by its run ID, **Then** its
   workspace is restored to the state saved at the last completed stage, the run carries
   on from the next stage with the same run ID, settings, iteration count, token use and
   cost so far, and the completed stages are not repeated.
8. **Given** a run that ended `budget_exceeded`, **When** the operator resumes it with a
   higher budget, **Then** it continues from its last completed stage under the new cap;
   resuming it without raising the budget is refused.
9. **Given** a run that ended `complete`, `iteration_limit`, `rejected`, or with a refusal
   or other permanent failure, **When** the operator tries to resume it, **Then** the
   resume is refused with a message naming the end reason, and nothing is changed.
10. **Given** three consecutive calls to one dependency have each failed after their
    retries, **When** a fourth call to that dependency is made before the cool-down
    ends, **Then** it is not attempted: the circuit is open, and the caller gets the
    cached result for that exact model request if one exists, or a structured error
    naming the dependency, the circuit state and the time left in the cool-down. The run
    does not crash.
11. **Given** an open circuit whose cool-down has passed, **When** the next call is made,
    **Then** exactly one trial call goes through (half-open). Success closes the circuit;
    failure opens it again for a new cool-down.
12. **Given** the model returns output that does not match the expected schema, **When**
    the agent receives it, **Then** nothing in the output is acted on (no file written,
    no tool run, no task created), the problem is recorded, and the agent follows its
    existing malformed-output path.

---

### User Story 2 - Operators Can See What Every Run Is Doing (Priority: P2)

An operator watching a run, or investigating one afterwards, gets a structured,
machine-readable event record for each run. Every event carries the run ID, the agent,
the stage (plan, build task, reflect, QA review, fix round, final report), timings, and
the outcome. Model calls, tool calls, sandbox runs, A2A exchanges, retries and errors each
produce events. At the end of a run, a summary gives totals per agent and per stage: model
calls, tokens, cost, duration, retries and errors. The human-readable progress lines the
CLI prints today stay. Nothing secret ever appears in any of it.

**Why this priority**: Once runs survive failures (US1), the operator must be able to tell
why a run was slow, expensive or failed without reading code or rerunning it.

**Independent Test**: Run the team with fake agents through a successful run and through
each failure end reason. Check that the event record parses, that every event carries the
run ID, agent and stage, that the run summary's totals match the run record, and that a
planted API key appears in no output.

**Acceptance Scenarios**:

1. **Given** a team run, **When** it ends for any reason, **Then** a per-run event record
   exists, one structured event per line, each with a timestamp, run ID, agent, stage,
   event type and outcome.
2. **Given** a run that failed, **When** the operator reads only its event record and run
   summary, **Then** they can tell which agent and stage failed, the error, and what was
   retried before the failure.
3. **Given** a run in progress, **When** the operator lists the event record, **Then** events
   appear as they happen, not only when the run ends.
4. **Given** an API key or other secret in the environment, **When** any event, log line,
   error message or run record is written, **Then** the secret's value does not appear.
5. **Given** the operator sets a verbosity level, **When** the run proceeds, **Then** only
   events at or above that level are recorded, and the run summary is always written.
6. **Given** tracing is turned on with LangSmith credentials, **When** a run ends,
   **Then** LangSmith holds one root trace for each run attempt (the first run and each
   resume), all carrying the same run ID. Every PM, Coder and QA
   invocation, including the QA reviews and fix rounds that cross the A2A broker,
   appears as a span under that root, with `agent_name`, `token_count`, `tool_calls`
   and `duration_ms`.
7. **Given** tracing is not turned on, **When** a run or the test suite runs, **Then**
   nothing is sent to LangSmith, and the run behaves the same.

---

### User Story 3 - Model Spend Is Visible, Bounded and Reduced (Priority: P3)

The budget owner sees what each run cost, broken down by agent, and can set a spending cap
per run. When a run reaches its cap, it stops at the next safe point, keeps everything
built so far, still has QA write its final report (which makes no model call), and ends
with end reason `budget_exceeded`. The team also spends less for the same results: the
PM keeps the larger model, because a bad plan wastes every later call, while the Coder
and QA, which make most of a run's calls, move to a smaller, faster model. Context that
repeats across calls is re-used instead of paid for again, and unchanged material is not
sent again where the agents don't need it.

**Why this priority**: Each live run costs real money and the benchmark is ten runs. Spend
must be reported and capped before the system is offered to more requesters.

**Independent Test**: With fake model clients that report token counts, check the cost
calculation per agent against the price table, check that a run with a low cap stops with
`budget_exceeded` and a final record, and compare the tokens billed for a scripted run
before and after the context-reuse changes.

**Acceptance Scenarios**:

1. **Given** a completed run, **When** the operator reads the run record, **Then** it
   shows the cost per agent and the total, alongside the token counts it was computed
   from, including re-used context.
2. **Given** a per-run budget, **When** the run's spend reaches it, **Then** no further
   model call is started, QA's final report (which makes no model call) is still
   written, and the run ends `budget_exceeded` with its workspace and record intact.
3. **Given** a model ID with no known price, **When** the run starts, **Then** the run
   still proceeds, the cost is reported as unknown for that agent with a warning, and a
   budget cap set for the run causes a refusal to start rather than an unenforceable cap.
4. **Given** the benchmark, **When** it runs before and after this feature, **Then** the
   results file reports the cost per item and in total, so the two can be compared.
5. **Given** any run, however it ends, **When** it ends, **Then** a JSON cost report
   exists in `docs/cost_reports/` for that run, with each agent's prompt tokens,
   completion tokens and estimated cost, and the run's total cost.
6. **Given** prompt caching with a TTL set by the operator, **When** an agent repeats a
   context within that TTL, **Then** the repeated part is billed at the re-use rate and
   the cost report shows the re-used tokens.

---

### User Story 4 - The Team Deploys as a Container (Priority: P4)

An operator on a machine with only Docker installed builds the application image and
starts a team run with documented commands, supplying the API key at run time. Workspaces,
the Coder's long-term memory, event records and benchmark results persist outside the
container. The image holds no secrets, runs as a non-root user, and pins every version it
installs. A health check reports whether the application can reach what it needs: the
container engine for the sandbox, the sandbox image, a writable workspace, and model
credentials. The sandbox's safety guarantees from 002 (no network, no secrets, resource
limits, read-only tests) hold exactly as they do outside a container. Each run is a
one-shot command-line job, as today: one container run per requirement (or per resume).
The application container starts sandboxes through the host's container engine; this is a
documented trust exception, and sandbox containers themselves never get that access.

**Why this priority**: Deployment packages the hardened system. It's worth most once
failures are handled, visible and affordable (US1–US3).

**Independent Test**: On a clean machine with Docker, follow the deployment guide: build
the image, run the health check, run one requirement, then confirm the outputs are on the
host, that no secret is in the image or its layers, and that the generated code still ran
with no network.

**Acceptance Scenarios**:

1. **Given** a machine with Docker and the repository, **When** the operator runs the
   documented build and run commands with an API key supplied at run time, **Then** a team
   run completes and its workspace, QA report, event record and run record are on the host.
2. **Given** the built image, **When** its contents and history are inspected, **Then** no
   API key or `.env` file is present.
3. **Given** the application container, **When** generated code runs in the sandbox,
   **Then** that code has no network, sees no secrets, and is held to the same limits as
   outside a container.
4. **Given** a missing API key, an unreachable container engine or a missing sandbox
   image, **When** the health check or a run starts, **Then** it fails at once with a
   message naming what is missing, and no model call is made.
5. **Given** a clean machine state (no containers, volumes, images or previous outputs),
   **When** the operator runs the documented `docker compose up` command, **Then** the
   Chroma service starts and reports healthy, the agent service starts only after that,
   the pipeline runs end to end on the configured requirement, and the outputs are on
   the host.

---

### User Story 5 - A Full, Fast, Offline Test Suite Guards Every Change (Priority: P5)

A maintainer runs one command and, within minutes and without spending money, learns
whether a change broke anything: every module, every end reason, every retry and recovery
path, cost and budget logic, event output, secret redaction, and the deployment's health
check. The suite runs the same way on a laptop and inside the application container, and
is ready to be run unattended by a continuous-integration service later. Tests that need
Docker or a live model are clearly separated and skipped, with a reason, when
unavailable.

**Why this priority**: The suite already exists and is strong for 001/002 behavior. This
story extends it to everything this feature adds and makes it measurable.

**Independent Test**: Run the suite with no network and no API key. It finishes, makes no
paid model call, reports coverage, and fails if coverage drops below the threshold or if
any end reason or recovery path lacks a test.

**Acceptance Scenarios**:

1. **Given** no API key and no network, **When** the maintainer runs the suite, **Then** it
   completes, makes no paid model call, and reports passed, failed and skipped counts with
   a reason for each skip.
2. **Given** the suite, **When** it finishes, **Then** it reports statement coverage of
   project source and fails if coverage is below the agreed threshold.
3. **Given** the application container, **When** the suite is run inside it, **Then** it
   gives the same results as on the host.
4. **Given** a live smoke run is explicitly requested with credentials, **When** it runs,
   **Then** it exercises one end-to-end team run and reports its cost.
5. **Given** the suite, **When** it runs, **Then** it includes unit tests for each agent
   with fully mocked model responses, integration tests for the PM + Coder and
   Coder + QA pairs, and one end-to-end test of the full pipeline on a realistic task.
   The end-to-end test passes only if the produced program is executable Python that
   passes a supplied test fixture the agents never see.

---

### Edge Cases

- The model service keeps rate-limiting for longer than the retry limit allows: the run
  ends with the dependency-failure end reason, not a hang, and the waits are capped.
- The service's requested wait is longer than the remaining retry allowance: the run does
  not wait beyond its allowance; it ends with the cause recorded.
- A retried model call may already have been billed: its tokens are counted once if the
  service reports them, and the retry is still recorded.
- An A2A request times out while the other agent is still working: the work is cancelled,
  not retried, so a fix round is never applied twice.
- A stop signal arrives during a sandbox run, during a file write, or during the final
  report: the container is stopped, files already written are kept, and a partial final
  report is marked as partial.
- A second stop signal arrives during shutdown: the process exits at once, still without
  leaving containers behind where possible, and says so.
- The budget cap is reached in the middle of a Coder session: the session stops after the
  current call; the cap may be passed by at most that one call.
- The event record's destination is not writable or the disk fills: the run continues,
  progress lines still reach stderr, and the run record states that events were lost.
- The file-tool server keeps dying after each restart: restarts stop once the call's retry
  limit is used up (FR-204, FR-206; 3 by default), and are never repeated indefinitely.
  The call then returns the structured error from FR-209c naming the file-tool server,
  the attempts made and the circuit state, and the agent handles it on its existing
  tool-error path.
- Two or more runs use the Coder's long-term memory store at the same time: all of them
  proceed; each sees lessons that were fully written before its read, never a partial
  one, and no write is lost (FR-230a).
- A run is killed in the middle of writing to the shared memory store: the store stays
  readable by every other run, and the half-written lesson is either complete or absent.
- The deployed container is stopped by the container platform: it is treated as a stop
  signal (US1 scenario 5), and the run can be resumed from a new container.
- A run is killed while its checkpoint is being written: the previous complete checkpoint
  stays valid and is used on resume; a half-written checkpoint is never read.
- The workspace was changed by hand after the interruption: on resume it is restored to
  the checkpoint, and the record lists the files that were overwritten or removed.
- The operator tries to resume a run that is still running (in another process or
  container): the resume is refused.
- The checkpoint was written by a different version of the application, or is corrupt:
  the resume is refused with the reason; the run's files are left untouched.
- The operator tries to change settings (models, iteration limit, step cap) on resume:
  refused, except raising the budget; the run keeps the settings it started with.
- An A2A request was pending when the run was interrupted: it is not replayed; the stage
  that sent it runs again from its start after resume.
- The price table is out of date for a model: cost is reported with a warning; token
  counts remain the source of truth.
- Two agents share one dependency (the Coder and QA both on the smaller model): they
  share its circuit, so failures seen by one count for the other.
- A circuit opens while QA is running on the broker thread: QA's call degrades exactly as
  a main-thread call would, and the breaker's state stays consistent across threads.
- A cached response exists but fails validation: it is discarded and the caller gets the
  structured error instead.
- LangSmith is unreachable while tracing is on: the run is unaffected, and one warning
  is recorded.

## Requirements *(mandatory)*

### Functional Requirements

**Scope guard**

- **FR-201**: The system MUST NOT add an agent or change any agent's role, inputs or
  outputs as defined in 001 and 002. The existing command-line usage, end reasons and
  `TeamState` fields MUST keep their meaning; new fields and end reasons may be added.
  Exit codes keep their meaning except as FR-201a states.
- **FR-201a**: The team command MUST exit with: 0 when the run completed; 1 when it ended
  for good (`failed`, `rejected` or `error` with a non-resumable end reason); 2 for a
  usage error, a bad setting, Docker not being available, or a refused resume; 3 when it
  ended with a resumable end reason (`dependency_unavailable`, `a2a_failed`,
  `budget_exceeded`); and 130 or 143 when it ended `interrupted` by an interrupt or
  terminate signal respectively. `a2a_failed`, which exited 1 before this feature, now
  exits 3; the README and the deployment guide MUST state this change.
- **FR-202**: The sandbox guarantees in 002 (no network, no secrets, non-root, resource
  limits, read-only tests, refuse to run if the sandbox is unavailable) MUST hold in every
  deployment mode.

**Error handling and recovery (US1)**

- **FR-203**: Every failure of an external dependency (model service, file-tool server,
  sandbox, container engine, A2A broker) MUST be classified as temporary or permanent, and
  the classification MUST be recorded with the failure.
- **FR-204**: Temporary failures of every model call and every tool call MUST be retried
  up to a configurable limit (default 3 retries) with exponential backoff and random
  jitter, capped per wait (default 60 s), and MUST honor a wait the service asks for when
  it fits within the cap.
- **FR-205**: Permanent failures (refusal, invalid request, invalid or missing
  credentials) MUST NOT be retried. A permanent model failure MUST end the run with the
  failing agent's existing end reason (for example `pm_error` or `qa_failed`), never
  `dependency_unavailable`, so it exits 1 and cannot be resumed (FR-201a, FR-213b).
  Only a temporary failure that exhausted its retries, or a call refused by an open
  circuit, ends the run `dependency_unavailable`.
- **FR-206**: A file-tool server that exits during a session MUST be restarted for the
  same workspace and protections, and the failed call made again. Each restart counts as
  one retry under FR-204, so restarts are bounded by the same limit.
- **FR-207**: A sandbox container that fails to start for a reason other than the
  generated code MUST be retried under FR-204; the generated code's own errors and
  timeouts MUST NOT be retried.
- **FR-208**: A2A requests MUST NOT be retried automatically; the 002 timeout and
  cancellation behavior stands.
- **FR-209**: When retries are exhausted, the run MUST end with a new end reason,
  `dependency_unavailable`, naming the dependency, the last error and the attempts made.
- **FR-209a**: Each external dependency (model service per model, file-tool server,
  sandbox, memory store) MUST have a circuit breaker. It opens after 3 consecutive
  failed calls (a call fails when its retries are exhausted or it fails permanently),
  and any success resets the count. While it is open, calls to that dependency are not
  attempted.
- **FR-209b**: After a configurable cool-down (default 30 s), an open circuit MUST let
  exactly one trial call through (half-open). A successful trial closes the circuit; a
  failed trial opens it again for a new cool-down. Every state change MUST be recorded.
- **FR-209c**: A call that cannot be served (circuit open, or retries exhausted) MUST
  degrade gracefully and never crash the run. A model call returns the cached response
  to the identical request if one exists within its configurable age limit, validated
  like a live response. Otherwise, and for every tool call, the caller gets a structured
  error naming the dependency, the failure kind, the attempts, the circuit state and the
  cool-down remaining. Tool results are never served from a cache. A model call that
  stays unserved ends the run under FR-209; a tool call's structured error is returned to
  the agent, which carries on as its existing tool-error path does.
- **FR-210**: On a stop signal, the system MUST stop running sandbox containers, cancel
  pending A2A requests, write the final record with end reason `interrupted`, and exit
  within 30 seconds with the exit code for that signal (FR-201a). A second signal MUST
  exit immediately.
- **FR-211**: Every sandbox container MUST be labeled with its run ID, and at start-up
  the system MUST remove containers left by runs that are no longer running.
- **FR-212**: Every run, however it ends, MUST leave its workspace, any QA report written
  so far, and a final run record on disk.
- **FR-213**: The system MUST save a checkpoint for the run when each stage completes:
  the PM plan, each build task, each Coder reflection, each QA review and each fix round.
  A checkpoint MUST hold everything needed to continue (the run record so far, settings,
  token use and cost, QA's copy of its tests, and the workspace contents) and no secrets.
  A checkpoint MUST replace the previous one only once it is completely written.
- **FR-213a**: The operator MUST be able to resume a run by its run ID. A resume MUST
  restore the workspace to the checkpoint, continue from the stage after the checkpoint
  with the same run ID, and carry forward iteration count, token use, cost and A2A log, so
  that the iteration limit and budget apply to the run as a whole.
- **FR-213b**: Resume MUST be allowed only for runs that ended `interrupted`,
  `dependency_unavailable`, `a2a_failed`, or `budget_exceeded` (the last only with a
  higher budget), and for runs killed without a final record. It MUST be refused, with
  the reason and without changing any files, for every other end reason, for a run that
  is still running, and for a missing, corrupt or incompatible checkpoint.
- **FR-213c**: A resume MUST use the run's original settings; any attempt to change them,
  other than raising the budget, MUST be refused.
- **FR-213d**: Each resume MUST be recorded in the run record and the event record (when,
  from which stage, and which workspace files the restore changed), and the run summary
  MUST cover all attempts together.

**Observability (US2)**

- **FR-214**: Each run MUST write a structured event record, one event per line, to a
  per-run location the operator can configure, flushed as events happen.
- **FR-214a**: When the operator turns tracing on with LangSmith credentials, each run
  attempt (the first run and each resume) MUST produce exactly one root trace in
  LangSmith, and every root of the same run MUST carry its run ID and, for a resume, the
  stage it resumed from. Every agent invocation (each PM call, each
  Coder build task, reflection and fix round, each QA review and the final report) MUST
  appear as a span under that root, including invocations that run behind the A2A
  broker, carrying the metadata `agent_name`, `token_count`, `tool_calls` and
  `duration_ms`. Model calls and tool calls MUST appear as spans under their agent's
  span of the same attempt. With tracing off, nothing MUST be sent, and a tracing
  failure MUST NOT fail the run.
- **FR-215**: Every event MUST carry a timestamp, run ID, agent (or `team`), stage, event
  type, severity, and outcome; events for model calls MUST also carry model ID, tokens
  (including re-used context), cost and duration.
- **FR-216**: Events MUST be produced for: run start and end, each stage start and end,
  each model call, each tool call, each sandbox run, each A2A exchange, each retry, each
  restart, each budget check that stops work, and each error.
- **FR-217**: At the end of every run, a run summary MUST be written to the run record
  with totals per agent and per stage: model calls, tokens, cost, duration, retries and
  errors.
- **FR-218**: A verbosity setting MUST control which severities are recorded; the run
  summary and errors MUST always be recorded.
- **FR-219**: Secret values MUST be replaced by a fixed redaction marker in every event,
  log line, error message, run record, checkpoint, QA report, cost report, and trace
  sent to LangSmith (inputs, outputs and metadata). Secret values are: (a)
  the value of every environment variable whose name contains `KEY`, `TOKEN`, `SECRET`,
  `PASSWORD` or `CREDENTIAL`, case-insensitively, and (b) any text shaped like an
  Anthropic API key, whether or not it is in the environment. Empty values and values
  shorter than 8 characters are not treated as secrets under (a), so ordinary short
  words are not redacted. Generated program output and QA test output stored in the
  workspace are not scrubbed; they are redacted only where they are copied into the
  outputs above.
- **FR-219a**: Every model output MUST be validated against the project's `TypedDict`
  schema for that call before anything in it is acted on. This covers structured replies
  from every agent, the inputs of every tool call the model requests, and cached
  responses served under FR-209c. Output that fails validation MUST NOT be used; the
  agent MUST take its existing malformed-output path, and the problem MUST be recorded as
  an event.
- **FR-220**: The existing human-readable progress lines on stderr MUST continue, and
  MUST be derivable from the same events.
- **FR-221**: A failure to write events MUST NOT fail the run; the run record MUST note
  that events were lost.

**Cost (US3)**

- **FR-222**: The system MUST track, per agent, prompt tokens (including re-used and
  newly cached context) and completion tokens, compute each run's cost per agent and in
  total from those counts and a configurable price table, and include it in the run
  record and benchmark results.
- **FR-222a**: At the end of every run, however it ends, the system MUST write a JSON
  cost report to `docs/cost_reports/` (configurable), named by run ID, with per-agent
  token use, per-agent cost estimate and total run cost. The file MUST be written
  atomically, MUST contain no secrets, and MUST be rewritten with cumulative totals when
  the run is resumed. A failure to write it MUST be reported and MUST NOT change the
  run's outcome.
- **FR-223**: The operator MUST be able to set a per-run budget (no cap by default). When
  spend reaches it, no new model call may start. QA's final report, which makes no model
  call, is still written, and the run ends `budget_exceeded`.
- **FR-224**: A budget cap with a model of unknown price MUST cause the run to refuse to
  start, naming the model.
- **FR-225**: Context that repeats across an agent's calls (system instructions, tool
  definitions, specification, unchanged source files) MUST be marked for re-use (prompt
  caching) so that repeated sends are billed at the service's reduced rate where
  offered. The cache lifetime MUST be configurable among the values the service supports
  (5 minutes by default, or 1 hour), and prompt caching MUST be possible to turn off.
- **FR-225a**: The PM Agent's default model MUST stay the larger model it uses today. The
  Coder and QA agents' default model MUST be the smaller, faster model tier (Claude Haiku
  5.5 or the current equivalent), for every call they make, including the Coder's
  self-reflection and QA's test writing and review. (QA's final report makes no model
  call.) Each agent's model MUST
  remain configurable, so an operator can move either agent back to the larger model.
- **FR-225b**: Every agent behavior that the default models' capabilities affect (for
  example, how much reasoning effort is requested, or server-side refusal fallback) MUST
  either work on the smaller model or be adjusted for it, and the run MUST refuse to start
  with a clear message if an agent is configured with a setting its model doesn't support.
- **FR-226**: The benchmark MUST report cost per item and in total, and a run of the full
  benchmark MUST be compared against a baseline recorded before this feature.

**Deployment (US4)**

- **FR-227**: The application MUST be packaged as a container image built from a
  versioned definition in the repository, with every base image and package version
  pinned.
- **FR-227a**: The repository MUST define a multi-service deployment (a `Dockerfile`
  and a `docker-compose.yml`) with an agent service and a Chroma service for the Coder's
  long-term memory. The Chroma service MUST have a health check, and the agent service
  MUST NOT start until it reports healthy; the agent MUST also retry its first
  connection under FR-204. Started from a clean state (no containers, volumes, images
  or previous outputs), the documented `docker compose up` command MUST run the pipeline
  end to end and exit with the agent's exit code.
- **FR-228**: The image MUST contain no secrets; credentials MUST be supplied at run
  time.
- **FR-229**: The application container MUST run as a non-root user.
- **FR-230**: Workspaces, the Coder's long-term memory, event records, run records and
  benchmark results MUST persist on the host across container restarts.
- **FR-230a**: Several runs, in separate processes or containers on the same host, MUST
  be able to use the same Coder long-term memory store at the same time. Concurrent reads
  and writes MUST NOT corrupt the store, lose a completed write, or expose a partly
  written lesson, and a run that dies mid-write MUST NOT block or break the others. This
  guarantee is provided through the Chroma service (FR-227a, or a Chroma server the
  operator runs). The embedded, file-based store used without a server supports one run
  at a time, and the README MUST say so.
- **FR-230b**: Concurrent runs MUST NOT share anything else that can collide: each has its
  own run ID, workspace, checkpoint, event record and sandbox containers, and start-up
  cleanup (FR-211) MUST remove only containers of runs that are no longer running.
- **FR-231**: A health check MUST verify credentials are present, the container engine is
  reachable, the sandbox image exists (or can be built), and the workspace is writable,
  and MUST report each failing item by name without making a paid model call.
- **FR-232**: The deployed application MUST be a one-shot command-line job: one
  container run per requirement or per resume, with the same options, output and exit
  codes as the command line outside a container. It MUST NOT open a network interface.
- **FR-233**: The application container MUST start sandboxes through the host's container
  engine. The deployment guide MUST state this as a trust exception (access to the engine
  equals administrator rights on the host) and explain how to limit it. Sandbox
  containers MUST NOT be given engine access, and generated code MUST see only its own
  run's workspace, as outside a container.
- **FR-234**: The repository MUST document how to build, configure, health-check, run,
  upgrade and remove the deployment, and list every configuration setting with its
  default.

**Test suite (US5)**

- **FR-235**: Every behavior added by this feature MUST have automated tests, including
  each failure classification, each retry and restart path, each circuit-breaker state
  change and degradation path, each end reason (including `interrupted`,
  `dependency_unavailable` and `budget_exceeded`), cost calculation, the cost report,
  prompt-cache markers, budget enforcement, output validation for every reply type, trace
  structure and metadata, event fields, secret redaction (including traces), the health
  check, the deployment definition, and checkpoint and resume
  (an interruption inside every stage, every refused-resume case, and a half-written
  checkpoint).
- **FR-235a**: Each agent (PM, Coder, QA) MUST have unit tests whose model responses are
  fully mocked, with no model service, container engine, file-tool server process or
  memory store involved. Existing agent unit tests that depend on any of these MUST be
  converted to mocks, with the real-dependency versions moved to integration tests.
- **FR-235b**: Integration tests MUST exercise each agent pair with the real agents and
  real team wiring: PM + Coder (plan handed to the Coder and built), and Coder + QA (a
  review, a fix round over the A2A broker, and a passing re-review).
- **FR-235c**: One end-to-end test MUST run the full pipeline on a realistic task and
  pass only if the produced program is executable Python that passes a supplied test
  fixture which the agents never see. It MUST run offline with scripted model responses
  by default, and on the real models only when explicitly requested.
- **FR-236**: The default test run MUST be offline, MUST make no paid model call, and MUST
  skip, with a stated reason, tests needing Docker or a live model when unavailable.
- **FR-237**: The test run MUST report statement coverage of project source and MUST fail
  below 85%.
- **FR-238**: The suite MUST be runnable with one documented, non-interactive command on
  the host and inside the application container, with the same results, and MUST signal
  success or failure (including a coverage shortfall) through its exit status alone, so
  that a continuous-integration service could run it unchanged.
- **FR-239**: Failure injection for tests MUST be possible through test fakes only; no
  failure-injection switch may exist in production configuration.

### Key Entities

- **Run Event**: one observable occurrence in a run. Timestamp, run ID, agent, stage,
  event type, severity, outcome, and type-specific details (model ID, tokens, cost,
  duration, attempt number, error class and message). Belongs to exactly one run.
- **Run Summary**: totals for one run, per agent and per stage: model calls, tokens,
  re-used tokens, cost, duration, retries, restarts, errors, and whether events were lost.
  Stored in the run record.
- **Failure Classification**: for each dependency failure, the dependency, whether it is
  temporary or permanent, the attempts made, and the final outcome.
- **Retry Policy**: per dependency, the most retries, the wait growth and cap, and which
  failures qualify. Configurable within safe bounds.
- **Circuit Breaker**: per dependency, its state (closed, open, half-open), the count of
  consecutive failed calls, when it opened, and the cool-down.
- **Cached Response**: a successful model response stored under a fingerprint of its
  request, with the time it was stored; served only when the dependency is unavailable
  and the entry is within its age limit.
- **Agent Span**: one agent invocation in a trace: agent name, token count, tool call
  count, duration, and its place under the run's root trace.
- **Cost Report**: the per-run JSON file: per-agent token use and cost, the total, the
  price-table version, the prompt-cache lifetime, the budget, and each run attempt.
- **Price Table**: per model ID, the price of input, output and re-used input tokens.
  Configurable; versioned with a date.
- **Run Budget**: the cap for one run, spend so far, and whether the cap stopped the run.
- **Run Checkpoint**: the saved state of a run at its last completed stage: the stage, the
  run record so far, settings, token use and cost, QA's test copy, a snapshot of the
  workspace, the application version that wrote it, and a marker that it is complete.
  One current checkpoint per run.
- **Resume Attempt**: one continuation of a run: when it happened, the stage it resumed
  from, any budget change, and the workspace files the restore changed.
- **Health Report**: each prerequisite checked, its status, and a message for each
  failure.
- **Deployment Configuration**: every setting the deployed application reads, with its
  default and whether it is secret.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-201**: With one in five model calls failing temporarily (offline, injected), at
  least 95% of scripted runs reach the same end reason as the same runs with no failures.
- **SC-202**: 100% of runs, including runs stopped by the operator and runs that exhaust
  retries, leave a final record, their workspace and an end reason; 0 sandbox containers
  remain 60 seconds after any run ends or the next start-up completes.
- **SC-203**: For every end reason, an operator who did not watch the run can name the
  failing agent, stage and cause from the event record and run summary alone, in under 5
  minutes.
- **SC-204**: Reported cost per run is within 2% of the cost computed by hand from the
  service's reported token counts and the price table.
- **SC-205**: The average cost per benchmark item falls by at least 40% against the
  pre-feature baseline (all agents on the larger model, no context re-use), while at
  least 8 of 10 benchmark runs still end with every test passing (002's SC-102 still
  holds) and 002's SC-103 (at least 70% of runs whose first draft fails a test end up
  passing) still holds.
- **SC-205a**: The median wall-clock time of a benchmark item does not increase against
  the pre-feature baseline.
- **SC-206**: No run overspends its budget by more than the cost of one model call.
- **SC-207**: A new operator with only Docker and an API key goes from cloning the
  repository to a completed team run in under 15 minutes, using only the documented
  commands.
- **SC-208**: With planted values for each secret-name pattern and a planted
  API-key-shaped string echoed by generated code and by a fake model reply, scans of the
  image, its history, and every event record, run record, checkpoint and QA report from
  the test suite and a benchmark run find 0 secret values.
- **SC-209**: The default test run finishes in under 5 minutes on a developer laptop, makes
  0 paid model calls, and covers at least 85% of project source statements, with every end
  reason and every recovery path exercised by at least one test.
- **SC-210**: Retries and restarts add no more than 5% to the median duration of runs in
  which no failure occurs.
- **SC-211**: Of scripted runs interrupted at a random point and then resumed, at least
  95% reach the same end reason as the same run uninterrupted, and the model calls repeated
  after a resume never exceed those of the one stage that was interrupted.
- **SC-212**: Saving checkpoints adds no more than 5% to the median duration of a run.
- **SC-213**: With 5 runs started at once against the same memory store, all 5 reach the
  end reason they reach when run alone, the store passes an integrity check afterwards,
  and every lesson each run reported writing can be read back.
- **SC-214**: In 100% of traced runs, LangSmith shows exactly one root trace per run
  attempt, every agent invocation appears under it with all four metadata keys, and the
  sum of the agent spans' `token_count` equals the run's total tokens in the cost report.
- **SC-215**: 100% of runs, including runs that fail, are interrupted or exceed their
  budget, leave a cost report in `docs/cost_reports/` whose per-agent costs add up to its
  total within $0.000001.
- **SC-216**: With every dependency forced to fail in turn (offline), 0 runs crash: each
  ends with an end reason, a final record and a cost report; and every call made while a
  circuit is open is answered within 1 second without contacting the dependency.
- **SC-217**: In scripted tests, 100% of malformed model outputs (missing field, wrong
  type, value outside the allowed set, unknown field, invalid tool input) are rejected
  before any file is written or any tool is run.
- **SC-218**: From a clean state, the documented `docker compose up` command completes a
  pipeline run end to end with exit code 0, and the agent service's first log line comes
  after the Chroma service reports healthy.

## Assumptions

- **No change to what agents do.** The Coder and QA move to a smaller model (FR-225a),
  and prompts may change to enable context re-use (FR-225) or to suit the smaller model,
  but each agent's role, inputs, outputs and checks stay as in 001 and 002. The benchmark
  (SC-205) guards the quality of the results.
- **If the smaller model misses the quality bar.** If the benchmark shows fewer than 8 of
  10 passing with the Coder and QA on the smaller model, the plan may move one of them
  back to the larger model by default; SC-205's cost target then applies to the
  configuration that meets the quality bar, and the result is recorded in the plan.
- **Constitution applies.** Observability, retries and cost tracking are built on the
  standard library and the approved SDKs. Constitution 1.3.0 approves `langsmith` for
  tracing and `coverage` as a development-only tool for the coverage gate.
- **One run per process.** Concurrency between runs comes from running several
  processes or containers at once (FR-230a); within a process, runs stay sequential as
  today. The expected scale is a handful (up to about 5) of concurrent runs on one host.
- **Default retry settings** (3 retries, exponential backoff with jitter, waits capped at
  60 s) follow the planning directive and common guidance for rate-limited model
  services, and are configurable. The circuit breaker opens after 3 consecutive failed
  calls, with a 30 s cool-down by default.
- **The response cache serves only degraded calls.** It is not consulted while a
  dependency is healthy, so normal runs always use live model output.
- **No budget cap by default**, so existing users see no change until they set one.
- **The price table ships with prices for the configured default models** and is
  updated by the operator when prices change; token counts remain authoritative.
- **Cost baseline**: a full benchmark run before implementation records the baseline cost
  per item; until it exists, SC-205 can't be measured.
- **Event record format** is one structured, machine-readable event per line, readable by
  common log tools; shipping events to a central log service is out of scope, but the
  format must allow it.
- **Distributed tracing is in scope through LangSmith only** (FR-214a). Tracing is off
  unless the operator supplies LangSmith credentials. The local event record and run
  summary remain the always-on observability surface. Metrics dashboards, alerting, and
  tracing backends other than LangSmith are out of scope.
- **Authentication, multi-tenancy and a network service interface are out of scope.**
  The deployment is a one-shot command-line job (FR-232).
- **Engine access.** 002's rule that no container gets the container engine's socket is
  narrowed by this feature to sandbox containers: the application container gets it
  (FR-233), sandboxes never do. The README must be updated to say so.
- **Checkpointing and the constitution.** The constitution forbids LangGraph checkpointers
  without an amendment. The plan must either keep checkpointing in the project's own
  code on the standard library, or propose that amendment with its justification.
- **Checkpoint storage** lives beside the run's workspace and is kept as long as the
  workspace is; deleting old runs is the operator's choice.
- **The Coder's long-term memory is not rolled back on resume.** It is shared across runs
  and only accumulates lessons, so lessons stored during an interrupted stage stay.
- **Continuous integration is out of scope.** No CI pipeline or provider configuration is
  added; the suite is only made ready for one (FR-238). Until a pipeline exists, the
  constitution's "full suite passes before merge" rule is enforced by running the
  documented command before each merge.
