# Feature Specification: Autonomous Multi-Agent Development Team

**Feature Branch**: `dev` (spec directory: `001-autonomous-dev-team`)

**Created**: 2026-10-15

**Status**: Draft

**Input**: User description: "The goal is to build a fully autonomous, multi-agent software development team powered by large language models. The system receives a high-level software requirement - in plain English - and produces tested, working code with no human intervention beyond the initial prompt. There will be three agents: 1. The Product Manager (PM) Agent - Interprets the user's requirement, decomposes it into discrete engineering tasks, and produces a formal technical specification. 2. Coder Agent - Receives tasks from the PM, writes clean and modular Python code, incorporates feedback from the QA Agent, and iterates until all tests pass. 3. QA & Debugger Agent - Reviews the code produced by the Coder Agent, writes automated tests, identifies bugs and style violations, and coordinates fixes."

## Clarifications

### Session 2026-10-15

- Q: What kind of software must a single run be able to build? → A: Anything, including
  web applications, databases, and integrations with outside services.
- Q: How strictly should generated code be kept away from your machine when the system
  runs it during testing? → A: A throwaway container for each run, with memory and CPU
  limits, that sees only the run's own folder and has full network access. (The network
  part is superseded by the 2026-10-17 session.)
- Q: When generated code needs a third-party Python package, how freely may the Coder
  Agent use one? → A: Any package, but the standard library is preferred and each package
  needs a recorded one-line reason.
- Q: Should your secrets, such as the language-model API key and other environment
  credentials, ever be visible inside the container where generated code runs? → A: Never;
  the container gets no credentials or host environment variables.
- Q: Should the Coder and QA agents work through the PM's tasks one at a time, or build
  everything first and then review it all together? → A: The Coder implements all tasks
  first, then QA tests and reviews the whole program; each fix round is one iteration.

### Session 2026-10-17

- Q: Should generated code have network access while it runs? → A: No. The containers
  that run generated code have no network access, so the code can neither download
  anything nor send data out.
- Q: With no network in the run containers, how should third-party packages be
  installed? → A: In a separate install step before the code runs: the Coder lists the
  packages it needs with a reason for each, and the system installs them into the run's
  workspace in its own throwaway container. That container has network access but no
  secrets, sees only the run's workspace, and installs prebuilt packages only, so no
  package or generated code runs during installation (Option C).
- Q: Should the 8-task limit apply to every run, and can the user change it? → A: It
  applies to every run and is fixed at 8. When the PM plans more than 8 tasks, it merges
  them into 8 or fewer, keeping every acceptance criterion, before any task reaches the
  Coder. The user cannot change the limit.
- Q: What should a task's priority (high, medium or low) control? → A: It is information
  only. The PM orders tasks by dependency first, then priority. Tasks always run in that
  order, and each task's priority is shown to the Coder and kept in the run record.
- Q: When the Coder can't finish a task (hits its step limit or the model declines), what
  should happen to the remaining tasks? → A: The run stops. That task is marked failed,
  the remaining tasks are marked skipped, the run ends with a failure result, and the code
  written so far is kept.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Requirement In, Tested Code Out (Priority: P1)

A user writes a short plain-English description of the software they want (for example,
"a command-line tool that converts temperatures between Celsius and Fahrenheit") and starts
a run. With no further input, the PM Agent turns the requirement into a technical
specification and a list of engineering tasks, the Coder Agent writes the Python code, the
QA & Debugger Agent writes and runs automated tests, and the system delivers the code, the
tests, and the specification once every test passes.

**Why this priority**: This is the product's core promise: one prompt in, working and
tested code out. Nothing else has value without it.

**Independent Test**: Submit a small, well-defined requirement and confirm that the run
finishes without asking the user anything, delivers a specification, a task list, source
code, and a test suite, and that the delivered tests pass when run outside the system.

**Acceptance Scenarios**:

1. **Given** a clear, small requirement, **When** the user submits it, **Then** the system
   delivers a technical specification, a task list, source code, and an automated test
   suite without asking the user any questions.
2. **Given** a successful run, **When** the user runs the delivered tests on their own,
   **Then** every test passes.
3. **Given** a successful run, **When** the user runs the delivered program as the
   specification describes, **Then** it does what the original requirement asked for.

---

### User Story 2 - Automatic Review and Fix Loop (Priority: P2)

When the Coder Agent's code has defects (failing tests, bugs found in review, or style
violations), the QA & Debugger Agent sends specific findings back to the Coder Agent, which
revises the code. Review, testing, and revision repeat automatically until all tests pass
and no blocking findings remain, or until a run limit is reached.

**Why this priority**: First drafts of generated code are often wrong. Catching and fixing
defects automatically is what turns plausible code into working code, and what makes the
team autonomous rather than a one-shot code generator.

**Independent Test**: Give the Coder and QA agents a specification whose first
implementation is likely to miss an edge case (such as empty input or division by zero).
Confirm that QA detects the defect, the Coder fixes it, and a passing test covers it in a
later iteration.

**Acceptance Scenarios**:

1. **Given** code that fails one or more tests, **When** QA reviews it, **Then** QA reports
   each failing behavior to the Coder Agent as a finding.
2. **Given** QA findings, **When** the Coder Agent revises the code, **Then** QA reviews the
   new version and runs the full test suite again.
3. **Given** code that passes its tests but breaks the project's style standards, **When**
   QA reviews it, **Then** the violations are reported and fixed before delivery.
4. **Given** a run limit is reached while tests still fail, **When** the run ends, **Then**
   the system reports failure and delivers the latest code with the unresolved findings.

---

### User Story 3 - Run Record and Outcome Report (Priority: P3)

After any run, the user can see what happened: the specification and task list the PM
produced, the assumptions it made, each iteration's QA findings and fixes, the final test
results, and a plain statement of whether the run succeeded and, if not, why.

**Why this priority**: Users must be able to check and trust autonomous output. A clear
record lets them review the PM's interpretation, audit what changed between iterations,
and diagnose failed runs.

**Independent Test**: Finish any run, successful or not, and confirm that every item above
is available and that the outcome report states the result, the number of iterations, and
the final test pass and fail counts.

**Acceptance Scenarios**:

1. **Given** a finished run, **When** the user opens its record, **Then** it contains the
   specification, the task list, each iteration's findings, and the final test results.
2. **Given** an ambiguous requirement, **When** the PM interprets it, **Then** every
   assumption the PM made is listed in the specification.
3. **Given** a failed run, **When** the user reads the outcome report, **Then** it states
   why the run failed and which findings are unresolved.

---

### Edge Cases

- **Ambiguous requirement**: The PM makes reasonable assumptions, records them in the
  specification, and continues. It never stops to ask the user.
- **Empty, nonsensical, or non-software requirement**: The system rejects it before any
  code is written and explains why.
- **Large requirement** (for example, a web application with a database and outside
  services): The system does not narrow or reject a requirement because of its size or
  type; its tasks are larger instead (see the next case).
- **PM plans more than 8 tasks**: The PM merges them into 8 or fewer before any task
  reaches the Coder, keeping every acceptance criterion and the task order. If its merge
  still has more than 8 tasks, leaves a task incomplete, or drops, rewords or reorders a
  criterion, the system merges neighbouring original tasks itself, so the limit always
  holds and no criterion is lost (FR-006a).
- **Requirement depends on an outside service** (for example, a payment or weather
  service): Code is written against that service, but tests use stand-ins (fakes or
  recorded responses), so a run never needs real credentials or live access to it.
- **Coder cannot finish a task** (it reaches its step limit, the model declines, or the
  model service fails): The task is marked failed, the remaining tasks are marked skipped
  and never start, and the run ends with a failure result that names the failed task. The
  code written so far is kept (FR-009a).
- **Code never converges**: The run stops at the first limit reached (iterations, time, or
  spend) and reports failure instead of running forever.
- **Coder and QA go in circles** (a fix for one finding brings back an earlier one): The
  run limits still end the run, and the record shows the repeated findings.
- **Generated code hangs or runs too long during testing**: Each test run has a time limit.
  A timeout counts as a test failure and is reported as a finding.
- **Generated code tries something harmful** (deleting files, using up memory, sending
  data out): It runs only inside the run's throwaway, resource-limited containers
  (FR-021), so it cannot affect the user's files, and it has no network access, so it
  cannot reach the internet. A resource-limit breach is reported as a test failure.
- **A test is wrong** (it expects behavior the specification does not call for): The Coder
  Agent disputes the test. QA checks the dispute against the specification and either
  keeps the test or corrects it. The dispute and decision are recorded.
- **A needed third-party package cannot be installed** (it does not exist, has no prebuilt
  version for the sandbox's Python, or conflicts with another package): The failure is
  reported to the Coder Agent as a finding so it can choose another package or approach;
  repeated failures count toward the iteration limit.
- **A package is malicious or impersonates a real one**: It can only run inside the
  run's containers, which have no network and no secrets (FR-021, FR-021a), so it cannot
  send anything out. Every package and its reason appear in the run record and the
  delivered dependency file, so the user can review them before installing the code
  elsewhere.
- **The language-model service is unavailable or returns errors**: The run retries a
  limited number of times, then stops with a clear error and keeps everything produced so
  far.

## Requirements *(mandatory)*

### Functional Requirements

**Input and coordination**

- **FR-001**: System MUST accept one software requirement written in plain English as its
  only required human input.
- **FR-002**: System MUST complete a run, from requirement to delivered result, without
  asking for any further human input.
- **FR-003**: System MUST coordinate three agents with separate responsibilities (Product
  Manager, Coder, and QA & Debugger) and pass work between them in the order
  PM → Coder ⇄ QA.
- **FR-004**: System MUST reject empty or clearly non-software requirements, with an
  explanation, before any development starts.

**Product Manager Agent**

- **FR-005**: The PM Agent MUST produce a technical specification from the requirement that
  describes the intended behavior, inputs, outputs, and acceptance criteria.
- **FR-006**: The PM Agent MUST break the requirement into an ordered list of discrete
  engineering tasks, each small enough to implement and check on its own, and each with
  its own acceptance criteria that state how to check it.
- **FR-006a**: A requirement MUST be broken into at most 8 tasks. This limit is fixed and
  the user cannot change it. When the PM plans more than 8 tasks, it MUST merge them into
  8 or fewer, keeping every acceptance criterion and the task order, before any task is
  handed to the Coder Agent. The system MUST check the PM's merged tasks: there are 8 or
  fewer, each is complete, and every original acceptance criterion appears word for word
  and in its original order (differences in letter case and spacing do not count). If any
  check fails, the system MUST merge neighbouring original tasks itself instead. Where
  FR-006 and this limit conflict, the limit wins, and merged tasks may be larger.
- **FR-006b**: Each task MUST have a priority of high, medium or low. The PM MUST order
  tasks by dependency first, then priority. Priority MUST NOT change the order in which
  tasks run; it is passed to the Coder Agent with the task and kept in the run record.
- **FR-007**: The PM Agent MUST resolve ambiguity by making explicit assumptions and
  recording them in the specification, never by asking the user.
- **FR-008**: The system MUST accept requirements of any size or type that describe
  buildable software, including web applications, database-backed systems, and
  integrations with outside services, and MUST NOT narrow or reject a requirement on size
  or type alone.
- **FR-008a**: When a requirement depends on outside services or infrastructure, the PM
  Agent MUST list each dependency in the specification, and the QA Agent MUST test the
  dependent behavior with stand-ins so tests run without real credentials or live access
  to those services.

**Coder Agent**

- **FR-009**: The Coder Agent MUST implement all of the PM's tasks, in order, as Python
  source code before handing the program to QA for its first review.
- **FR-009a**: If the Coder Agent cannot finish a task, the system MUST mark that task
  failed, mark every remaining task skipped without starting it, end the run with a
  failure result, and keep the code written so far. The program does not go to QA.
- **FR-010**: Generated code MUST follow the project constitution's Clean Code and Simple
  Design principles and its Python Standards.
- **FR-011**: The Coder Agent MAY use any third-party package, but MUST prefer the Python
  standard library and MUST record a one-line reason for each package in the run record.
  Every package used MUST be listed in a dependency file delivered with the code, so the
  user can install and run the code outside the system.
- **FR-011a**: Packages MUST be installed in a separate install step, before the code
  that needs them runs, into the run's own working folder. The install step MUST run in
  its own throwaway container that has network access, receives no credentials or host
  environment variables, sees only the run's working folder, and installs prebuilt
  packages only, so that neither generated code nor package code runs during
  installation. A failed installation MUST be reported to the Coder Agent as a finding.
- **FR-012**: The Coder Agent MUST respond to every QA finding, either by fixing it or by
  disputing it with a reason based on the specification.
- **FR-013**: The Coder Agent MUST NOT create, change, or delete tests. Only the QA Agent
  may change the test suite.
- **FR-014**: The Coder Agent MUST keep revising the code until all tests pass and no
  blocking findings remain, or until a run limit is reached.

**QA & Debugger Agent**

- **FR-015**: The QA Agent MUST write an automated test suite based on the specification's
  acceptance criteria, covering normal behavior and the edge cases the specification names.
- **FR-016**: The QA Agent MUST run the full test suite against every version of the code
  and record a pass or fail result for each test.
- **FR-017**: The QA Agent MUST review the code for bugs and style violations and mark each
  finding as blocking or advisory.
- **FR-018**: Each QA finding MUST state the problem, where it is in the code, and the
  expected behavior.
- **FR-019**: When the Coder Agent disputes a test or finding, the QA Agent MUST decide it
  by checking the specification, either keeping or correcting the test, and MUST record
  the dispute and the decision.
- **FR-020**: The QA Agent MUST declare the code complete only when all tests pass and no
  blocking findings remain.

**Safety, limits, and delivery**

- **FR-021**: System MUST run generated code and tests only inside throwaway containers
  that belong to the run and are discarded afterwards (one container per execution
  satisfies this). Each container MUST have memory and CPU limits, MUST see only the
  run's own working folder, and MUST have no network access. Each test run MUST have a
  time limit (default: 60 seconds). The package install step (FR-011a) is the only
  container with network access, and it runs no generated code.
- **FR-021a**: No container (for running code or installing packages) may receive any
  credentials or host environment variables. Agents MUST call the language-model service from outside the container, and
  generated code that needs a secret MUST be given a stand-in value in tests.
- **FR-022**: System MUST end a run with a failure result as soon as any limit is reached:
  number of Coder ⇄ QA iterations (default: 5), total elapsed time (default: 30 minutes),
  or total language-model spend (default: US$2). The user MUST be able to change each
  limit when starting a run.
- **FR-023**: System MUST retry temporary language-model service failures a limited number
  of times (default: 3) before stopping the run with a clear error.
- **FR-024**: System MUST save, for every run, the final source code, the test suite, the
  dependency file (if any), the specification, the task list, and an outcome report as
  files the user can open after the run.
- **FR-025**: The outcome report MUST state success or failure, the limit or failed task
  that ended the run (if any), each task's status, the number of iterations, elapsed time, language-model spend, final test
  pass and fail counts, the PM's assumptions, and any unresolved findings.
- **FR-026**: System MUST keep a record of every iteration: the code version, its test
  results, the QA findings, any disputes with their decisions, and the reason for each
  third-party package added.

### Key Entities

- **Requirement**: The user's plain-English description of the software they want; the
  only human input to a run.
- **Technical Specification**: The PM's formal reading of the requirement: intended
  behavior, inputs and outputs, acceptance criteria, assumptions, and what is out of scope.
- **Engineering Task**: One ordered unit of work from the specification, with an ID, a
  description, its own acceptance criteria, a priority (high, medium or low),
  and a status (pending, in progress, completed, failed, or skipped).
- **Code Version**: The Python source the Coder Agent produced in one iteration.
- **Test Suite**: The automated tests QA wrote, each traceable to an acceptance criterion.
- **QA Finding**: One reported problem (failing test, bug, style violation, or failed
  package installation) with its location, expected behavior, and severity (blocking or
  advisory).
- **Dispute**: The Coder Agent's objection to a test or finding, its reason, and QA's
  recorded decision.
- **Iteration**: One Coder ⇄ QA cycle over the whole program (the first draft of all
  tasks, or one round of fixes), linking a code version, its test results, and its
  findings. The iteration limit counts these cycles.
- **Run**: One full pass from requirement to outcome, containing everything above.
- **Outcome Report**: The final summary of a run, with the contents listed in FR-025.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On the benchmark of 10 small requirements (see Assumptions), at least 8 runs
  deliver code whose full test suite passes, with no human input after the requirement.
- **SC-002**: On the same benchmark, delivered code passes at least 80% of a separate set
  of acceptance tests written by a person from the original requirements. This guards
  against generated tests that only confirm whatever the code happens to do.
- **SC-003**: Every run ends with an explicit success or failure result; no run hangs or
  continues past its iteration, time, or spend limit.
- **SC-004**: At least 8 of the 10 benchmark requirements go from submission to delivered
  code in under 10 minutes each.
- **SC-005**: Code delivered by successful runs has no blocking style violations under the
  project's style standards.
- **SC-006**: Of the runs whose first code version fails at least one test, at least 70%
  reach a fully passing state within the run limits.
- **SC-007**: A reviewer can tell from the run record alone why a failed run failed in
  under 5 minutes.

## Assumptions

- **Output language**: All generated code and tests are Python, as the user requested and
  the project constitution requires.
- **Interaction model**: One user starts a run from the command line. Multi-user access,
  sign-in, and a graphical interface are out of scope.
- **Supported scope**: Any buildable software is in scope, from single functions and
  command-line tools up to web applications, database-backed systems, and integrations
  with outside services. Larger requirements may need higher run limits than the defaults,
  and their tasks are larger, because the task count is fixed at no more than 8 (FR-006a).
- **Deployment**: The system delivers tested code; deploying or hosting that code is out
  of scope.
- **Default limits**: The defaults in FR-021, FR-022, and FR-023 are starting values that
  planning may tune.
- **Benchmark**: A "small requirement" is one a single command-line program or library of
  up to about 300 lines can meet, with no outside services. The benchmark is a fixed list
  of 10 such requirements, written before the full team's first evaluation and kept
  unchanged between evaluations so results can be compared.
- **Spend tracking**: Language-model cost can be measured per run.
- **Isolation**: A container runtime is available on the machine running the system, so
  each run can create a throwaway container for generated code.
- **Network**: The machine running the system has internet access for the language-model
  service, to fetch the sandbox image, and for the package install step to reach the
  public package index (FR-011a). Generated code itself never has network access
  (FR-021).
- **Prebuilt packages**: Requiring prebuilt packages (FR-011a) means a package that is
  published only as source cannot be used; the Coder Agent must pick an alternative.
- **Language-model access**: A hosted large-language-model service is available through
  credentials supplied by environment configuration, as in the existing lab setup.
- **Test ownership**: See FR-013; the point is that the Coder Agent cannot make tests pass
  by weakening them.
- **Style standards**: "Style violations" are judged against the project constitution's
  Clean Code principle and Python Standards section.
- **Existing work**: The Lab 1.1 files (`agent.py`, `memory.py`, `test_memory.py`) are
  retired; the agents' own memory replaces them.
