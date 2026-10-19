"""A small stdlib JSON-Schema checker for QA's findings (contracts/qa-findings.schema.json).

The constitution keeps project tests on the standard library, so this implements
only the keywords that schema uses: ``type``, ``enum``, ``const``, ``pattern``,
``minLength``, ``minimum``, ``minItems``, ``required``, ``additionalProperties``,
``properties``, ``items``, ``$ref`` (local ``#/$defs/...``), ``anyOf`` and
``allOf`` with ``if``/``then``. ``specs/`` is not in git, so the schema is copied
to ``tests/fixtures/``; ``SchemaCopyTests`` in tests/test_qa_agent.py checks the
copy matches the contract whenever ``specs/`` is present.

The file name does not match ``test*.py``, so ``unittest discover`` never runs it.
"""

import json
import re
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "qa-findings.schema.json"
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "specs" / "002-qa-feedback-loop-mcp" / "contracts" / "qa-findings.schema.json"
)
JSON_TYPES = {
    "string": str, "integer": int, "array": list, "object": dict, "null": type(None),
    "boolean": bool, "number": (int, float),
}


def load_schema() -> dict:
    """Return the findings schema from the test fixture."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def schema_errors(value, schema: dict, root: dict, path: str = "$") -> list[str]:
    """Return every way ``value`` breaks ``schema`` (empty when it matches)."""
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        return schema_errors(value, root["$defs"][name], root, path)
    errors = _scalar_errors(value, schema, path)
    if "anyOf" in schema and all(
        schema_errors(value, option, root, path) for option in schema["anyOf"]
    ):
        errors.append(f"{path}: matches none of anyOf")
    for rule in schema.get("allOf", []):
        if not schema_errors(value, rule["if"], root, path):
            errors += schema_errors(value, rule["then"], root, path)
    if isinstance(value, dict):
        errors += _object_errors(value, schema, root, path)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(item, schema["items"], root, f"{path}[{index}]")
    return errors


def _scalar_errors(value, schema: dict, path: str) -> list[str]:
    errors = []
    expected = schema.get("type")
    if expected and not _has_type(value, expected):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: {value!r} != {schema['const']!r}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: below {schema['minimum']}")
    if isinstance(value, list) and len(value) < schema.get("minItems", 0):
        errors.append(f"{path}: fewer than {schema['minItems']} items")
    return errors


def _object_errors(value: dict, schema: dict, root: dict, path: str) -> list[str]:
    errors = [f"{path}: missing {key!r}" for key in schema.get("required", []) if key not in value]
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        errors += [f"{path}: unexpected {key!r}" for key in value if key not in properties]
    for key, subschema in properties.items():
        if key in value:
            errors += schema_errors(value[key], subschema, root, f"{path}.{key}")
    return errors


def _has_type(value, expected: str) -> bool:
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, JSON_TYPES[expected])


def assert_matches_findings_schema(findings) -> None:
    """Raise ``AssertionError`` listing every schema violation in ``findings``."""
    schema = load_schema()
    errors = schema_errors(json.loads(json.dumps(findings)), schema, schema)
    if errors:
        raise AssertionError("findings do not match the schema:\n" + "\n".join(errors))
