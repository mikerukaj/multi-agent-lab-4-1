"""A small stdlib JSON-Schema checker for the contracts' schemas.

Used for QA's findings (002 contracts/qa-findings.schema.json), the run event record
and the cost report (003 contracts). The constitution keeps project tests on the
standard library, so this implements only the keywords those schemas use: ``type``
(one name or a list), ``enum``, ``const``, ``pattern``, ``minLength``, ``minimum``,
``exclusiveMinimum``, ``maximum``, ``minItems``, ``maxItems``, ``required``,
``additionalProperties``, ``properties``, ``items``, ``$ref`` (local
``#/$defs/...``), ``anyOf`` and ``allOf`` with ``if``/``then``. ``format`` is
ignored. The schemas are copied to ``tests/fixtures/``; the ``SchemaCopyTests`` check
each copy matches its contract whenever ``specs/`` is present.

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


SPECS = Path(__file__).resolve().parents[1] / "specs"


def load_schema() -> dict:
    """Return the findings schema from the test fixture."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def load_fixture_schema(name: str) -> dict:
    """Return a schema copied into ``tests/fixtures/`` (e.g. ``run-event.schema.json``)."""
    return json.loads((FIXTURE.parent / name).read_text(encoding="utf-8"))


def contract_path(feature: str, name: str) -> Path:
    """Where a feature's contract schema lives (``specs/`` may be absent)."""
    return SPECS / feature / "contracts" / name


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
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above {schema['exclusiveMinimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
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


def _has_type(value, expected: str | list) -> bool:
    if isinstance(expected, list):
        return any(_has_type(value, option) for option in expected)
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, JSON_TYPES[expected])


def assert_matches_findings_schema(findings) -> None:
    """Raise ``AssertionError`` listing every schema violation in ``findings``."""
    schema = load_schema()
    errors = schema_errors(json.loads(json.dumps(findings)), schema, schema)
    if errors:
        raise AssertionError("findings do not match the schema:\n" + "\n".join(errors))
