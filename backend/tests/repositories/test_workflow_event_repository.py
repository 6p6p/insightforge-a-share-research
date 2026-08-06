"""Tests for the workflow event repository query construction."""

from uuid import uuid4

import pytest

from app.repositories.workflow_event_repository import WorkflowEventRepository


class _Result:
    def __init__(self, rows=None, scalar=None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self) -> None:
        self.executed = []
        self.result = _Result()

    def add(self, obj) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):
        self.executed.append(stmt)
        return self.result


@pytest.mark.asyncio
async def test_list_after_passes_params() -> None:
    session = _FakeSession()
    repo = WorkflowEventRepository(session)
    run_id = uuid4()

    await repo.list_after(run_id=run_id, after_event_id=7, limit=100)

    assert len(session.executed) == 1
    compiled = str(session.executed[0])
    assert "event_id > " in compiled


@pytest.mark.asyncio
async def test_get_latest_event_id_returns_none_when_empty() -> None:
    session = _FakeSession()
    repo = WorkflowEventRepository(session)

    assert await repo.get_latest_event_id(uuid4()) is None


@pytest.mark.asyncio
async def test_count_for_run() -> None:
    session = _FakeSession()
    session.result = _Result(scalar=0)
    repo = WorkflowEventRepository(session)

    assert await repo.count_for_run(uuid4()) == 0
