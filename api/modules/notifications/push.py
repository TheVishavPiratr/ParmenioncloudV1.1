"""Push delivery through Firebase Cloud Messaging (HTTP v1 API).

The server signs in as a Google service account: it signs a short JWT with the
account's private key, swaps it for an access token, and reuses that token
until shortly before it expires.
"""

import asyncio
import json
import logging
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import jwt

from api.core.config import settings

logger = logging.getLogger(__name__)

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
# Fetch a new access token this long before the old one runs out.
_TOKEN_MARGIN_S = 60


class PushResult(StrEnum):
    SENT = "sent"
    # The app was uninstalled or the token replaced: stop using it.
    INVALID_TOKEN = "invalid_token"
    # Anything else (network, quota, Firebase outage): try again next time.
    FAILED = "failed"


def _classify(response: httpx.Response) -> PushResult:
    if response.is_success:
        return PushResult.SENT
    if response.status_code == 404:  # UNREGISTERED
        return PushResult.INVALID_TOKEN
    if response.status_code == 400:
        # INVALID_ARGUMENT also covers a bad message, which must not cost the
        # user their token; only a bad token says so in the message.
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = ""
        if "registration token" in message.lower():
            return PushResult.INVALID_TOKEN
    return PushResult.FAILED


class FcmSender:
    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        timeout_s: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._project = credentials["project_id"]
        self._client_email = credentials["client_email"]
        self._private_key = credentials["private_key"]
        self._token_uri = credentials.get(
            "token_uri", "https://oauth2.googleapis.com/token"
        )
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_file(cls, path: str, *, timeout_s: float = 5) -> "FcmSender":
        return cls(json.loads(Path(path).read_text()), timeout_s=timeout_s)

    async def _token(self, *, renew: bool = False) -> str:
        async with self._lock:
            now = time.time()
            if (
                not renew
                and self._access_token
                and now < self._expires_at - _TOKEN_MARGIN_S
            ):
                return self._access_token
            assertion = jwt.encode(
                {
                    "iss": self._client_email,
                    "scope": _SCOPE,
                    "aud": self._token_uri,
                    "iat": int(now),
                    "exp": int(now) + 3600,
                },
                self._private_key,
                algorithm="RS256",
            )
            response = await self._client.post(
                self._token_uri,
                data={"grant_type": _GRANT_TYPE, "assertion": assertion},
            )
            response.raise_for_status()
            body = response.json()
            self._access_token = body["access_token"]
            self._expires_at = now + float(body.get("expires_in", 3600))
            return self._access_token

    async def send(
        self, token: str, title: str, body: str, data: dict[str, str]
    ) -> PushResult:
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": data,
                "android": {"priority": "high"},
                "apns": {"headers": {"apns-priority": "10"}},
            }
        }
        url = _SEND_URL.format(project=self._project)
        try:
            response = await self._client.post(
                url,
                json=message,
                headers={"Authorization": f"Bearer {await self._token()}"},
            )
            if response.status_code == 401:
                # Revoked or expired early: sign in again once.
                access = await self._token(renew=True)
                response = await self._client.post(
                    url, json=message, headers={"Authorization": f"Bearer {access}"}
                )
        except (httpx.HTTPError, KeyError, ValueError):
            logger.exception("FCM request failed")
            return PushResult.FAILED
        result = _classify(response)
        if result is PushResult.FAILED:
            logger.warning(
                "FCM refused a push: %s %s", response.status_code, response.text[:300]
            )
        return result


_fcm: FcmSender | None = None


def fcm() -> FcmSender:
    """The shared sender, created on first use from FCM_CREDENTIALS_FILE."""
    global _fcm
    if _fcm is None:
        _fcm = FcmSender.from_file(
            settings.fcm_credentials_file, timeout_s=settings.push_timeout_s
        )
    return _fcm
