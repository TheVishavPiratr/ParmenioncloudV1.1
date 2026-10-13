"""Tests for the auth endpoints: signup, email OTP, login, password reset, /auth/me."""

import re
from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.modules.auth.models import OtpCode, User

SIGNUP = {
    "email": "Alice@Example.com",
    "password": "correct-horse",
    "full_name": "Alice",
}
EMAIL = "alice@example.com"

Outbox = list[dict[str, str]]


def last_code(outbox: Outbox) -> str:
    match = re.search(r"\b(\d{6})\b", outbox[-1]["body"])
    assert match, outbox[-1]["body"]
    return match.group(1)


def wrong_code(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def login(client: AsyncClient, password: str = SIGNUP["password"]):
    return await client.post("/auth/login", json={"email": EMAIL, "password": password})


async def signup_and_verify(client: AsyncClient, outbox: Outbox) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    response = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": last_code(outbox)}
    )
    assert response.status_code == 200


async def signup_verify_and_login(client: AsyncClient, outbox: Outbox) -> str:
    await signup_and_verify(client, outbox)
    return (await login(client)).json()["access_token"]


async def get_user(db_session: AsyncSession) -> User:
    return await db_session.scalar(select(User).where(User.email == EMAIL))


async def age_latest_otp(db_session: AsyncSession, **changes: datetime) -> None:
    otp = await db_session.scalar(select(OtpCode).order_by(OtpCode.created_at.desc()))
    for field, value in changes.items():
        setattr(otp, field, value)
    await db_session.commit()


# ---------- signup ----------


async def test_signup_creates_unverified_user_and_emails_code(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    response = await client.post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == EMAIL
    assert body["full_name"] == "Alice"
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert "password" not in body
    assert "password_hash" not in body

    user = await get_user(db_session)
    assert user.password_hash.startswith("$argon2")

    assert len(outbox) == 1
    assert outbox[0]["to"] == EMAIL
    code = last_code(outbox)
    otp = await db_session.scalar(select(OtpCode))
    assert code not in otp.code_hash


async def test_signup_duplicate_email_is_rejected(client: AsyncClient) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    response = await client.post(
        "/auth/signup", json={**SIGNUP, "email": "ALICE@example.com"}
    )

    assert response.status_code == 409


async def test_signup_validates_input(client: AsyncClient) -> None:
    short_password = await client.post(
        "/auth/signup", json={**SIGNUP, "password": "short"}
    )
    bad_email = await client.post(
        "/auth/signup", json={**SIGNUP, "email": "not-an-email"}
    )
    blank_name = await client.post("/auth/signup", json={**SIGNUP, "full_name": "   "})

    assert short_password.status_code == 422
    assert bad_email.status_code == 422
    assert blank_name.status_code == 422


# ---------- email verification ----------


async def test_login_blocked_until_email_verified(
    client: AsyncClient, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    before = await login(client)
    assert before.status_code == 403
    assert before.json()["detail"] == "Email is not verified"

    verify = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": last_code(outbox)}
    )
    assert verify.status_code == 200

    after = await login(client)
    assert after.status_code == 200


async def test_verify_rejects_wrong_reused_and_malformed_codes(
    client: AsyncClient, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    code = last_code(outbox)

    wrong = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": wrong_code(code)}
    )
    malformed = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": "12ab"}
    )
    right = await client.post("/auth/verify-email", json={"email": EMAIL, "code": code})
    reused = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": code}
    )
    unknown = await client.post(
        "/auth/verify-email", json={"email": "nobody@example.com", "code": code}
    )

    assert wrong.status_code == 400
    assert malformed.status_code == 422
    assert right.status_code == 200
    assert reused.status_code == 400
    assert unknown.status_code == 400


async def test_code_locked_after_too_many_wrong_attempts(
    client: AsyncClient, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    code = last_code(outbox)

    for _ in range(5):
        response = await client.post(
            "/auth/verify-email", json={"email": EMAIL, "code": wrong_code(code)}
        )
        assert response.status_code == 400

    right_but_locked = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": code}
    )
    assert right_but_locked.status_code == 400


async def test_expired_code_is_rejected(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    code = last_code(outbox)
    await age_latest_otp(
        db_session, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    response = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": code}
    )
    assert response.status_code == 400


async def test_resend_respects_cooldown_and_replaces_old_code(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    first_code = last_code(outbox)

    too_soon = await client.post("/auth/resend-verification", json={"email": EMAIL})
    assert too_soon.status_code == 202
    assert len(outbox) == 1

    await age_latest_otp(
        db_session, created_at=datetime.now(UTC) - timedelta(minutes=2)
    )
    later = await client.post("/auth/resend-verification", json={"email": EMAIL})
    assert later.status_code == 202
    assert len(outbox) == 2
    second_code = last_code(outbox)

    if first_code != second_code:
        old = await client.post(
            "/auth/verify-email", json={"email": EMAIL, "code": first_code}
        )
        assert old.status_code == 400
    new = await client.post(
        "/auth/verify-email", json={"email": EMAIL, "code": second_code}
    )
    assert new.status_code == 200


async def test_resend_gives_same_answer_for_unknown_email(
    client: AsyncClient, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    outbox.clear()

    unknown = await client.post(
        "/auth/resend-verification", json={"email": "nobody@example.com"}
    )
    known = await client.post("/auth/resend-verification", json={"email": EMAIL})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert outbox == []  # unknown: nothing to send; known: still in cooldown


# ---------- password reset ----------


async def test_password_reset_flow(client: AsyncClient, outbox: Outbox) -> None:
    await signup_and_verify(client, outbox)

    forgot = await client.post("/auth/forgot-password", json={"email": EMAIL})
    assert forgot.status_code == 202
    assert "reset" in outbox[-1]["subject"].lower()

    reset = await client.post(
        "/auth/reset-password",
        json={
            "email": EMAIL,
            "code": last_code(outbox),
            "new_password": "new-pass-123",
        },
    )
    assert reset.status_code == 200

    assert (await login(client)).status_code == 401
    assert (await login(client, "new-pass-123")).status_code == 200


async def test_password_reset_rejects_wrong_code(
    client: AsyncClient, outbox: Outbox
) -> None:
    await signup_and_verify(client, outbox)
    await client.post("/auth/forgot-password", json={"email": EMAIL})

    response = await client.post(
        "/auth/reset-password",
        json={
            "email": EMAIL,
            "code": wrong_code(last_code(outbox)),
            "new_password": "new-pass-123",
        },
    )

    assert response.status_code == 400
    assert (await login(client)).status_code == 200


async def test_verification_code_cannot_reset_password(
    client: AsyncClient, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)

    response = await client.post(
        "/auth/reset-password",
        json={
            "email": EMAIL,
            "code": last_code(outbox),
            "new_password": "new-pass-123",
        },
    )

    assert response.status_code == 400


async def test_password_reset_also_verifies_email(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await client.post("/auth/signup", json=SIGNUP)
    await client.post("/auth/forgot-password", json={"email": EMAIL})
    await client.post(
        "/auth/reset-password",
        json={
            "email": EMAIL,
            "code": last_code(outbox),
            "new_password": "new-pass-123",
        },
    )

    assert (await get_user(db_session)).is_verified is True
    assert (await login(client, "new-pass-123")).status_code == 200


async def test_forgot_password_gives_same_answer_for_unknown_email(
    client: AsyncClient, outbox: Outbox
) -> None:
    await signup_and_verify(client, outbox)
    outbox.clear()

    unknown = await client.post(
        "/auth/forgot-password", json={"email": "nobody@example.com"}
    )
    known = await client.post("/auth/forgot-password", json={"email": EMAIL})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert len(outbox) == 1
    assert outbox[0]["to"] == EMAIL


# ---------- login and /auth/me ----------


async def test_login_returns_token_accepted_by_me(
    client: AsyncClient, outbox: Outbox
) -> None:
    token = await signup_verify_and_login(client, outbox)

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["is_verified"] is True


async def test_login_wrong_password_and_unknown_email_look_the_same(
    client: AsyncClient, outbox: Outbox
) -> None:
    await signup_and_verify(client, outbox)
    wrong_password = await login(client, "wrong-password")
    unknown_email = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever1"}
    )

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_login_disabled_account_is_forbidden(
    client: AsyncClient, db_session: AsyncSession, outbox: Outbox
) -> None:
    await signup_and_verify(client, outbox)
    user = await get_user(db_session)
    user.is_active = False
    await db_session.commit()

    response = await login(client)

    assert response.status_code == 403
    assert response.json()["detail"] == "Account is disabled"


async def test_me_requires_valid_token(client: AsyncClient, outbox: Outbox) -> None:
    token = await signup_verify_and_login(client, outbox)
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    expired = jwt.encode(
        {**claims, "exp": datetime.now(UTC) - timedelta(minutes=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    forged = jwt.encode(
        claims,
        "some-other-secret-that-is-long-enough-for-hs256",
        algorithm=settings.jwt_algorithm,
    )

    no_token = await client.get("/auth/me")
    responses = [no_token] + [
        await client.get("/auth/me", headers={"Authorization": f"Bearer {bad}"})
        for bad in ("not-a-jwt", expired, forged)
    ]

    for response in responses:
        assert response.status_code == 401
