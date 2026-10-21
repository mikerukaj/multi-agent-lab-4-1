"""Tests for ``orchestration.validation`` and the reply ``TypedDict``s it checks.

Every model reply is validated against a ``TypedDict`` before it is used
(specs/003-production-hardening/contracts/llm-output-validation.md). The
``SchemaAgreementTests`` keep each JSON schema sent to the model and its reply type in
step, so neither can change without the other.
"""

import typing
import unittest
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from agents.coder_agent import CRITIQUE_FORMAT, SUMMARY_FORMAT
from agents.pm_agent import CONSOLIDATION_SCHEMA, SPEC_SCHEMA
from agents.qa_agent import DISPUTES_FORMAT, REVIEW_FORMAT, WRITE_TESTS_FORMAT
from orchestration import state
from orchestration.validation import first_problems, validate
from tests.team_fakes import FakeFileTools
from tools import session_tool_definitions
from tools.file_tools import READ_FILE_TOOL, WRITE_FILE_TOOL
from tools.python_exec import EXEC_PYTHON_TOOL


class Item(TypedDict):
    name: str
    priority: Literal["high", "medium", "low"]
    count: int
    ratio: float
    note: NotRequired[str]


class Plan(TypedDict):
    title: str
    tasks: list[Item]
    labels: dict[str, int]
    owner: str | None
    done: bool


def item(**overrides) -> dict:
    return {"name": "a", "priority": "high", "count": 1, "ratio": 0.5, **overrides}


def plan(**overrides) -> dict:
    return {
        "title": "t", "tasks": [item(), item(), item()], "labels": {"x": 1},
        "owner": None, "done": False, **overrides,
    }


class ValidateTests(unittest.TestCase):
    def test_a_valid_nested_typeddict_has_no_problems(self) -> None:
        self.assertEqual(validate(plan(), Plan), [])

    def test_a_missing_required_key_is_reported(self) -> None:
        value = plan()
        del value["done"]
        self.assertEqual(validate(value, Plan), ["$: missing required key 'done'"])

    def test_an_unknown_key_is_reported(self) -> None:
        self.assertEqual(validate(plan(extra=1), Plan), ["$: unknown key 'extra'"])

    def test_an_absent_not_required_key_is_fine(self) -> None:
        self.assertEqual(validate(item(), Item), [])
        self.assertEqual(validate(item(note="n"), Item), [])

    def test_a_wrong_scalar_type_is_reported(self) -> None:
        self.assertEqual(validate(plan(title=3), Plan), ["$.title: expected str, got int"])

    def test_bool_is_not_accepted_for_int(self) -> None:
        self.assertEqual(validate(item(count=True), Item), ["$.count: expected int, got bool"])

    def test_int_is_accepted_for_float(self) -> None:
        self.assertEqual(validate(item(ratio=2), Item), [])
        self.assertEqual(validate(item(ratio=False), Item), ["$.ratio: expected float, got bool"])

    def test_a_literal_value_outside_its_set_is_reported(self) -> None:
        self.assertEqual(
            validate(item(priority="urgent"), Item),
            ["$.priority: 'urgent' is not one of ('high', 'medium', 'low')"],
        )

    def test_a_bad_list_element_reports_its_path(self) -> None:
        value = plan(tasks=[item(), item(), item(priority="urgent")])
        self.assertEqual(
            validate(value, Plan),
            ["$.tasks[2].priority: 'urgent' is not one of ('high', 'medium', 'low')"],
        )
        self.assertEqual(validate(plan(tasks="x"), Plan), ["$.tasks: expected list, got str"])

    def test_dict_values_are_checked(self) -> None:
        self.assertEqual(
            validate(plan(labels={"x": 1, "y": "2"}), Plan),
            ["$.labels['y']: expected int, got str"],
        )
        self.assertEqual(
            validate(plan(labels={3: 1}), Plan), ["$.labels: key 3 is not a str"]
        )

    def test_optional_values_accept_none_or_the_type(self) -> None:
        self.assertEqual(validate(plan(owner="me"), Plan), [])
        self.assertEqual(
            validate(plan(owner=4), Plan), ["$.owner: 4 does not match str | None"]
        )

    def test_problems_are_reported_in_document_order(self) -> None:
        value = plan(title=1, tasks=[item(count="x")], done="no")
        self.assertEqual(
            validate(value, Plan),
            [
                "$.title: expected str, got int",
                "$.tasks[0].count: expected int, got str",
                "$.done: expected bool, got str",
            ],
        )

    def test_a_non_object_for_a_typeddict_is_reported(self) -> None:
        self.assertEqual(validate([1], Plan), ["$: expected object, got list"])

    def test_an_unsupported_annotation_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            validate(1, set[int])

    def test_first_problems_joins_at_most_the_limit(self) -> None:
        self.assertEqual(first_problems(["a", "b", "c", "d"]), "a; b; c (and 1 more)")
        self.assertEqual(first_problems(["a"], limit=3), "a")


def _annotation_kind(annotation) -> tuple:
    """Describe an annotation as the JSON-schema shape it must mirror."""
    origin = typing.get_origin(annotation)
    if origin in (NotRequired, typing.Required):
        return _annotation_kind(typing.get_args(annotation)[0])
    if origin is Literal:
        return ("enum", tuple(typing.get_args(annotation)))
    if origin is list:
        return ("array", typing.get_args(annotation)[0])
    if typing.is_typeddict(annotation):
        return ("object", annotation)
    return ({str: "string", int: "integer", float: "number", bool: "boolean"}[annotation],)


class SchemaAgreementTests(unittest.TestCase):
    """Each JSON schema the model sees agrees with the reply ``TypedDict`` that checks it."""

    def assert_agrees(self, schema: dict, reply_type: type, path: str = "$") -> None:
        hints = typing.get_type_hints(reply_type, include_extras=True)
        self.assertEqual(schema["type"], "object", path)
        self.assertEqual(set(schema["properties"]), set(hints), path)
        self.assertEqual(set(schema["required"]), set(reply_type.__required_keys__), path)
        for name, annotation in hints.items():
            self.assert_property(schema["properties"][name], annotation, f"{path}.{name}")

    def assert_property(self, prop: dict, annotation, path: str) -> None:
        kind = _annotation_kind(annotation)
        if kind[0] == "enum":
            self.assertEqual(prop["type"], "string", path)
            self.assertEqual(tuple(prop["enum"]), kind[1], path)
        elif kind[0] == "array":
            self.assertEqual(prop["type"], "array", path)
            self.assert_property(prop["items"], kind[1], f"{path}[]")
        elif kind[0] == "object":
            self.assert_agrees(prop, kind[1], path)
        else:
            self.assertEqual(prop["type"], kind[0], path)
            self.assertNotIn("enum", prop, path)

    def test_every_structured_reply_schema_matches_its_typeddict(self) -> None:
        pairs = [
            (SPEC_SCHEMA, state.PMSpecReply),
            (CONSOLIDATION_SCHEMA, state.PMConsolidationReply),
            (SUMMARY_FORMAT["schema"], state.CoderSummaryReply),
            (CRITIQUE_FORMAT["schema"], state.CritiqueReply),
            (WRITE_TESTS_FORMAT, state.QATestFilesReply),
            (DISPUTES_FORMAT, state.QADisputeReply),
            (REVIEW_FORMAT, state.QAReviewReply),
        ]
        for schema, reply_type in pairs:
            with self.subTest(reply_type=reply_type.__name__):
                self.assert_agrees(schema, reply_type)

    def test_tool_input_typeddicts_match_the_session_tool_schemas(self) -> None:
        inputs = {
            "read_file": state.ReadFileInput,
            "write_file": state.WriteFileInput,
            "exec_python": state.ExecPythonInput,
        }
        session = session_tool_definitions(FakeFileTools(Path(".")))
        self.assertEqual([tool["name"] for tool in session], list(inputs))
        for tool in session:
            with self.subTest(tool=tool["name"]):
                self.assert_agrees(tool["input_schema"], inputs[tool["name"]])

    def test_raw_tool_schemas_match_without_the_purpose_argument(self) -> None:
        raw = {"read_file": READ_FILE_TOOL, "write_file": WRITE_FILE_TOOL,
               "exec_python": EXEC_PYTHON_TOOL}
        inputs = {"read_file": state.ReadFileInput, "write_file": state.WriteFileInput,
                  "exec_python": state.ExecPythonInput}
        for name, tool in raw.items():
            with self.subTest(tool=name):
                keys = set(typing.get_type_hints(inputs[name])) - {"purpose"}
                self.assertEqual(set(tool["input_schema"]["properties"]), keys)

    def test_valid_and_invalid_replies_are_told_apart(self) -> None:
        task = {"title": "t", "description": "d", "acceptance_criteria": ["c"],
                "priority": "high"}
        reply = {"name": "n", "description": "d", "acceptance_criteria": ["c"],
                 "assumptions": [], "tasks": [task], "rejection_reason": ""}
        self.assertEqual(validate(reply, state.PMSpecReply), [])
        self.assertEqual(
            validate({**reply, "tasks": [{**task, "priority": "urgent"}]}, state.PMSpecReply),
            ["$.tasks[0].priority: 'urgent' is not one of ('high', 'medium', 'low')"],
        )


if __name__ == "__main__":
    unittest.main()
