"""Tests for the notifications module: the inbox, alert settings, push and
email delivery, the alerts other modules send, and the Firebase and SMTP
transports."""

import json
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Self

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import Settings, settings
from api.modules.notifications import mailer, push, repository
from api.modules.notifications import service as notifications_service
from api.modules.notifications.models import Notification
from api.modules.notifications.push import FcmSender, PushResult
from tests.conftest import INVALID_PUSH_TOKENS
from tests.test_auth import last_code
from tests.test_edge_agent import ENROLL, Headers, bearer
from tests.test_end_to_end import sign_up

# The real transports; conftest swaps them for capturing fakes in every test.
real_send_email = notifications_service.send_email
real_send_push = notifications_service.send_push

EMAIL = "notified@example.com"
PASSWORD = "correct-horse-battery"  # what sign_up uses
PHONE = {"name": "Pixel 8", "platform": "android"}
TOKEN = "fcm-token-pixel-8"


# ---------- helpers ----------


async def phone_with_push(
    client: AsyncClient, name: str = "Pixel 8", token: str | None = TOKEN
) -> dict:
    """Log in from a phone and register its push token. Returns the token pair."""
    pair = (
        await client.post(
            "/devices/login",
            json={
                "email": EMAIL,
                "password": PASSWORD,
                "device": {**PHONE, "name": name},
            },
        )
    ).json()
    if token is not None:
        response = await client.put(
            f"/devices/{pair['device_id']}/push-token",
            json={"provider": "fcm", "token": token},
            headers=bearer(pair["access_token"]),
        )
        assert response.status_code == 200, response.text
    return pair


async def inbox(client: AsyncClient, user: Headers, **params) -> list[dict]:
    response = await client.get("/notifications", params=params, headers=user)
    assert response.status_code == 200, response.text
    return response.json()


async def set_channels(
    client: AsyncClient, user: Headers, category: str, **channels: bool
) -> dict:
    response = await client.patch(
        f"/notifications/preferences/{category}", json=channels, headers=user
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------- alerts sent by other modules ----------


async def test_new_sign_in_alerts_inbox_other_phones_and_email(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)

    tablet = await phone_with_push(client, name="Galaxy Tab", token=None)

    [item, _] = await inbox(client, web)
    assert item["category"] == "security"
    assert item["kind"] == "device_linked"
    assert item["title"] == "New sign-in"
    assert "Galaxy Tab (android)" in item["body"]
    assert item["data"] == {"device_id": tablet["device_id"]}
    assert item["read"] is False
    # The first phone, the only one with a push token, gets the alert.
    alert = pushbox[-1]
    assert alert["token"] == TOKEN
    assert alert["title"] == "New sign-in"
    assert alert["data"] == {
        "device_id": tablet["device_id"],
        "notification_id": item["id"],
        "category": "security",
        "kind": "device_linked",
    }
    # Security alerts are emailed by default.
    assert outbox[-1]["to"] == EMAIL
    assert outbox[-1]["subject"] == "VisionPulse: New sign-in"
    assert "Galaxy Tab" in outbox[-1]["body"]


async def test_password_reset_alerts_the_owner(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)
    await client.post("/auth/forgot-password", json={"email": EMAIL})
    reset = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "code": last_code(outbox), "new_password": "new-pass-1"},
    )
    assert reset.status_code == 200, reset.text

    # The phone was signed out, but its push token still reaches the owner.
    assert pushbox[-1]["title"] == "Password changed"
    assert outbox[-1]["subject"] == "VisionPulse: Password changed"
    login = await client.post(
        "/auth/login", json={"email": EMAIL, "password": "new-pass-1"}
    )
    user = bearer(login.json()["access_token"])
    assert (await inbox(client, user))[0]["kind"] == "password_changed"


async def test_reused_session_alerts_the_owner(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)
    stolen = await phone_with_push(client, name="Old Phone", token=None)

    for _ in range(2):
        await client.post(
            "/devices/refresh", json={"refresh_token": stolen["refresh_token"]}
        )

    item = (await inbox(client, web))[0]
    assert item["kind"] == "session_reused"
    assert "Old Phone was signed out" in item["body"]
    assert item["data"] == {"device_id": stolen["device_id"]}
    assert pushbox[-1]["title"] == "Sign-in used from two places"


async def test_enrolled_agent_alerts_without_email(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)
    emails = len(outbox)
    created = await client.post("/agents", json={"name": "Shop"}, headers=web)
    agent = created.json()

    enrolled = await client.post(
        "/agent/enroll",
        json={**ENROLL, "enrollment_token": agent["enrollment_token"]},
    )
    assert enrolled.status_code == 200, enrolled.text

    item = (await inbox(client, web))[0]
    assert item["category"] == "system"
    assert item["kind"] == "agent_enrolled"
    assert "Shop is now connected from edge-box" in item["body"]
    assert item["data"] == {"agent_id": agent["agent"]["id"]}
    assert pushbox[-1]["title"] == "Edge box connected"
    assert len(outbox) == emails  # system alerts are not emailed by default


# ---------- push delivery ----------


async def test_push_skips_devices_with_alerts_off(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    muted = await phone_with_push(client, token="muted-token")
    await client.patch(
        f"/devices/{muted['device_id']}",
        json={"notifications_enabled": False},
        headers=web,
    )
    await phone_with_push(client, name="Second", token="second-token")
    pushbox.clear()

    await phone_with_push(client, name="Third", token=None)

    assert [p["token"] for p in pushbox] == ["second-token"]


async def test_uninstalled_app_token_is_dropped(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    [dead_token] = INVALID_PUSH_TOKENS
    gone = await phone_with_push(client, token=dead_token)

    await phone_with_push(client, name="New Phone", token=None)

    devices = {d["id"]: d for d in (await client.get("/devices", headers=web)).json()}
    assert devices[gone["device_id"]]["has_push_token"] is False


async def test_test_push_reaches_devices_and_skips_the_inbox(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client, token="first-token")
    await phone_with_push(client, name="Second", token="second-token")
    [dead_token] = INVALID_PUSH_TOKENS
    await phone_with_push(client, name="Dead", token=dead_token)
    # Category settings do not apply to the test alert.
    await set_channels(client, web, "security", push=False)
    before = len(await inbox(client, web))
    pushbox.clear()

    response = await client.post("/notifications/test", headers=web)

    assert response.status_code == 200, response.text
    assert response.json() == {"devices": 3, "delivered": 2}
    assert {p["token"] for p in pushbox} == {"first-token", "second-token"}
    assert pushbox[0]["data"] == {"kind": "test"}
    assert len(await inbox(client, web)) == before


# ---------- settings ----------


async def test_preferences_start_at_defaults(client: AsyncClient, outbox: list) -> None:
    web = await sign_up(client, outbox, EMAIL)

    response = await client.get("/notifications/preferences", headers=web)

    assert response.json() == [
        {"category": "security", "push": True, "email": True},
        {"category": "system", "push": True, "email": False},
        {"category": "detection", "push": True, "email": False},
    ]


async def test_switched_off_channels_are_skipped_but_inbox_still_fills(
    client: AsyncClient, outbox: list, pushbox: list
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)
    assert await set_channels(client, web, "security", push=False) == {
        "category": "security",
        "push": False,
        "email": True,
    }
    assert await set_channels(client, web, "security", email=False) == {
        "category": "security",
        "push": False,
        "email": False,
    }
    pushbox.clear()
    emails = len(outbox)

    await phone_with_push(client, name="Another", token=None)

    assert pushbox == []
    assert len(outbox) == emails
    assert (await inbox(client, web))[0]["kind"] == "device_linked"


async def test_preference_changes_are_per_user_and_audited(
    client: AsyncClient, outbox: list
) -> None:
    alice = await sign_up(client, outbox, "alice@example.com")
    bob = await sign_up(client, outbox, "bob@example.com")

    await set_channels(client, alice, "system", email=True)

    bobs = (await client.get("/notifications/preferences", headers=bob)).json()
    assert bobs[1] == {"category": "system", "push": True, "email": False}
    audit = (
        await client.get(
            "/audit",
            params={"action": "notification_settings_updated"},
            headers=alice,
        )
    ).json()
    assert audit[0]["details"] == {"category": "system", "push": True, "email": True}


async def test_unknown_category_is_rejected(client: AsyncClient, outbox: list) -> None:
    web = await sign_up(client, outbox, EMAIL)

    response = await client.patch(
        "/notifications/preferences/marketing", json={"push": False}, headers=web
    )

    assert response.status_code == 422


# ---------- inbox ----------


async def make_notifications(
    db_session: AsyncSession, user_id: uuid.UUID, count: int
) -> None:
    now = datetime.now(UTC)
    for i in range(count):
        db_session.add(
            Notification(
                user_id=user_id,
                category="system",
                kind="test",
                title=f"Number {i}",
                body="Body",
                created_at=now - timedelta(minutes=count - i),
            )
        )
    await db_session.commit()


async def user_id(client: AsyncClient, user: Headers) -> uuid.UUID:
    return uuid.UUID((await client.get("/auth/me", headers=user)).json()["id"])


async def test_inbox_pages_newest_first_and_counts_unread(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await make_notifications(db_session, await user_id(client, web), 5)

    page = await inbox(client, web, limit=3)
    assert [n["title"] for n in page] == ["Number 4", "Number 3", "Number 2"]
    rest = await inbox(client, web, before=page[-1]["created_at"])
    assert [n["title"] for n in rest] == ["Number 1", "Number 0"]
    count = await client.get("/notifications/unread-count", headers=web)
    assert count.json() == {"unread": 5}


async def test_marking_read_and_deleting(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await make_notifications(db_session, await user_id(client, web), 3)
    first, second, third = await inbox(client, web)

    read = await client.post(f"/notifications/{first['id']}/read", headers=web)
    assert read.status_code == 200
    assert read.json()["read"] is True
    again = await client.post(f"/notifications/{first['id']}/read", headers=web)
    assert again.json()["read"] is True
    unread = await inbox(client, web, unread_only=True)
    assert [n["id"] for n in unread] == [second["id"], third["id"]]

    deleted = await client.delete(f"/notifications/{second['id']}", headers=web)
    assert deleted.status_code == 204
    marked = await client.post("/notifications/read-all", headers=web)
    assert marked.json() == {"marked": 1}
    count = await client.get("/notifications/unread-count", headers=web)
    assert count.json() == {"unread": 0}
    assert [n["id"] for n in await inbox(client, web)] == [first["id"], third["id"]]


async def test_inbox_is_private(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    alice = await sign_up(client, outbox, "alice@example.com")
    bob = await sign_up(client, outbox, "bob@example.com")
    await make_notifications(db_session, await user_id(client, alice), 1)
    [item] = await inbox(client, alice)

    assert await inbox(client, bob) == []
    for response in (
        await client.post(f"/notifications/{item['id']}/read", headers=bob),
        await client.delete(f"/notifications/{item['id']}", headers=bob),
    ):
        assert response.status_code == 404
    assert (await client.post("/notifications/read-all", headers=bob)).json() == {
        "marked": 0
    }
    assert (await inbox(client, alice))[0]["read"] is False
    assert (await client.get("/notifications")).status_code == 401


async def test_purge_removes_only_expired_notifications(
    client: AsyncClient, outbox: list, db_session: AsyncSession
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await make_notifications(db_session, await user_id(client, web), 3)
    now = datetime.now(UTC)
    await db_session.execute(
        update(Notification)
        .where(Notification.title != "Number 2")
        .values(created_at=now - timedelta(days=91))
    )
    await db_session.commit()

    assert await notifications_service.purge_expired(db_session, now) == 2
    assert await db_session.scalar(select(func.count(Notification.id))) == 1


# ---------- failures never break the action ----------


async def test_failed_store_and_delivery_do_not_break_sign_in(
    client: AsyncClient, outbox: list, pushbox: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)

    async def broken_store(*args: object) -> None:
        raise RuntimeError("database hiccup")

    async def broken_push(*args: object) -> PushResult:
        raise RuntimeError("push service down")

    with monkeypatch.context() as patched:
        patched.setattr(repository, "add_notification", broken_store)
        patched.setattr(notifications_service, "send_push", broken_push)
        response = await client.post(
            "/devices/login",
            json={"email": EMAIL, "password": PASSWORD, "device": PHONE},
        )

    assert response.status_code == 200, response.text
    assert "access_token" in response.json()


async def test_notify_survives_a_failing_insert(
    client: AsyncClient, outbox: list, pushbox: list, db_session: AsyncSession
) -> None:
    web = await sign_up(client, outbox, EMAIL)
    await phone_with_push(client)
    pushbox.clear()

    # A title longer than the column makes the insert itself fail.
    await notifications_service.notify(
        db_session,
        await user_id(client, web),
        notifications_service.NotificationCategory.SYSTEM,
        "too_long",
        "x" * 500,
        "Body",
    )

    assert len(pushbox) == 1  # delivery still went ahead
    assert (await inbox(client, web))[0]["kind"] == "device_linked"


# ---------- transports ----------


@pytest.fixture
def service_account(tmp_path: Path) -> tuple[dict, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credentials = {
        "type": "service_account",
        "project_id": "visionpulse-test",
        "client_email": "push@visionpulse-test.iam.gserviceaccount.com",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return credentials, key.public_key()


class FakeGoogle:
    """Google's token endpoint and FCM, answering from a script of statuses."""

    def __init__(self, public_key: rsa.RSAPublicKey, *fcm_replies: httpx.Response):
        self.public_key = public_key
        self.replies = list(fcm_replies)
        self.tokens_issued = 0
        self.messages: list[dict] = []
        self.auth_headers: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = dict(
                pair.split("=", 1) for pair in request.content.decode().split("&")
            )
            claims = jwt.decode(
                form["assertion"],
                self.public_key,
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
            )
            assert claims["scope"].endswith("firebase.messaging")
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{self.tokens_issued}",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/v1/projects/visionpulse-test/messages:send"
        self.messages.append(json.loads(request.content))
        self.auth_headers.append(request.headers["Authorization"])
        return self.replies.pop(0) if self.replies else httpx.Response(200, json={})


def sender(credentials: dict, google: FakeGoogle) -> FcmSender:
    return FcmSender(credentials, transport=httpx.MockTransport(google))


async def test_fcm_signs_in_once_and_sends(service_account) -> None:
    credentials, public_key = service_account
    google = FakeGoogle(public_key)
    fcm = sender(credentials, google)

    first = await fcm.send("device-token", "Title", "Body", {"kind": "test"})
    second = await fcm.send("device-token", "Title", "Body", {"kind": "test"})

    assert first is second is PushResult.SENT
    assert google.tokens_issued == 1
    assert google.auth_headers == ["Bearer access-1", "Bearer access-1"]
    message = google.messages[0]["message"]
    assert message["token"] == "device-token"
    assert message["notification"] == {"title": "Title", "body": "Body"}
    assert message["data"] == {"kind": "test"}


async def test_fcm_signs_in_again_after_401(service_account) -> None:
    credentials, public_key = service_account
    google = FakeGoogle(public_key, httpx.Response(401))
    fcm = sender(credentials, google)

    assert await fcm.send("device-token", "T", "B", {}) is PushResult.SENT
    assert google.auth_headers == ["Bearer access-1", "Bearer access-2"]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}), "invalid"),
        (
            httpx.Response(
                400,
                json={
                    "error": {
                        "status": "INVALID_ARGUMENT",
                        "message": "The registration token is not a valid FCM "
                        "registration token",
                    }
                },
            ),
            "invalid",
        ),
        # A bad message must not cost the user their token.
        (
            httpx.Response(
                400,
                json={"error": {"status": "INVALID_ARGUMENT", "message": "bad data"}},
            ),
            "failed",
        ),
        (httpx.Response(400, text="not json"), "failed"),
        (httpx.Response(429), "failed"),
        (httpx.Response(503), "failed"),
    ],
)
async def test_fcm_classifies_refusals(
    service_account, reply: httpx.Response, expected: str
) -> None:
    credentials, public_key = service_account
    fcm = sender(credentials, FakeGoogle(public_key, reply))

    result = await fcm.send("device-token", "T", "B", {})

    assert (
        result
        is {
            "invalid": PushResult.INVALID_TOKEN,
            "failed": PushResult.FAILED,
        }[expected]
    )


async def test_fcm_network_error_is_a_failure(service_account) -> None:
    credentials, _ = service_account

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    fcm = FcmSender(credentials, transport=httpx.MockTransport(offline))

    assert await fcm.send("device-token", "T", "B", {}) is PushResult.FAILED


async def test_send_push_uses_fcm_in_fcm_mode(
    service_account, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials, public_key = service_account
    google = FakeGoogle(public_key)
    monkeypatch.setattr(settings, "push_mode", "fcm")
    monkeypatch.setattr(push, "_fcm", sender(credentials, google))

    assert await real_send_push("fcm", "tok", "T", "B", {}) is PushResult.SENT
    assert await real_send_push("apns", "tok", "T", "B", {}) is PushResult.FAILED
    assert len(google.messages) == 1


async def test_console_push_prints(capsys: pytest.CaptureFixture) -> None:
    assert await real_send_push("fcm", "abcdefghijklmnop", "T", "B", {}) == "sent"
    out = capsys.readouterr().out
    assert "PUSH (console mode)" in out
    assert "abcdefghijklmnop" not in out  # tokens are not printed in full


class FakeSmtp:
    instances: ClassVar[list["FakeSmtp"]] = []

    def __init__(self, host: str, port: int, timeout: float, **kwargs: object):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent: list = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: object) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}")

    def send_message(self, message: object) -> None:
        self.sent.append(message)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(settings, "email_mode", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", "mailer")
    monkeypatch.setattr(settings, "smtp_password", "mail-pass")
    return FakeSmtp


async def test_smtp_sends_with_starttls_and_login(smtp: type[FakeSmtp]) -> None:
    await real_send_email("owner@example.com", "Hello", "Body text")

    [server] = smtp.instances
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls == ["starttls", "login:mailer", "quit"]
    [message] = server.sent
    assert message["To"] == "owner@example.com"
    assert message["Subject"] == "Hello"
    assert message.get_content().strip() == "Body text"


async def test_smtp_over_ssl_skips_starttls(
    smtp: type[FakeSmtp], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "smtp_security", "ssl")
    monkeypatch.setattr(settings, "smtp_port", 465)

    await real_send_email("owner@example.com", "Hello", "Body")

    assert smtp.instances[0].calls == ["login:mailer", "quit"]


async def test_smtp_failure_is_logged_not_raised(
    smtp: type[FakeSmtp], monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def refuse(self: FakeSmtp, message: object) -> None:
        raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(FakeSmtp, "send_message", refuse)

    await real_send_email("owner@example.com", "Hello", "Body")

    assert "Could not send email" in caplog.text


def test_email_headers_cannot_be_injected() -> None:
    with pytest.raises(ValueError):
        mailer.build_message("a@example.com", "Hi\nBcc: evil@example.com", "Body")


def test_transports_need_their_settings() -> None:
    base = {
        "database_url": settings.database_url,
        "jwt_secret": "x",
        "camera_credentials_key": "x",
    }
    with pytest.raises(ValidationError, match="SMTP_HOST"):
        Settings(**base, email_mode="smtp", smtp_host=None)
    with pytest.raises(ValidationError, match="FCM_CREDENTIALS_FILE"):
        Settings(**base, push_mode="fcm", fcm_credentials_file=None)
