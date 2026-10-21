# Feature Specification: QA & Debugger Agent, Coder ⇄ QA Feedback Loop, and MCP File Tools

**Feature Branch**: `dev` (spec directory: `002-qa-feedback-loop-mcp`)

**Created**: 2026-10-18

**Status**: Draft

**Input**: User description: "Complete the three-agent team by adding a QA & Debugger agent. Introduce an iterative Coder -> QA -> Coder feedback loop that runs until all tests pass or a maximum iteration count is reached. Integrate MCP server to expose the, read_file and write_file tools."

**Planning input (2026-10-19)**: "Implement Google's A2A protocol to facilitate
communication between the Coder agent and QA agent. Use Python's asyncio to set up a
broker that decouples the messages between the agents." This adds FR-129–FR-133,
SC-109–SC-110 and the related entities and assumptions.

**Builds on**: [001-autonomous-dev-team](../001-autonomous-dev-team/spec.md). Its PM Agent,
Coder Agent and PM → Coder handoff are delivered. This feature adds the third agent and
the loop that 001 describes in User Story 2 and FR-012–FR-020. Where this spec says
nothing, 001's requirements still apply.

## Clarifications

### Session 2026-10-18

- Q: When QA writes the tests, how much of the Coder's code may it look at first? → A: QA
  takes expected behavior only from the specification. Before writing tests, it reads only
  the program's public interface (function and class names, parameters, and how to run it
  from the command line). It reads the full code afterwards, for the review and to
  diagnose failures.
- Q: After iteration 1, may QA add new tests to the suite, or is it fixed except for
  corrections after a dispute? → A: QA writes the full suite in iteration 1. Later, it may
  only correct a test after a dispute, or add a test that reproduces a bug it found in
  review, traced to an existing acceptance criterion.
- Q: Should a style problem ever stop a run from succeeding when all the tests pass?
  → A: Only a breach of an explicit MUST rule in the constitution's Clean Code or Python
  Standards sections can block. Every other style point is advisory.
- Q: When the Coder runs out of steps partway through a fix round, should the whole run
  fail, or should QA review whatever the Coder fixed? → A: Each fix round is one Coder
  session with the usual step cap. If it runs out of steps, the round ends and QA reviews
  the code as it stands, which uses up that iteration. Only a refusal or a model-service
  failure stops the run.
- Q: During a fix round, may the Coder read QA's test files, or only the findings QA
  sends? → A: The Coder may read test files but never change them. QA's review treats
  code that special-cases test inputs as a blocking bug.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - QA Tests and Reviews the Finished Program (Priority: P1)

When the Coder Agent has built every task, the QA & Debugger Agent takes over without any
user input. It reads the specification and only the program's public interface (how to
call it), and writes an automated test suite whose expected results come from the
specification's acceptance criteria. It then runs the suite in the same isolated sandbox
the Coder uses, and reads the full code to review it for bugs and style problems and to
diagnose any failures. It then gives a verdict:
the program is complete, or here are the findings that must be fixed.

**Why this priority**: The third agent is the missing piece of the team. Without it, a
run delivers code no one has checked, and the loop in User Story 2 has nothing to act on.

**Independent Test**: Run the team on a small requirement and confirm that, after the
Coder finishes, QA writes a test suite, runs it, and reports either "complete" or a list
of findings. Each test must trace to an acceptance criterion, and the reported pass and
fail counts must match what the tests actually do when run outside the system.

**Acceptance Scenarios**:

1. **Given** the Coder has completed every task, **When** QA runs, **Then** it writes a
   test suite covering every acceptance criterion in the specification, including the edge
   cases the specification names, and runs the full suite.
2. **Given** the full suite passes and QA's review finds no blocking problems, **When**
   QA reports, **Then** it declares the program complete and the run ends successfully.
3. **Given** one or more tests fail, **When** QA reports, **Then** each failure becomes a
   finding that states the problem, where it is in the code, the expected behavior, and
   QA's diagnosis of the likely cause.
4. **Given** the code passes its tests but has a bug or style problem that QA's review
   finds, **When** QA reports, **Then** the problem is listed as a finding. A bug is
   blocking. A style problem is blocking only if it breaks a named constitution MUST
   rule, and is advisory otherwise (FR-104a).
5. **Given** a Coder task failed while building (001 FR-009a), **When** the run ends,
   **Then** QA never runs.

---

### User Story 2 - Coder ⇄ QA Feedback Loop Until Tests Pass (Priority: P2)

When QA reports blocking findings, the Coder Agent gets them, revises the code, and hands
the program back to QA, which runs the full test suite again and reviews the new version.
This repeats on its own until QA declares the program complete or the maximum number of
iterations is reached. The user can set that maximum when starting a run.

**Why this priority**: First drafts of generated code are often wrong. The loop is what
turns plausible code into working code. It depends on User Story 1.

**Independent Test**: Give the team a requirement whose obvious first implementation
misses an edge case (for example, dividing by zero or handling empty input). Confirm that
QA reports the defect, the Coder fixes it, the next iteration passes, and the run record
shows both iterations. Then run with a maximum of 1 iteration on a requirement that fails
at first, and confirm the run ends in failure after exactly one iteration, keeping the
latest code and listing the unresolved findings.

**Acceptance Scenarios**:

1. **Given** QA reports blocking findings and iterations remain, **When** the next
   iteration starts, **Then** the Coder receives every finding and answers each one by
   fixing it or by disputing it with a reason based on the specification.
2. **Given** the Coder has revised the code, **When** QA runs again, **Then** it re-runs
   the whole test suite, not only the tests that failed before, and reviews the changed
   code.
3. **Given** the Coder disputes a test or finding, **When** QA handles the dispute,
   **Then** QA checks it against the specification, either keeps or corrects the test,
   and records the dispute and its decision.
4. **Given** the maximum number of iterations is reached while blocking findings remain,
   **When** the run ends, **Then** it reports failure, says the iteration limit ended it,
   keeps the latest code and test suite, and lists the unresolved findings.
5. **Given** QA declares the program complete in any iteration, **When** that happens,
   **Then** the loop stops at once and no further iterations run.
6. **Given** the Coder tries to create, change or delete a test file, **When** it does,
   **Then** the attempt is refused with an error the Coder can read, and the test suite
   is unchanged.
7. **Given** the Coder has finished building or fixing, **When** it is about to hand the
   program to QA, **Then** it first writes a critique of its own code (at least one
   potential bug or edge case) and revises the code against it, and both are in the run
   record (FR-126).
8. **Given** any handoff between the Coder and QA (a review request or a fix request),
   **When** it happens, **Then** it travels as an A2A message through the broker, with no
   direct call between the agents, and the run record shows the exchange with its task
   states (FR-129, FR-132).

---

### User Story 3 - File Tools Served Over MCP (Priority: P3)

The `read_file` and `write_file` tools are offered through a Model Context Protocol (MCP)
server instead of being wired straight into each agent. The Coder and QA agents get their
file tools from this server. A developer can also start the server on its own, point it at
a workspace folder, and connect any standard MCP client to it to read and write files in
that folder under the same rules the agents follow.

**Why this priority**: It makes the tool layer reusable and inspectable through a
standard protocol, and it is a course requirement. The team still works without it, so it
ranks below the agent and the loop.

**Independent Test**: Start the server on its own against a sample workspace, connect a
standard MCP client, and confirm that it lists exactly two tools, `read_file` and
`write_file`, that a file written through one can be read back through the other, and
that paths outside the workspace are refused. Then run the team and confirm the agents'
file reads and writes go through the server.

**Acceptance Scenarios**:

1. **Given** the server has been started for a workspace, **When** a client asks which
   tools it offers, **Then** it lists `read_file` and `write_file` with their argument
   descriptions, and no other tools.
2. **Given** a connected client, **When** it writes a file and then reads it back,
   **Then** the content is identical, and the file is inside the workspace on disk.
3. **Given** a connected client, **When** it reads or writes a path that is absolute or
   escapes the workspace (through `..` or a link), **Then** the call fails with an error
   message, nothing outside the workspace is read or changed, and the server keeps
   running.
4. **Given** a team run, **When** the Coder or QA reads or writes a file, **Then** the
   call goes through the MCP server, and its results and errors are the same as with the
   direct tools today.
5. **Given** the server is asked to start with a workspace folder that does not exist,
   **When** it starts, **Then** it refuses to start and says why.

---

### Edge Cases

- **The first draft passes everything**: QA declares the program complete in iteration 1
  and the Coder is not called again.
- **A fix breaks a test that passed before**: Every iteration runs the full suite, so the
  regression is reported as a new finding.
- **Coder and QA go in circles** (a fix brings back an earlier finding): The iteration
  limit ends the run, and the record shows the repeated findings in each iteration.
- **The Coder runs out of steps in a fix round**: The round ends, and QA reviews the code
  as it stands in the next iteration. Unfixed findings carry over. This uses up an
  iteration but does not end the run (FR-116a).
- **The Coder cannot finish a fix round for another reason** (the model declines or the
  model service fails): The run ends in failure, names the fix round that failed, and
  keeps the latest code and test suite.
- **QA cannot finish its turn** (step limit, model declines, model service fails): The run
  ends with an error result that says QA failed, and keeps the code and any tests written
  so far. The program is never declared complete without a full test run.
- **QA's own test suite cannot load** (for example, a syntax error in a test file): This
  is QA's defect, not the Coder's. QA must fix it before it reports findings. A suite that
  never loads is not counted as passing.
- **A test or the code hangs**: Each test run has a time limit (001 FR-021). A timeout
  counts as a failure and becomes a finding.
- **QA writes a test the specification does not call for**: The Coder disputes it, and
  QA keeps or corrects it after checking the specification (User Story 2, scenario 3).
- **QA's review finds a bug no test covers**: QA reports it as a finding and adds a test
  that reproduces it, traced to an existing acceptance criterion (FR-107a). The bug
  finding already blocks completion in that iteration, and the new test runs in every
  later iteration.
- **The Coder hard-codes test values to make tests pass**: QA's review reports it as a
  blocking bug (FR-111a), so the program is not declared complete until the real
  behavior is in place.
- **Only advisory findings remain**: QA declares the program complete. Advisory findings
  are listed in the outcome but do not block success.
- **The user asks for a maximum outside the allowed range** (0, a negative number, or more
  than 10): The run does not start, and the user is told the allowed range.
- **Two MCP clients connect to the same standalone server**: Out of scope. One client per
  server is supported.
- **An MCP client sends a malformed call** (missing argument, wrong type): The call fails
  with an error message, and the server keeps serving.
- **The final-report request fails** (QA cannot be reached, the request times out, or QA
  fails while writing the report): The run's result and end reason, already decided,
  stay as they are. The run record has no report, and it says why. The iteration records
  still hold every finding, so no information is lost (FR-118).

## Requirements *(mandatory)*

### Functional Requirements

**QA & Debugger Agent**

- **FR-101**: The system MUST add a QA & Debugger Agent as the third agent of the team.
  It runs after the Coder has completed every task, and the team order becomes
  PM → Coder ⇄ QA (001 FR-003).
- **FR-102**: QA MUST write an automated test suite from the specification's acceptance
  criteria, covering normal behavior and every edge case the specification names. Each
  test MUST trace to at least one acceptance criterion (001 FR-015).
- **FR-102a**: Every expected result in a test MUST come from the specification, never
  from what the code currently does. Before writing tests, QA MUST read only the
  program's public interface: function and class names, their parameters, and how to run
  it from the command line. QA MUST read the full code only after the tests are written,
  for the review (FR-104) and to diagnose failures (FR-105).
- **FR-103**: QA MUST run the full test suite against every version of the code, inside
  the same isolated, network-less, time-limited sandbox the Coder uses, and record a
  pass or fail result for each test (001 FR-016, FR-021).
- **FR-104**: QA MUST review the code for bugs and style problems against the project
  constitution, and mark each finding as blocking or advisory (001 FR-017).
- **FR-104a**: A failing test or a bug is always blocking. A style finding MUST be marked
  blocking only when it breaks an explicit MUST rule in the constitution's Clean Code or
  Python Standards sections (for example, a wildcard import, a hard-coded secret, or a
  missing `__main__` guard on an entry-point script), and the finding MUST name that rule.
  Every other style finding MUST be advisory.
- **FR-105**: Each finding MUST state the problem, where it is in the code (file, and the
  function or line where possible), the expected behavior, and QA's diagnosis of the
  likely cause (001 FR-018).
- **FR-106**: QA MUST declare the program complete only when the whole suite has loaded
  and run, every test passes, and no blocking findings remain (001 FR-020).
- **FR-107**: QA alone MAY create, change or delete test files. Test files MUST be kept
  apart from the program's source files. FR-111 says how the Coder is kept out of
  them.
- **FR-107a**: QA MUST write the full test suite in iteration 1. In later iterations, QA
  MAY change the suite only in two ways: correcting a test after a dispute (FR-112), or
  adding a test that reproduces a bug QA found in its review, traced to an existing
  acceptance criterion. Each added test MUST be recorded in that iteration with the
  finding it reproduces. QA MUST NOT add tests for behavior the specification does not
  call for.

**Coder ⇄ QA feedback loop**

- **FR-108**: An iteration is one QA review of the whole program. Iteration 1 reviews the
  Coder's first draft of all tasks, and each later iteration reviews one round of the
  Coder's fixes. The iteration limit counts these reviews.
- **FR-109**: After an iteration with blocking findings, and while iterations remain, the
  system MUST pass every finding to the Coder, let it revise the code, and then start the
  next iteration, with no user input.
- **FR-110**: The Coder MUST answer every blocking finding, either by fixing it or by
  disputing it with a reason based on the specification (001 FR-012). Findings left
  unanswered when a fix round hits its step cap stay open for the next iteration
  (FR-116a).
- **FR-111**: The Coder MUST NOT create, change or delete test files. Any attempt MUST be
  refused with an error message the Coder can read, and MUST leave the test suite
  unchanged (001 FR-013). The Coder MAY read test files.
- **FR-111a**: QA's review MUST report, as a blocking bug, any code that special-cases
  the exact inputs or expected values in the tests instead of carrying out the behavior
  the specification asks for.
- **FR-112**: When the Coder disputes a test or finding, QA MUST decide the dispute by
  checking the specification, then either keep or correct the test, and MUST record the
  dispute, the decision, and the reason (001 FR-019).
- **FR-113**: The loop MUST stop as soon as QA declares the program complete, and the run
  then ends successfully.
- **FR-114**: The loop MUST stop when the maximum number of iterations is reached without
  QA declaring the program complete. The run then ends in failure and keeps the latest
  code and test suite. The final report states the reason (FR-118).
- **FR-115**: The maximum number of iterations MUST default to 5. The user MUST be able to
  set it when starting a run, to any whole number from 1 to 10, through an environment
  setting or a command-line option (the option wins when both are given). Values outside
  that range MUST be refused before the run starts.
- **FR-116**: If the Coder's fix round ends because the model declines or the model
  service fails, or QA cannot finish its turn, the run MUST end at once (failure for the
  Coder, error for QA) and keep everything produced so far. The message MUST name which
  agent stopped and in which iteration.
- **FR-116a**: Each fix round MUST be one Coder session that handles all of that
  iteration's findings, with the same step cap as one build task. If the Coder reaches
  its step cap, the fix round MUST end without ending the run. The next iteration then
  reviews the code as it stands, and any unanswered findings stay open. This still counts
  toward the iteration limit, and the record MUST note that the round hit the step cap.

**Coder self-reflection**

- **FR-126**: Every time the Coder is about to hand the program to QA (after building
  all tasks, and after each fix round), it MUST first reflect on its own code. It writes a
  critique of the current program that lists at least one potential bug or unhandled
  edge case, then runs a revision session that answers every item in the critique,
  either by changing the code or by saying why no change is needed. The program reaches
  QA only after the revision.
- **FR-127**: Reflection MUST run at least once before each handoff. The number of
  critique-and-revise rounds per handoff MUST default to 1, and the user can set it from
  1 to 3.
- **FR-128**: If a revision session reaches its step cap, the program MUST still go to
  QA as it stands, as in FR-116a. If the model declines or the model service fails during
  reflection, the run MUST end in failure as in FR-116.

**Agent-to-agent communication**

- **FR-129**: Every exchange between the Coder and QA MUST travel as a message in the
  Agent2Agent (A2A) protocol through a message broker. These exchanges are: the program
  handed from the Coder to QA for review, with QA's findings coming back as the reply;
  and QA's fix instructions sent to the Coder, with the Coder's answer coming back as the
  reply. The team's request for QA's final report (FR-118) also goes through the broker
  as an A2A message from the team to QA. Neither agent may call the other directly.
  Messages cross the broker as plain data, so the two agents never share in-memory
  objects. Each agent's internal work stays local and is not sent as messages: the
  Coder's build tasks and self-reflection, and QA's internal steps.
- **FR-130**: The broker MUST keep a separate queue for each recipient, so a sender only
  names the recipient. Each agent MUST handle its messages one at a time, in the order
  they arrive. Every reply MUST be matched to the request it answers.
- **FR-131**: Each agent MUST publish an agent card listing what it can do (its skills)
  and the protocol features it does not support. A message to an unknown agent, for an
  unknown skill, or with an unsupported protocol version MUST be refused with the matching
  A2A error, and the run then ends with an error.
- **FR-132**: The run record MUST list every exchange in order. For each one it records
  the sender, the recipient, the skill, the A2A task ID, each state the task passed
  through, the start and end times, and the outcome. All exchanges in one run MUST share
  one conversation ID, and each fix request MUST point to the review it answers.
- **FR-133**: A request that gets no final answer within a time limit (default 30
  minutes) MUST be cancelled where possible. The run then ends with an error naming the
  agent that did not answer.

**Run record and outcome**

- **FR-117**: The run record MUST keep, for each iteration: its number, the files the
  Coder changed, each test's result, the pass and fail counts, QA's findings with their
  severity, any tests QA added or corrected, any disputes with their decisions, and the
  Coder's self-reflection before that iteration (its critique items and the files the
  revision changed).
- **FR-118**: For every run that reaches QA, QA MUST produce a final report. When the
  iteration limit ends the run, the report is how the loop stops instead of running on.
  The report and the run's final result MUST state success or failure, the reason the run
  ended (complete, iteration limit, or which agent failed), how many iterations ran, the
  final pass and fail counts, and every unresolved finding. If the request for the
  report itself fails, the run's result stands and the run record says the report is
  missing and why (see Edge Cases).
- **FR-119**: While a run is in progress, the system MUST report each iteration's start
  and its pass and fail counts as it goes, in the same style as the PM and Coder progress
  lines that exist today.

**MCP file-tool server**

- **FR-120**: The system MUST provide an MCP server that offers exactly two tools,
  `read_file` and `write_file`, with the same arguments, behavior, size limits and error
  messages as the existing file tools (001 contracts/tools.md).
- **FR-121**: Every server instance MUST be tied to one workspace folder, set when it
  starts. All reads and writes MUST stay inside that folder. Absolute paths and paths
  that escape it (through `..` or links) MUST be refused with an error, and nothing
  outside the folder may be read or changed.
- **FR-122**: Tool failures (missing file, refused path, malformed call) MUST come back to
  the caller as error results. They MUST NOT stop the server.
- **FR-123**: In a team run, the Coder and QA agents MUST get `read_file` and `write_file`
  through the MCP server for that run's workspace, not by calling the file functions
  directly. Code execution stays outside the MCP server, as it is today.
- **FR-124**: A developer MUST be able to start the server on its own, from the command
  line, for a chosen workspace folder, and connect a standard MCP client to it. The
  server MUST refuse to start if that folder does not exist.
- **FR-125**: The server MUST NOT read, receive or expose credentials or host environment
  variables to its clients beyond what it needs to start (001 FR-021a).

### Key Entities

- **Test Suite**: The automated tests QA owns for one run. Each test traces to an
  acceptance criterion and has a pass or fail result per iteration. It is kept apart from
  the program's source files.
- **QA Finding**: One problem QA reports: a failing test, a bug, or a style problem. It
  has a location, the expected behavior, a likely cause, a severity (blocking or
  advisory), and a state (open, fixed, disputed, or withdrawn).
- **Dispute**: The Coder's objection to a test or finding, its reason, and QA's decision
  (keep or correct) with QA's reason.
- **Iteration**: One QA review of the whole program. It links the Coder's changes, the
  test results, the findings and the disputes. Iterations are numbered from 1.
- **Iteration Limit**: The most iterations a run may use. It defaults to 5, the user can
  set it from 1 to 10, and it is fixed once the run starts.
- **Self-Reflection**: The Coder's review of its own code before a handoff to QA: the
  critique items (potential bugs and edge cases, at least one) and the revision that
  answered them.
- **QA Report**: QA's final summary of a run that reached QA: the result, why the run
  ended, the iterations run, the final pass and fail counts, and every unresolved
  finding.
- **Agent Card**: An agent's published description: name, skills, the protocol version
  and transport it supports, and the features it does not support.
- **A2A Message / Task**: One request from one agent to another (a message with a text
  summary and a structured payload), and the unit of work it starts on the recipient,
  with its states (submitted, working, completed, failed, canceled, rejected) and its
  result (an artifact).
- **Message Broker**: The in-process service that queues each agent's incoming
  messages, delivers them in order, matches replies to requests, and logs every
  exchange.
- **QA Verdict**: QA's conclusion for one iteration: complete, or findings to fix.
- **MCP File-Tool Server**: The service that offers `read_file` and `write_file` for one
  workspace folder to the agents or to an outside MCP client.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-101**: Every run that reaches QA ends with an explicit result (success, iteration
  limit, or agent failure). No run uses more iterations than its limit or loops without
  end.
- **SC-102**: On 001's benchmark of 10 small requirements, at least 8 runs end with every
  test passing and no human input after the requirement.
- **SC-103**: Of the benchmark runs whose first draft fails at least one test, at least
  70% reach a fully passing state within the default iteration limit.
- **SC-104**: In 100% of runs, the delivered test suite gives the same pass and fail
  counts when the user runs it outside the system as the system reported.
- **SC-105**: In 100% of attempts, a Coder write to a test file is refused and the suite
  is unchanged.
- **SC-106**: A standard MCP client connected to the standalone server can list the two
  tools and do a write-then-read round trip in under 1 minute of setup, following the
  project's instructions.
- **SC-107**: In 100% of attempts, through the MCP server or directly, a read or write
  outside the workspace is refused, and no file outside the workspace changes.
- **SC-108**: For 100% of runs that reach QA, the run record alone holds, for every
  iteration: QA's findings with their fix instructions, the Coder's answer to each one,
  and the test results. For a failed run it also holds the end reason. No other source
  is needed to explain the run. This is checked by reading the records of one successful
  run and one run stopped by the iteration limit.
- **SC-109**: In 100% of runs that reach QA, every Coder ⇄ QA exchange appears in the run
  record as an A2A exchange. A run with *k* QA reviews has exactly *k* review requests
  from the Coder to QA and at most *k − 1* fix requests from QA to the Coder.
- **SC-110**: Neither agent's code refers to the other agent's code. A check over the
  source confirms that the Coder never imports QA code and that QA never imports Coder
  code.

## Assumptions

- **Scope of this increment**: This feature delivers 001's QA Agent, the Coder ⇄ QA loop,
  disputes and test ownership (001 FR-012–FR-020), plus the MCP file-tool server. These
  stay deferred: the time and spend limits (001 FR-022, apart from the iteration limit),
  third-party packages and the install step (001 FR-011, FR-011a), and saving the outcome
  report as separate files (001 FR-024).
- **Standard library only, apart from the test runner**: Generated code still uses only
  the Python standard library (001 Increment 2 deviation). Generated tests are written for
  pytest, as the user asked during planning. pytest is built into the sandbox image ahead
  of time, so tests still run with no network and no install step during a run.
- **Iteration limit range**: The 1–10 range matches the Coder's existing per-task step
  cap and keeps runs bounded until the time and spend limits arrive.
- **MCP consumers**: "Integrate an MCP server" is read as both of these: the team's agents
  get their file tools through it, and it can be started on its own for outside MCP
  clients. Only `read_file` and `write_file` are offered. Code execution is not offered
  over MCP, because it needs the sandbox and the run's settings.
- **A2A scope**: The agents follow the A2A protocol's data model and operations (version
  1.0) over an in-process message broker, which the protocol allows as a custom
  transport. A2A over a network (HTTP or gRPC), between processes or machines, and with
  authentication is out of scope, because both agents run in one process for one user.
  Streaming updates and push notifications are not supported, and the agent cards say
  so.
- **MCP connection**: The server runs as a local process that its client starts on the
  same machine. Network-exposed or multi-user hosting is out of scope.
- **Test protection boundary**: The ban on the Coder changing tests (FR-111) is enforced
  for the Coder's tool calls in a team run. It covers creating, changing and deleting test
  files, not reading them. A developer using the standalone server has full access to the
  workspace they chose.
- **Dependencies**: The MCP integration uses the official Python MCP SDK, the only new
  runtime package. Its justification under constitution Principle III is recorded in the
  plan's research, together with a check that it fits the existing pinned packages. A
  constitution amendment adds it to the approved list. A2A and the benchmark use only the
  standard library, and pytest is used only inside the sandbox that runs generated
  tests.
- **Benchmark**: This feature delivers 001's benchmark, which SC-102 and SC-103 are
  measured on: a fixed set of 10 small requirements, each a command-line program or
  library with no outside services, and a runner that scores both criteria. The set is
  frozen from its first full evaluation. Replacing it starts a new version whose results
  cannot be compared with earlier ones.
- **Style standard**: "Style problems" are judged against the project constitution's
  Clean Code principle and Python Standards, as in 001. Only their explicit MUST rules
  can block (FR-104a), so 001's SC-005 ("no blocking style violations") means no breach
  of those rules.
- **Existing behavior unchanged**: The PM's planning, the 8-task limit, the
  build-all-tasks-first order, and the "stop at the first failed task" rule (001 FR-009a)
  all stay the same.
