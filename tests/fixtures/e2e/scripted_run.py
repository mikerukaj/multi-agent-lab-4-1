"""Scripted models for the end-to-end run on tests/fixtures/e2e/requirement.txt (FR-235c).

The PM plans two tasks; the Coder writes ``inventory.py`` and ``main.py`` through real
``write_file`` and ``exec_python`` calls; QA writes its tests, reports one blocking
finding in its first review, the Coder answers it in a fix round, and QA's second
review is complete. The program the script writes passes the supplied fixture
(test_inventory_fixture.py), which no model ever sees.
"""

import re
from pathlib import Path

from tests.fakes import scripted_claude as sc
from tests.fakes.team_script import TeamModel, bug_finding, first_user_text

REQUIREMENT = (Path(__file__).parent / "requirement.txt").read_text(encoding="utf-8")
CRITERIA = (
    "add_item, remove_item and report behave as the requirement describes",
    "main.py runs a commands file and exits 1 with an error line on bad input",
)

INVENTORY = '''"""An inventory of item names and whole-number quantities."""


def add_item(inventory: dict[str, int], name: str, quantity: int) -> dict[str, int]:
    """Return a new inventory with ``quantity`` more of ``name``.

    Raises:
        ValueError: if ``quantity`` is not positive.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, not {quantity}")
    updated = dict(inventory)
    updated[name] = updated.get(name, 0) + quantity
    return updated


def remove_item(inventory: dict[str, int], name: str, quantity: int) -> dict[str, int]:
    """Return a new inventory with ``quantity`` less of ``name`` (removed at zero).

    Raises:
        ValueError: if ``name`` is unknown, the stock is too low, or ``quantity`` is
            not positive.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, not {quantity}")
    if name not in inventory:
        raise ValueError(f"unknown item {name!r}")
    if quantity > inventory[name]:
        raise ValueError(f"only {inventory[name]} {name} in stock")
    updated = dict(inventory)
    updated[name] -= quantity
    if updated[name] == 0:
        del updated[name]
    return updated


def report(inventory: dict[str, int]) -> str:
    """One ``name: quantity`` line per item, sorted by name."""
    return "\\n".join(f"{name}: {inventory[name]}" for name in sorted(inventory))
'''

MAIN = '''"""Run a commands file against an inventory that starts empty."""

import sys

from inventory import add_item, remove_item, report


def run(lines: list[str]) -> None:
    """Apply each command in order.

    Raises:
        ValueError: for any bad command or quantity.
    """
    inventory: dict[str, int] = {}
    for line in lines:
        words = line.split()
        if not words:
            continue
        if words == ["report"]:
            print(report(inventory))
        elif len(words) == 3 and words[0] in ("add", "remove"):
            try:
                quantity = int(words[2])
            except ValueError as error:
                raise ValueError(f"quantity must be a whole number: {words[2]!r}") from error
            change = add_item if words[0] == "add" else remove_item
            inventory = change(inventory, words[1], quantity)
        else:
            raise ValueError(f"bad command: {line.strip()!r}")


def main(argv: list[str]) -> int:
    """Run the commands file named in ``argv``; 0 on success, 1 on bad input."""
    if len(argv) != 1:
        print("error: usage: main.py COMMANDS_FILE", file=sys.stderr)
        return 1
    try:
        with open(argv[0], encoding="utf-8") as commands:
            run(commands.read().splitlines())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

QA_TESTS = '''from inventory import add_item, remove_item, report


def test_add_remove_and_report():
    stock = add_item({}, "apple", 3)
    assert remove_item(stock, "apple", 1) == {"apple": 2}
    assert report({"pear": 1, "apple": 2}) == "apple: 2\\npear: 1"
'''


class E2EPM(TeamModel):
    def _spec(self, request):
        tasks = [
            {"title": "Write the inventory library", "priority": "high",
             "description": "inventory.py with add_item, remove_item and report.",
             "acceptance_criteria": [CRITERIA[0]]},
            {"title": "Write the command-line tool", "priority": "high",
             "description": "main.py that runs a commands file.",
             "acceptance_criteria": [CRITERIA[1]]},
        ]
        return sc.text_reply({
            "name": "Inventory tracker", "description": "Tracks stock levels.",
            "acceptance_criteria": list(CRITERIA), "assumptions": [], "tasks": tasks,
            "rejection_reason": "",
        }, model=request["model"])


class E2ECoder(TeamModel):
    """Writes the program through real tool calls, then runs what it wrote."""

    def _loop(self, request):
        turns = sum(1 for message in request["messages"] if message["role"] == "assistant")
        found = re.findall(r"Task T(\d) of 2", first_user_text(request))
        task = int(found[-1]) if found else None
        files = {1: ("inventory.py", INVENTORY), 2: ("main.py", MAIN)}
        if task in files and turns == 0:
            path, content = files[task]
            return sc.tool_use_reply(
                [("write_file", {"path": path, "content": content, "purpose": "Write it."}),
                 ("exec_python", {"path": path, "purpose": "Check it runs."})],
                model=request["model"],
            )
        if task is None and turns == 0:  # A revision or fix: rewrite main.py unchanged.
            return sc.tool_use_reply(
                [("write_file", {"path": "main.py", "content": MAIN, "purpose": "Fix it."})],
                model=request["model"],
            )
        return sc.text_reply("Done.", model=request["model"])


class E2EQA(TeamModel):
    def __init__(self) -> None:
        super().__init__(bug_in_first_review=True)

    def _tests(self, request):
        test_file = {"path": "tests/test_inventory.py", "content": QA_TESTS,
                     "criteria": [CRITERIA[0]]}
        return sc.text_reply({"test_files": [test_file], "notes": ""}, model=request["model"])

    def _review(self, request):
        first = "Findings from the last review" not in first_user_text(request)
        finding = {**bug_finding(), "file": "main.py", "expected": CRITERIA[1],
                   "problem": "Check that blank lines are skipped."}
        return sc.text_reply({"findings": [finding] if first else [], "regression_tests": [],
                              "summary": "ok"}, model=request["model"])


def models() -> tuple[E2EPM, E2ECoder, E2EQA]:
    """Fresh PM, Coder and QA models for one scripted end-to-end run."""
    return E2EPM(), E2ECoder(), E2EQA()
