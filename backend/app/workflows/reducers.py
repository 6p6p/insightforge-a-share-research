"""State reducers for LangGraph."""


def merge_unique_strings(
    current: list[str] | None,
    update: list[str] | None,
) -> list[str]:
    """Merge lists of strings, de-duplicating while preserving first-occurrence order."""
    result = list(current or [])
    for value in update or []:
        if value not in result:
            result.append(value)
    return result
