"""The supplied acceptance fixture for the end-to-end test (FR-235c).

It is copied into a scratch copy of the generated program after the run, and run with
pytest in the sandbox. The agents never see it. Its name matches ``test*.py`` but it
lives under ``tests/fixtures/``, which the project's own discovery never imports (it
needs pytest, which only the sandbox image has).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from inventory import add_item, remove_item, report


def run_cli(tmp_path: Path, *lines: str, args=None) -> subprocess.CompletedProcess:
    commands = tmp_path / "commands.txt"
    commands.write_text("\n".join(lines) + "\n", encoding="utf-8")
    argv = [str(commands)] if args is None else args
    return subprocess.run([sys.executable, "main.py", *argv], capture_output=True, text=True,
                          timeout=30, check=False)


def test_adding_a_new_item_starts_from_zero():
    assert add_item({}, "apple", 3) == {"apple": 3}


def test_adding_an_existing_item_increases_it_and_leaves_the_input_unchanged():
    stock = {"apple": 3}
    assert add_item(stock, "apple", 2) == {"apple": 5}
    assert stock == {"apple": 3}


@pytest.mark.parametrize("quantity", [0, -1])
def test_adding_a_non_positive_quantity_is_refused(quantity):
    with pytest.raises(ValueError):
        add_item({}, "apple", quantity)


def test_removing_decreases_the_stock_and_leaves_the_input_unchanged():
    stock = {"apple": 5}
    assert remove_item(stock, "apple", 2) == {"apple": 3}
    assert stock == {"apple": 5}


def test_an_item_removed_to_zero_disappears():
    assert remove_item({"apple": 2, "pear": 1}, "apple", 2) == {"pear": 1}


@pytest.mark.parametrize("stock, name, quantity", [
    ({"apple": 1}, "pear", 1),
    ({"apple": 1}, "apple", 2),
    ({"apple": 1}, "apple", 0),
])
def test_removing_unknown_too_much_or_non_positive_is_refused(stock, name, quantity):
    with pytest.raises(ValueError):
        remove_item(stock, name, quantity)


def test_the_report_is_sorted_by_name():
    assert report({"pear": 1, "apple": 3}) == "apple: 3\npear: 1"


def test_an_empty_report_is_an_empty_string():
    assert report({}) == ""


def test_the_cli_runs_commands_and_prints_the_report(tmp_path):
    result = run_cli(tmp_path, "add apple 3", "", "add pear 1", "remove apple 1", "report")
    assert result.returncode == 0
    assert result.stdout.strip() == "apple: 2\npear: 1"


@pytest.mark.parametrize("line", ["sell apple 1", "add apple", "add apple three",
                                  "remove ghost 1"])
def test_the_cli_reports_bad_input_and_exits_1(tmp_path, line):
    result = run_cli(tmp_path, line)
    assert result.returncode == 1
    assert result.stderr.strip().splitlines()[-1].startswith("error:")


def test_the_cli_needs_a_commands_file(tmp_path):
    result = run_cli(tmp_path, args=[])
    assert result.returncode == 1
    assert result.stderr.startswith("error:")
