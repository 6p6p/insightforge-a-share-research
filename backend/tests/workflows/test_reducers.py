"""Tests for state reducers."""

from app.workflows.reducers import merge_unique_strings


def test_dedup_preserves_first_occurrence() -> None:
    result = merge_unique_strings(["a", "b"], ["b", "c", "a"])
    assert result == ["a", "b", "c"]


def test_does_not_mutate_inputs() -> None:
    current = ["a", "b"]
    update = ["b", "c"]
    merge_unique_strings(current, update)
    assert current == ["a", "b"]
    assert update == ["b", "c"]


def test_none_inputs() -> None:
    assert merge_unique_strings(None, None) == []
    assert merge_unique_strings(["a"], None) == ["a"]
    assert merge_unique_strings(None, ["a"]) == ["a"]
