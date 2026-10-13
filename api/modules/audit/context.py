"""Who is on the other end of the current request, for audit entries.

RequestContextMiddleware stores the client's IP and user agent for each HTTP
request and WebSocket, so audit.service.record can add them without every
service having to pass them along.
"""

from contextvars import ContextVar
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

_MAX_USER_AGENT = 255


@dataclass(frozen=True)
class RequestContext:
    ip: str | None = None
    user_agent: str | None = None


_EMPTY = RequestContext()
_current: ContextVar[RequestContext | None] = ContextVar(
    "audit_request_context", default=None
)


def current() -> RequestContext:
    """The current request's context; empty outside a request, e.g. in a
    background job."""
    return _current.get() or _EMPTY


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        user_agent = None
        for name, value in scope.get("headers", []):
            if name == b"user-agent":
                user_agent = value.decode("latin-1")[:_MAX_USER_AGENT] or None
                break
        token = _current.set(
            RequestContext(ip=client[0] if client else None, user_agent=user_agent)
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
