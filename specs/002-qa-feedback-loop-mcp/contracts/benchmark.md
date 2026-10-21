# Contract: Benchmark Set and Runner (SC-102, SC-103)

This measures SC-102 and SC-103 on "001's benchmark of 10 small requirements".
001's Assumption "Benchmark" defines that benchmark:
- each requirement can be met by one command-line program or library of up to about 300
  lines;
- no outside services;
- the list is fixed before the full team's first evaluation, and kept unchanged
  afterwards so results can be compared.

This feature delivers the list and a runner:
- `benchmarks/requirements.json`
- `benchmarks/run_benchmark.py`

## 1. File format: `benchmarks/requirements.json`

```json
{
  "version": 1,
  "requirements": [
    {"id": "temp-convert", "title": "Temperature converter CLI", "requirement": "…"}
  ]
}
```

| Rule | Check |
|------|-------|
| exactly 10 entries | `ValueError` otherwise |
| `id` unique, lowercase kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`) | `ValueError` otherwise |
| `title` and `requirement` non-empty strings | `ValueError` otherwise |
| `version` integer ≥ 1. Increment it only when deliberately replacing the set, and older results are then no longer comparable | |

## 2. The benchmark set (version 1)

Copy these into the file word for word, in this order. Every one can be built with the
Python standard library, needs no network, and names the edge cases it expects.

| id | title | requirement |
|----|-------|-------------|
| `temp-convert` | Temperature converter CLI | A command-line tool temp.py that converts a temperature like '100C' or '-40F' to the other scale and prints it rounded to one decimal place, for example '212.0F'. Malformed input prints an error to stderr and exits with code 2. |
| `word-count` | Word count CLI | A command-line tool wc.py that prints the number of lines, words and characters in a text file given as an argument, or in standard input when no file is given, as three numbers separated by spaces. A missing file prints an error to stderr and exits with code 1. |
| `roman-numerals` | Roman numerals library | A library roman.py with to_roman(n) and from_roman(s) for whole numbers from 1 to 3999 using standard subtractive notation. Both raise ValueError for out-of-range numbers or invalid numerals, including non-canonical forms such as 'IIII'. |
| `stats` | Descriptive statistics library | A library stats.py with mean, median and mode for lists of numbers; mode returns all values tied for most frequent, sorted ascending. Every function raises ValueError on an empty list. |
| `bank-account` | Bank account library | A library bank.py with a BankAccount class supporting deposit, withdraw, balance and a list of past transactions. Non-positive amounts raise ValueError, and a withdrawal larger than the balance raises InsufficientFunds without changing the balance. |
| `todo-cli` | To-do list CLI | A command-line to-do tool todo.py that stores tasks in a JSON file given by --file, with subcommands add <text>, list, done <id> and remove <id>. IDs are increasing integers that are never reused; an unknown id prints an error to stderr and exits with code 1. |
| `csv-summary` | CSV column summary CLI | A command-line tool csvsum.py that reads a CSV file with a header row and prints, for each column whose values are all numeric, its name, minimum, maximum and mean. Non-numeric columns are skipped; a file with no data rows prints an error to stderr and exits with code 1. |
| `password-strength` | Password strength library | A library pwcheck.py with check(password) returning a score from 0 to 4 and a list of reasons: one point each for a length of at least 12, both a lowercase and an uppercase letter, a digit, and a symbol. A password from a built-in list of ten common passwords always scores 0. |
| `matrix` | Matrix operations library | A library matrix.py with add, multiply and transpose for matrices given as lists of lists of numbers. Mismatched dimensions, empty matrices or ragged rows raise ValueError. |
| `rate-limiter` | Token-bucket rate limiter library | A library ratelimit.py with a TokenBucket(capacity, refill_per_second, clock) class whose allow() returns True and uses up a token when one is available, and returns False otherwise. The clock is a callable returning seconds, so behavior can be tested without sleeping. |

## 3. Runner: `python -m benchmarks.run_benchmark`

```text
python -m benchmarks.run_benchmark [--only ID[,ID...]] [--max-iterations N] [--out DIR]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--only` | all 10 | Run a subset. The summary then reports SC-102 and SC-103 as `"not evaluated (partial run)"` |
| `--max-iterations` | `TEAM_MAX_ITERATIONS` or 5 | Passed to `run_team`. SC-103 is defined at the default limit, so a non-default value is recorded in the results |
| `--out` | `benchmarks/results/` | Where the results file goes |

Behavior:
- **Order and isolation**: requirements run one at a time, in file order. Each run gets
  a new `PMAgent`, `CoderAgent` and `QAAgent` built from the env configs, and a **fresh
  temporary Chroma folder** (`CoderConfig.chroma_path`). The Coder's long-term memory
  must not carry lessons from one benchmark item to the next. Workspaces go under
  `workspace/benchmark-<session>/`.
- **Item failures**: an exception from one item (for example `SandboxUnavailableError`)
  is recorded as that item's `error`, and the run continues. A missing credential or
  Docker is checked once before the first item, and gives exit code 2.
- **Cost warning**: each item is a full live team run with real model spend. Before
  starting, the runner prints `Running N benchmark requirements (live model calls)` to
  stderr.
- **Testability**: `main(argv, run_team=run_team, make_agents=build_agents)` takes an
  injectable `run_team` and agent builder (`make_agents(chroma_path) -> (pm, coder, qa)`),
  so the offline tests need no API key and no Docker. The credential and Docker
  pre-flight runs only with the real builder.

Python API (stdlib only):

```python
def load_benchmark(path: Path = BENCHMARK_FILE) -> dict: ...         # validates §1; ValueError
def fingerprint(path: Path = BENCHMARK_FILE) -> str: ...             # sha256 hex of the file bytes
def record_for(entry: dict, final: TeamState | None, error: str | None, duration: float) -> dict: ...
def score(records: list[dict], partial: bool) -> dict: ...
def build_agents(chroma_path: Path) -> tuple[PMAgent, CoderAgent, QAAgent]: ...
def main(argv: list[str] | None = None, run_team=run_team, make_agents=build_agents) -> int: ...
```

## 4. Per-item record

| Field | Type | Source |
|-------|------|--------|
| `id` | str | entry |
| `run_id` | str \| None | `final["run_id"]` |
| `status`, `end_reason` | str \| None | final state (`"error"` / `None` if the item raised) |
| `iterations` | int | `len(final["iterations"])` |
| `first_draft_failed` | bool \| None | iteration 1's `test_run`: `not loaded` or `failed + errors > 0`. `None` if QA never ran. "First draft" means the code QA first reviews, which is after the Coder's first self-reflection |
| `final_passed`, `final_failed` | int | the last iteration's `test_run` (`final_failed` includes errors) |
| `succeeded` | bool | `status == "completed"` and `final_failed == 0` |
| `duration_seconds` | float | wall clock around `run_team` |
| `input_tokens`, `output_tokens` | int | `final["usage"]` |
| `error` | str \| None | exception text, or `final["error"]` |

## 5. Scoring

| Criterion | Rule | Met when |
|-----------|------|----------|
| SC-102 | `succeeded` count out of 10 | ≥ 8 |
| SC-103 | among records with `first_draft_failed is True`, the share with `succeeded` | ≥ 0.70. If no record failed its first draft: `"n/a"`, which counts as met |
| 001 SC-004 (reported only) | records with `duration_seconds < 600` | ≥ 8 (informational) |

For a partial run (`--only`), SC-102 and SC-103 are `"not evaluated (partial run)"`.

## 6. Results file and exit code

`<out>/<UTC timestamp YYYYmmddTHHMMSSZ>.json`:

```json
{"benchmark_version": 1, "fingerprint": "<sha256>", "started_at": "…Z", "finished_at": "…Z",
 "max_iterations": 5, "partial": false,
 "records": ["… §4 …"],
 "summary": {"sc_102": {"value": "9/10", "met": true},
             "sc_103": {"value": "5/6 = 0.83", "met": true},
             "sc_004_001": {"value": "8/10 under 600 s", "met": true}}}
```

- **Comparability check**: if `<out>` already holds a results file whose `fingerprint`
  differs, the runner prints `warning: benchmark set changed since <file>; results are
  not comparable` to stderr.
- **Committed**: results files are checked into the repository, so evaluations can be
  compared.
- **Summary**: a summary table goes to stderr.

| Exit code | Meaning |
|-----------|---------|
| 0 | full run, SC-102 and SC-103 both met |
| 1 | full run with either criterion not met, or any partial run |
| 2 | usage error, invalid benchmark file, or missing credentials or Docker |
