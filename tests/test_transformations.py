from __future__ import annotations

import pytest

from app.transformations import TransformationError, transform_payload, validate_rules


def test_all_transformations_are_ordered_and_non_mutating() -> None:
    payload = {"customer": {"email": "a@example.com"}, "amount": 2500, "drop": True}
    rules = [
        {"op": "copy", "from": "customer.email", "to": "email"},
        {"op": "rename", "from": "amount", "to": "total"},
        {"op": "remove", "path": "drop"},
        {"op": "add", "path": "event.type", "value": "purchase"},
    ]
    assert transform_payload(payload, rules) == {
        "customer": {"email": "a@example.com"},
        "email": "a@example.com",
        "total": 2500,
        "event": {"type": "purchase"},
    }
    assert payload["amount"] == 2500


def test_wrap_payload() -> None:
    assert transform_payload({"id": 1}, [{"op": "wrap", "root": "event"}]) == {"event": {"id": 1}}


@pytest.mark.parametrize(
    "rules,message",
    [
        ([{"op": "execute"}], "unsupported"),
        ([{"op": "copy", "from": "a"}], "requires string"),
        ([{"op": "remove"}], "requires a string"),
        ([{"op": "add"}], "requires a string"),
        ([{"op": "wrap"}], "requires a string"),
    ],
)
def test_invalid_rule_shapes(rules: list[dict[str, str]], message: str) -> None:
    with pytest.raises(TransformationError, match=message):
        validate_rules(rules)


@pytest.mark.parametrize(
    "payload,rules,message",
    [
        ({}, [{"op": "remove", "path": "missing"}], "does not exist"),
        ({"a": 1}, [{"op": "add", "path": "a.b", "value": 2}], "non-object"),
        ({"a": 1}, [{"op": "copy", "from": "a.b", "to": "c"}], "does not exist"),
        ([], [{"op": "add", "path": "a", "value": 1}], "require a JSON object"),
        ({}, [{"op": "add", "path": "", "value": 1}], "must not be empty"),
    ],
)
def test_invalid_transform_paths(payload, rules, message: str) -> None:
    with pytest.raises(TransformationError, match=message):
        transform_payload(payload, rules)
