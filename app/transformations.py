from __future__ import annotations

from copy import deepcopy
from typing import Any


class TransformationError(ValueError):
    pass


def _parts(path: str) -> list[str]:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise TransformationError("JSON path must not be empty")
    return parts


def _get(data: Any, path: str) -> Any:
    current = data
    for part in _parts(path):
        if not isinstance(current, dict) or part not in current:
            raise TransformationError(f"source path does not exist: {path}")
        current = current[part]
    return deepcopy(current)


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = _parts(path)
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise TransformationError(f"path crosses a non-object value: {path}")
        current = child
    current[parts[-1]] = deepcopy(value)


def _pop(data: dict[str, Any], path: str) -> Any:
    current = data
    parts = _parts(path)
    for part in parts[:-1]:
        if not isinstance(current.get(part), dict):
            raise TransformationError(f"source path does not exist: {path}")
        current = current[part]
    if parts[-1] not in current:
        raise TransformationError(f"source path does not exist: {path}")
    return current.pop(parts[-1])


def validate_rules(rules: list[dict[str, Any]]) -> None:
    supported = {"rename", "remove", "add", "copy", "wrap"}
    for index, rule in enumerate(rules):
        operation = rule.get("op")
        if operation not in supported:
            raise TransformationError(f"rule {index}: unsupported operation {operation!r}")
        if operation in {"rename", "copy"} and not all(
            isinstance(rule.get(key), str) for key in ("from", "to")
        ):
            raise TransformationError(f"rule {index}: {operation} requires string from/to paths")
        if operation == "remove" and not isinstance(rule.get("path"), str):
            raise TransformationError(f"rule {index}: remove requires a string path")
        if operation == "add" and not isinstance(rule.get("path"), str):
            raise TransformationError(f"rule {index}: add requires a string path")
        if operation == "wrap" and not isinstance(rule.get("root"), str):
            raise TransformationError(f"rule {index}: wrap requires a string root")


def transform_payload(payload: Any, rules: list[dict[str, Any]]) -> Any:
    validate_rules(rules)
    result = deepcopy(payload)
    if rules and not isinstance(result, dict):
        raise TransformationError("transformations require a JSON object payload")
    for rule in rules:
        operation = rule["op"]
        if operation == "rename":
            _set(result, rule["to"], _pop(result, rule["from"]))
        elif operation == "remove":
            _pop(result, rule["path"])
        elif operation == "add":
            _set(result, rule["path"], rule.get("value"))
        elif operation == "copy":
            _set(result, rule["to"], _get(result, rule["from"]))
        elif operation == "wrap":
            result = {rule["root"]: result}
    return result
