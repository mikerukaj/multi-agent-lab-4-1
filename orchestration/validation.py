"""Check model output against the project's ``TypedDict`` reply types before it is used.

Every structured model reply, every tool input the model asks for, and every cached
response served in place of a live one passes through ``validate`` first
(specs/003-production-hardening/contracts/llm-output-validation.md). The validator is
standard-library only (research R10): it understands ``TypedDict`` (with ``Required``
and ``NotRequired``), ``Literal``, ``list[T]``, ``dict[str, T]``, unions such as
``T | None``, and the JSON scalars.
"""

import types
import typing
from typing import Literal, NotRequired, Required

_SCALARS = {str: "str", int: "int", float: "float", bool: "bool"}


def validate(value: object, schema_type: type) -> list[str]:
    """Return the problems with ``value`` as an instance of ``schema_type``, in order.

    Each problem names its JSON path, for example
    ``"$.tasks[2].priority: 'urgent' is not one of ('high', 'medium', 'low')"``.
    An empty list means the value conforms. A ``TypedDict`` rejects unknown keys,
    mirroring ``additionalProperties: false`` in the schemas sent to the model.

    Raises:
        TypeError: if ``schema_type`` contains an annotation the validator does not
            support. That is a programming error, caught by the unit tests.
    """
    problems: list[str] = []
    _check(value, schema_type, "$", problems)
    return problems


def first_problems(problems: list[str], limit: int = 3) -> str:
    """Join the first ``limit`` problems into one line for an error message."""
    shown = "; ".join(problems[:limit])
    hidden = len(problems) - limit
    return f"{shown} (and {hidden} more)" if hidden > 0 else shown


def _check(value: object, annotation, path: str, problems: list[str]) -> None:
    origin = typing.get_origin(annotation)
    if origin in (Required, NotRequired):
        _check(value, typing.get_args(annotation)[0], path, problems)
    elif typing.is_typeddict(annotation):
        _check_typeddict(value, annotation, path, problems)
    elif origin is Literal:
        _check_literal(value, typing.get_args(annotation), path, problems)
    elif origin is list:
        _check_list(value, typing.get_args(annotation)[0], path, problems)
    elif origin is dict:
        _check_dict(value, typing.get_args(annotation), path, problems)
    elif origin in (typing.Union, types.UnionType):
        _check_union(value, annotation, path, problems)
    elif annotation is None or annotation is type(None):
        if value is not None:
            problems.append(f"{path}: expected null, got {_type_name(value)}")
    elif annotation in _SCALARS:
        _check_scalar(value, annotation, path, problems)
    else:
        raise TypeError(f"validate does not support the annotation {annotation!r}")


def _check_typeddict(value: object, schema_type: type, path: str, problems: list[str]) -> None:
    if not isinstance(value, dict):
        problems.append(f"{path}: expected object, got {_type_name(value)}")
        return
    hints = typing.get_type_hints(schema_type, include_extras=True)
    for key in hints:
        if key in value:
            _check(value[key], hints[key], f"{path}.{key}", problems)
        elif key in schema_type.__required_keys__:
            problems.append(f"{path}: missing required key {key!r}")
    for key in value:
        if key not in hints:
            problems.append(f"{path}: unknown key {key!r}")


def _check_literal(value: object, allowed: tuple, path: str, problems: list[str]) -> None:
    # ``True == 1`` in Python, so compare types too: a bool never matches an int literal.
    if not any(value == option and type(value) is type(option) for option in allowed):
        problems.append(f"{path}: {value!r} is not one of {allowed!r}")


def _check_list(value: object, item_type, path: str, problems: list[str]) -> None:
    if not isinstance(value, list):
        problems.append(f"{path}: expected list, got {_type_name(value)}")
        return
    for index, item in enumerate(value):
        _check(item, item_type, f"{path}[{index}]", problems)


def _check_dict(value: object, key_value_types: tuple, path: str, problems: list[str]) -> None:
    key_type, value_type = key_value_types
    if key_type is not str:
        raise TypeError(f"validate supports only dict[str, T], not dict[{key_type!r}, ...]")
    if not isinstance(value, dict):
        problems.append(f"{path}: expected object, got {_type_name(value)}")
        return
    for key, item in value.items():
        if not isinstance(key, str):
            problems.append(f"{path}: key {key!r} is not a str")
        else:
            _check(item, value_type, f"{path}[{key!r}]", problems)


def _check_union(value: object, annotation, path: str, problems: list[str]) -> None:
    for option in typing.get_args(annotation):
        if not validate(value, option):
            return
    names = " | ".join(_annotation_name(option) for option in typing.get_args(annotation))
    problems.append(f"{path}: {value!r} does not match {names}")


def _check_scalar(value: object, expected: type, path: str, problems: list[str]) -> None:
    if isinstance(value, bool) and expected is not bool:
        ok = False
    elif expected is float:
        ok = isinstance(value, (int, float))
    else:
        ok = isinstance(value, expected)
    if not ok:
        problems.append(f"{path}: expected {_SCALARS[expected]}, got {_type_name(value)}")


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _annotation_name(annotation) -> str:
    if annotation is type(None):
        return "None"
    return getattr(annotation, "__name__", repr(annotation))
