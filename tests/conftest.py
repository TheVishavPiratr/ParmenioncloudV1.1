"""Shared test fixtures: a rolled-back database session and an HTTP client."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from api.core.config import settings
from api.core.database import get_db
from api.main import app
from api.modules.notifications import service as notifications_service
from api.modules.notifications.push import PushResult

# Push tokens the fake push service treats as belonging to uninstalled apps.
INVALID_PUSH_TOKENS = {"uninstalled-app-token"}


@pytest.fixture(autouse=True)
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Run each test inside a transaction that is rolled back afterwards.

    Commits made by the app become savepoints, so nothing a test writes
    is left behind in the database.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            yield session
        finally:
            app.dependency_overrides.clear()
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture(autouse=True)
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Capture emails instead of printing them; each is {to, subject, body}."""
    sent: list[dict[str, str]] = []

    async def fake_send_email(to: str, subject: str, body: str) -> None:
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(notifications_service, "send_email", fake_send_email)
    return sent


@pytest.fixture(autouse=True)
def pushbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture push alerts instead of printing them; each is {provider, token,
    title, body, data}. Tokens in INVALID_PUSH_TOKENS count as uninstalled."""
    sent: list[dict] = []

    async def fake_send_push(
        provider: str, token: str, title: str, body: str, data: dict[str, str]
    ) -> PushResult:
        if token in INVALID_PUSH_TOKENS:
            return PushResult.INVALID_TOKEN
        sent.append(
            {
                "provider": provider,
                "token": token,
                "title": title,
                "body": body,
                "data": data,
            }
        )
        return PushResult.SENT

    monkeypatch.setattr(notifications_service, "send_push", fake_send_push)
    return sent


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
