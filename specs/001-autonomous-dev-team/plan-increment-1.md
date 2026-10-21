# Implementation Plan: Autonomous Multi-Agent Development Team — Increment 1: Coder Agent

**Branch**: `dev` (spec directory: `001-autonomous-dev-team`) | **Date**: 2026-10-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-autonomous-dev-team/spec.md`, scoped by the
user to a first increment: a single, fully functional Coder Agent.

> Archived on 2026-10-17 when [plan.md](plan.md) moved on to Increment 2. Text unchanged.

## Summary

Build the Coder Agent (`agents/coder_agent.py`) as a standalone, working component of the
future PM → Coder ⇄ QA team. It accepts a natural-language coding task, plans, writes files
and runs Python through custom tools inside a ReAct loop (Thought → Action → Observation,
hard cap of 10 steps), and returns a structured result: generated code, explanation,
execution plan, and the actual execution result.

Technical approach (details in [research.md](research.md)): a LangGraph `StateGraph` with
four nodes (`recall → think → act`, then `finalize`) drives Claude Sonnet 5.5 through the
official Anthropic SDK. Two-tier memory: a 5-turn sliding window plus a persistent Chroma
store using Chroma's built-in embeddings, queried before every model call and injected
append-only. Tools: `read_file`, `write_file` (confined to a per-run workspace) and
`exec_python` (a throwaway, network-less sandbox container per call, with a configurable
timeout and no secrets). The
README is rewritten to describe the full project vision, setup, and design decisions.

### Spec coverage in this increment

| Covered now | Deferred to later increments |
|-------------|------------------------------|
| FR-010 (constitution style), FR-021 (a throwaway, network-less sandbox container per code run), FR-021a (no secrets where generated code runs), FR-023 (SDK retries ×3), FR-024/FR-026 *for the Coder's own run* (files on disk, step trace), FR-025 *partially* (result, elapsed time, token usage; no dollar spend or PM assumptions yet) | PM Agent (FR-004–FR-008a), all-tasks-then-QA flow (FR-009), packages, install step and dependency file (FR-011, FR-011a), Coder ⇄ QA loop, test ownership and disputes (FR-012–FR-020), run limits on iterations/time/spend (FR-022) |

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: `anthropic` 1.x (Claude Sonnet 5.5, model `claude-sonnet-5-5`),
`langgraph` 1.x (orchestration), `chromadb` (semantic memory, built-in embeddings),
`python-dotenv` (local config). Exact pins set at implementation time. `openai` is removed
together with the Lab 1.1 files that import it (constitution v1.1.0).

**Storage**: Chroma `PersistentClient` at `.chroma/`; generated files under
`workspace/<run_id>/` (both already git-ignored)

**Testing**: stdlib `unittest` (`python -m unittest discover -s tests -t .`); offline unit
tests plus one live smoke test skipped without credentials

**Target Platform**: Linux host with Docker (the agent runs on the host; generated code runs
in sandbox containers)

**Project Type**: Python library + CLI (`python -m agents.coder_agent "<task>"`)

**Performance Goals**: A short single-file task completes in under 2 minutes; a multi-step
task within the 10-step cap

**Constraints**: ≤ 10 ReAct steps (model calls); `exec_python` timeout configurable (default 30 s);
history append-only (Sonnet 5.5 preserved thinking); no secrets in the code-execution
environment; file I/O confined to the run workspace

**Scale/Scope**: One user, one task at a time; tasks of up to a few files

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Pre-research | Post-design | Notes |
|------|:---:|:---:|-------|
| I. Clean Code — intent-revealing names, single-purpose functions ≤ ~40 lines, type hints + docstrings on public API, explicit error handling | ✅ | ✅ | Design splits work into small node functions and tool functions; tool errors return `is_error` results instead of bare `except` |
| II. Simple Design — no speculative abstractions, plain functions, one responsibility per module | ✅ | ✅ | One class with state (`CoderAgent`) plus dataclasses; no base classes, registries, or plugin systems; PM/QA hooks not pre-built |
| III. Minimal Dependencies — stdlib first; justify every new package; no overlapping packages; no architecture-imposing frameworks except the approved `langgraph` core graph API | ✅ | ✅ | Constitution v1.1.0 approves `langgraph` (core `StateGraph`/`START`/`END` only) and removes `openai`, so no LLM clients overlap. `anthropic` justified in research R1; Chroma's built-in embeddings avoid an extra package (R6); raw JSON schema avoids a direct `pydantic` import (R7) |
| Python Standards — PEP 8/257, grouped imports, secrets from env, `__main__` guard, stdlib tests | ✅ | ✅ | `unittest` only; `.env` via `python-dotenv`; CLI under `if __name__ == "__main__":` |
| Workflow — tests for new behavior; small focused changes | ✅ | ✅ | Every tool, memory tier, and the loop cap has a test |

**Result**: PASS (constitution v1.1.0); no violations to track.

## Project Structure

### Documentation (this feature)

```text
specs/001-autonomous-dev-team/
├── spec.md
├── plan.md              # This file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── coder-agent-api.md
│   └── tools.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
agents/
├── __init__.py
├── coder_agent.py       # CoderConfig, CoderResult, CoderAgent, LangGraph graph, CLI
├── memory.py            # SlidingWindowBuffer, SemanticMemory (Chroma)
├── pm_agent.py          # existing empty placeholder — untouched
└── qa_agent.py          # existing empty placeholder — untouched

tools/
├── __init__.py          # TOOL_DEFINITIONS, dispatch helper
├── file_tools.py        # read_file, write_file, workspace path confinement
└── python_exec.py       # exec_python, ExecutionResult

tests/
├── __init__.py
├── test_tools.py
├── test_agent_memory.py
├── test_coder_agent_loop.py    # offline: fake client, 10-step cap
└── test_coder_agent_smoke.py   # live: skipped without credentials

README.md                # rewritten: vision, setup, design decisions
requirements.txt         # + anthropic, langgraph, pysqlite3-binary (pinned); − openai

# Removed: agent.py, memory.py, test_memory.py (Lab 1.1 skeletons; the only openai users)
# Unchanged: orchestration/, docs/
```

**Structure Decision**: Single Python project using the folders already in the repository
(`agents/`, `tools/`, `tests/`), matching the constitution's module responsibilities:
agent behavior in `agents/`, tool functions in `tools/`. Memory lives in `agents/memory.py`
(shared agent infrastructure for the future PM and QA agents); the Lab 1.1 root `memory.py`
is retired. `orchestration/` stays empty until the multi-agent increment.

## Spec Deviations (this increment)

| Spec item | This increment | Why | Resolution |
|-----------|----------------|-----|------------|
| FR-022: iteration limit default 5 | ReAct cap of 10 *steps* | Different loop: a step is one Coder model call within one task; the spec's 5 counts Coder ⇄ QA iterations | Named `max_steps`/`steps_used`/`step_limit` so the two never share a name; both limits coexist once QA exists |
| FR-011/FR-011a: any package, with a recorded reason and a dependency file, installed in a separate network-enabled, secret-free, wheels-only install container | Standard library only | The install step is not built in this increment, and the run sandbox has no network (FR-021) | Deferred, design decided: see research R11. Lands with QA or as its own increment |

## Complexity Tracking

No violations. The two items previously tracked here are resolved by constitution v1.1.0:
`langgraph` is an approved exception to Principle III (core graph API only), and `openai`
is removed with the Lab 1.1 files, so no LLM clients overlap.
