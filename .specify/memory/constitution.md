<!--
Sync Impact Report
==================
Version change: 1.4.0 → 1.4.1
Bump rationale: PATCH. The Principle III baseline list now names `pysqlite3-binary`, a
runtime dependency since 001 (declared in requirements.txt with a Linux marker and
justified in specs/001-autonomous-dev-team/plan.md). `agents/memory.py` swaps it in for
the standard `sqlite3` module on hosts whose SQLite is too old for `chromadb`. No rule
changes meaning: the list was incomplete (/speckit-analyze finding D2).

Modified principles:
  - III. Minimal Dependencies: baseline list includes `pysqlite3-binary` and its limited
    use

Templates reviewed (read at runtime, not modified by this command): plan, spec and tasks
templates; no change needed.

Follow-up TODOs: None

Previous amendment (1.3.0 → 1.4.0): MINOR. Principle III approves a new test-only import: `httpx2`, the HTTP
transport the approved `anthropic` SDK depends on, may be imported by project tests only to
build the request and response objects that real `anthropic` exception classes require. It
is not declared separately (it is pinned through `anthropic`), and project code MUST NOT
import it. No principle is removed or redefined.

Modified principles:
  - III. Minimal Dependencies: new rule for test-only imports of transitive dependencies,
    naming `httpx2` as the one approved case

Modified sections:
  - Python Standards: the testing rule now names the `httpx2` exception

Justification (from /speckit-analyze finding D1, feature 003):
  - The existing tests (test_pm_agent, test_qa_agent, test_coder_agent_loop,
    test_team_handoff) and the planned `tests/fakes/scripted_claude.py` (003 T012) raise
    real `anthropic` errors such as `RateLimitError` and `APIStatusError`, whose constructors
    need an `httpx2.Request` and an `httpx2.Response`. Faking those errors with look-alike
    classes would skip the SDK's own types, which the failure classifier (003 R4) depends on.
  - `httpx2` is already installed as a dependency of the pinned `anthropic`, so no package
    is added and its version moves only when `anthropic` is bumped.

Templates reviewed (read at runtime, not modified by this command):
  - .specify/templates/plan-template.md — Constitution Check gates derive from Principles I–III
  - .specify/templates/spec-template.md — no constitution-specific fields required
  - .specify/templates/tasks-template.md — no constitution-specific fields required

Follow-up TODOs: None

Previous amendment (1.2.0 → 1.3.0): MINOR. Principle III's approved dependency baseline is expanded for
feature 003 (production hardening): `langsmith` is approved as a runtime dependency with
a limited API surface for distributed tracing, and `coverage` is approved as a
development-only tool that runs the test suite and enforces the coverage gate. No
principle is removed or redefined.

Modified principles:
  - III. Minimal Dependencies: baseline now includes `langsmith` (trace,
    tracing_context, RunTree header propagation and Client with hide_inputs /
    hide_outputs / hide_metadata only); `coverage` approved as a development-only
    dependency declared in requirements-dev.txt and never imported by project code or
    tests; development-only dependencies get their own rule

Modified sections:
  - Python Standards: the testing rule names `coverage` as the approved test runner
    wrapper

Justification (from specs/003-production-hardening/plan.md, Complexity Tracking):
  - langsmith: the SDK for the LangSmith tracing service the user requires. It is
    already installed transitively by the approved `langgraph` (through
    `langchain-core`), so declaring it adds no package. MIT licensed, actively
    maintained by LangChain Inc. Raw HTTP would re-implement batching, run-tree ordering
    and header propagation; `wrap_anthropic` would bypass the project's retry, cache and
    redaction layer.
  - coverage: the standard library has no statement-coverage tool with a threshold, and
    FR-237 requires an 85% gate. Apache 2.0, actively maintained. It only runs the
    unittest suite.

Previous amendment (1.1.1 → 1.2.0): `mcp` approved with a limited API surface; `pytest`
allowed only inside the sandbox image.

Added sections:
  - None

Removed sections:
  - None

Templates reviewed (read at runtime, not modified by this command):
  - .specify/templates/plan-template.md — Constitution Check gates derive from Principles I–III
  - .specify/templates/spec-template.md — no constitution-specific fields required
  - .specify/templates/tasks-template.md — no constitution-specific fields required

Previous amendment (1.1.0 → 1.1.1): the Python version rule now names the sandbox
image instead of the removed Dockerfile.

Follow-up TODOs: None
-->

# Multi-Agent Lab Constitution

## Core Principles

### I. Clean Code

Code is written to be read first and executed second.

- Names (modules, functions, classes, variables) MUST state intent; abbreviations are
  permitted only when they are standard in the domain (e.g., `llm`, `id`, `url`).
- Each function MUST do one thing. A function exceeding ~40 lines or 3 levels of nesting
  MUST be split or carry a written justification in review.
- Public functions and classes MUST have type hints and a docstring stating purpose,
  inputs, outputs, and raised exceptions where non-obvious.
- Comments MUST explain *why*, not *what*; commented-out code MUST NOT be committed.
- Errors MUST be handled explicitly: no bare `except:`, no silently swallowed
  exceptions, and failures MUST surface a message that identifies the cause.
- Duplicated logic appearing in three or more places MUST be extracted into a shared
  function.

**Rationale**: Agent systems are hard to debug; readable, explicit code keeps the cost of
understanding and changing behavior low.

### II. Simple Design

Build the simplest thing that satisfies the current, stated requirement.

- Features MUST NOT be implemented for speculative future needs (YAGNI).
- Prefer plain functions and modules; classes MUST be introduced only when they hold
  state or represent a real domain concept. Inheritance hierarchies deeper than one level
  MUST be justified.
- Abstractions (base classes, registries, plugin systems, factories) MUST NOT be added
  until at least two concrete use cases exist.
- Data MUST flow through explicit function arguments and return values; hidden global
  mutable state is prohibited except for clearly named module-level configuration.
- Each module MUST have a single, describable responsibility (e.g., `agents/memory.py`
  owns memory, `agents/` owns agent behavior, `tools/` owns tool functions).
- When two designs meet the requirement, the one with fewer moving parts MUST be chosen.

**Rationale**: Simple designs are easier to test, reason about, and change as the lab
evolves; unneeded indirection is the most common source of accidental complexity.

### III. Minimal Dependencies

Every third-party package is a liability that must earn its place.

- The Python standard library MUST be used whenever it reasonably solves the problem.
- A new third-party dependency MUST be justified in the feature plan by stating: the
  problem it solves, why the standard library or an existing dependency is insufficient,
  and its maintenance/licensing status.
- All runtime dependencies MUST be declared in `requirements.txt`; undeclared imports are
  prohibited.
- Overlapping dependencies (two packages serving the same purpose) MUST NOT coexist.
- Frameworks that impose their own architecture (e.g., full agent-orchestration
  frameworks) MUST NOT be adopted where a small amount of direct code against an existing
  client suffices. The one approved exception is `langgraph`, the shared graph substrate
  for the PM → Coder ⇄ QA agents. Only its core graph API (`StateGraph`, `START`, `END`)
  may be used; prebuilt agents, LangChain model wrappers, and checkpointers MUST NOT be
  used without a further amendment.
- The current baseline — `anthropic`, `langgraph`, `python-dotenv`, `chromadb`, `mcp`,
  `langsmith`, `pysqlite3-binary` — is approved. `pysqlite3-binary` is declared for Linux
  only and may be used only to replace the standard `sqlite3` module before `chromadb` is
  imported, on hosts whose SQLite is too old. `mcp` (the official Model Context Protocol Python SDK) may
  be used only through its low-level `Server`, `stdio_server`, `ClientSession`,
  `stdio_client` and `mcp.types`; its high-level `MCPServer` and HTTP transports MUST NOT
  be used without a further amendment.
- `langsmith` is approved for distributed tracing only, through `trace`,
  `tracing_context`, `RunTree` (including `to_headers` for propagation) and `Client`
  (including `hide_inputs`, `hide_outputs` and `hide_metadata` for redaction). Its
  wrappers (such as `wrap_anthropic`, `wrap_openai`), evaluation, dataset and prompt-hub
  APIs MUST NOT be used without a further amendment, and project code MUST NOT import
  `langchain-core` or any other LangChain package directly. Tracing MUST stay off unless
  the operator supplies LangSmith credentials.
- Development-only dependencies are declared in `requirements-dev.txt`, pinned exactly,
  and MUST NOT be imported by project code. `coverage` is approved as a
  development-only dependency to run the test suite and enforce the coverage threshold;
  tests MUST NOT import it.
- Project tests MAY import a transitive dependency of an approved package only when this
  constitution names it. It is not declared separately: its version comes from the
  approved package that pins it. `httpx2`, the HTTP transport of `anthropic`, is approved
  for tests only, to build the `Request` and `Response` objects that real `anthropic`
  exception classes require. Project code MUST NOT import `httpx2`.
- `pytest` is approved only inside the sandbox image that runs generated tests; project
  code MUST NOT import it, and project tests stay on the standard library. Agent-to-agent
  (A2A) messaging is implemented with the standard library; no A2A SDK is approved. `openai` is not approved; it MUST be removed together with the Lab 1.1 files
  that import it.

**Rationale**: Fewer dependencies mean a smaller attack surface, faster installs, fewer
version conflicts, and code whose behavior is visible in this repository.

## Python Standards

- All source code MUST be written for Python 3.12 and MUST NOT rely on language features
  newer than that version. The sandbox image that runs generated code MUST use the
  same version.
- Code MUST follow PEP 8 conventions and PEP 257 docstring conventions.
- Imports MUST be grouped as standard library, third-party, then local, and MUST NOT use
  wildcard (`from x import *`) imports.
- Secrets and environment-specific values (API keys, base URLs) MUST be read from
  environment variables (via `python-dotenv` locally) and MUST NOT be hard-coded or
  committed.
- Entry-point scripts MUST guard execution with `if __name__ == "__main__":`.
- Tests MUST be runnable with the standard library (`unittest` or plain `python` test
  scripts) unless a test dependency is approved under Principle III (currently only the
  test-only `httpx2` import, used to build real `anthropic` errors). The suite MAY be run
  under the approved `coverage` tool to measure and gate statement coverage; it MUST
  still pass when run with plain `unittest`.

## Development Workflow

- Every feature MUST pass a Constitution Check during `/speckit-plan` confirming
  compliance with Principles I–III; any violation MUST be recorded with justification in
  the plan's Complexity Tracking section.
- New or changed behavior MUST be accompanied by automated tests that exercise it; the
  full test suite MUST pass before a change is merged.
- Changes MUST be small and focused; a single commit or pull request MUST NOT mix
  unrelated concerns.
- Code review MUST verify naming, function size, error handling, absence of speculative
  abstractions, and justification for any new dependency.

## Governance

This constitution supersedes all other project practices and conventions. Where guidance
conflicts, this document wins.

- **Amendments**: Proposed via a pull request that edits this file, states the reason for
  the change, and updates the Sync Impact Report. Amendments take effect on merge.
- **Versioning**: The constitution follows semantic versioning:
  - MAJOR — a principle is removed or redefined in a backward-incompatible way.
  - MINOR — a principle or section is added or guidance is materially expanded.
  - PATCH — clarifications, wording, or typo fixes with no change in meaning.
- **Compliance**: All plans, reviews, and pull requests MUST verify compliance with this
  constitution. Added complexity or dependencies MUST be justified in writing; unjustified
  violations are grounds to reject a change.

**Version**: 1.4.1 | **Ratified**: 2026-10-15 | **Last Amended**: 2026-10-20
