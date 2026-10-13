"""Tests for the /health endpoint and app-wide behaviour."""

from httpx import ASGITransport, AsyncClient

from api.main import app


async def test_health() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


async def test_nan_in_request_is_422_not_500(client: AsyncClient) -> None:
    """JSON cannot echo NaN back in the error, which used to crash with a 500."""
    response = await client.post(
        "/auth/signup",
        content='{"email": "a@example.com", "password": "x", "full_name": NaN}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
