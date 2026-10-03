import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.core_database.database import CrudUserData
from src.core_database.models.db_helper import db_helper
from src.core_database.models.users import UserData


@pytest.fixture
async def user_database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(UserData.__table__.create)
    monkeypatch.setattr(db_helper, "engine", engine)
    try:
        yield CrudUserData()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_registering_existing_user_is_idempotent(user_database):
    data = {"user_id": 1001, "bot_username": "suggest_bot"}
    await user_database.insert_user(data)
    await user_database.insert_user(data)
    assert len(await user_database.get_user_data(**data)) == 1


@pytest.mark.asyncio
async def test_concurrent_registration_keeps_one_row(user_database):
    data = {"user_id": 1001, "bot_username": "suggest_bot"}
    await asyncio.gather(*(user_database.insert_user(data) for _ in range(10)))
    assert len(await user_database.get_user_data(**data)) == 1


@pytest.mark.asyncio
async def test_user_identity_includes_bot_username(user_database):
    pairs = [(1001, "first_bot"), (1001, "second_bot"), (1002, "first_bot")]
    for user_id, bot_username in pairs:
        await user_database.insert_user({"user_id": user_id, "bot_username": bot_username})
    rows = await user_database.get_user_data()
    assert {(row.user_id, row.bot_username) for row in rows} == set(pairs)


@pytest.mark.asyncio
async def test_user_can_register_again_after_deletion(user_database):
    data = {"user_id": 1001, "bot_username": "suggest_bot"}
    await user_database.insert_user(data)
    await user_database.delete_user_data(**data)
    await user_database.insert_user(data)
    assert len(await user_database.get_user_data(**data)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "bot_username"])
async def test_registration_does_not_suppress_missing_required_fields(user_database, field):
    data = {"user_id": 1001, "bot_username": "suggest_bot", field: None}
    with pytest.raises(IntegrityError):
        await user_database.insert_user(data)
    assert await user_database.get_user_data() == []


@pytest.mark.asyncio
async def test_registration_does_not_suppress_unrelated_primary_key_conflict(user_database):
    await user_database.insert_user({"id": 1, "user_id": 1001, "bot_username": "suggest_bot"})
    with pytest.raises(IntegrityError):
        await user_database.insert_user({"id": 1, "user_id": 1002, "bot_username": "suggest_bot"})
    assert len(await user_database.get_user_data()) == 1


@pytest.mark.asyncio
async def test_postgres_registration_targets_only_user_bot_unique_key(monkeypatch):
    connection = SimpleNamespace(
        dialect=postgresql.dialect(), execute=AsyncMock(), commit=AsyncMock(),
    )

    class ConnectionContext:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(db_helper, "engine", SimpleNamespace(connect=ConnectionContext))
    await CrudUserData.insert_user({"user_id": 1001, "bot_username": "suggest_bot"})
    statement = connection.execute.await_args.args[0]
    sql = str(statement.compile(dialect=connection.dialect))
    assert "ON CONFLICT (user_id, bot_username) DO NOTHING" in sql
    connection.commit.assert_awaited_once()
