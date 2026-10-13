"""FastAPI application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.account.router import router as account_router
from api.modules.audit import service as audit_service
from api.modules.audit.context import RequestContextMiddleware
from api.modules.audit.router import router as audit_router
from api.modules.auth.router import router as auth_router
from api.modules.cameras.router import agent_router as camera_agent_router
from api.modules.cameras.router import user_router as camera_user_router
from api.modules.device_link.router import router as device_router
from api.modules.edge_agent.router import agent_router, user_router
from api.modules.events import service as events_service
from api.modules.events.router import agent_router as events_agent_router
from api.modules.events.router import user_router as events_user_router
from api.modules.gallery.router import agent_router as gallery_agent_router
from api.modules.gallery.router import user_router as gallery_user_router
from api.modules.media_access import service as media_service
from api.modules.media_access.router import agent_router as media_agent_router
from api.modules.media_access.router import user_router as media_user_router
from api.modules.notifications import service as notifications_service
from api.modules.notifications.router import router as notifications_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    loops = [
        asyncio.create_task(media_service.run_retention_loop()),
        asyncio.create_task(audit_service.run_retention_loop()),
        asyncio.create_task(notifications_service.run_retention_loop()),
        asyncio.create_task(events_service.run_retention_loop()),
    ]
    yield
    for loop in loops:
        loop.cancel()


app = FastAPI(title="VisionPulse Cloud", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(device_router)
app.include_router(user_router)
app.include_router(agent_router)
app.include_router(camera_user_router)
app.include_router(camera_agent_router)
app.include_router(media_user_router)
app.include_router(media_agent_router)
app.include_router(gallery_user_router)
app.include_router(gallery_agent_router)
app.include_router(audit_router)
app.include_router(notifications_router)
app.include_router(events_user_router)
app.include_router(events_agent_router)


@app.exception_handler(RequestValidationError)
async def validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """The default 422 echoes each bad input back. JSON cannot carry NaN or
    Infinity, so for such input it crashed with a 500; drop the echo then."""
    try:
        return await request_validation_exception_handler(request, exc)
    except ValueError:
        errors = [
            {key: value for key, value in error.items() if key != "input"}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content={"detail": jsonable_encoder(errors)}
        )


@app.get("/health")
async def health(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, str]:
    await db.execute(select(1))
    return {"status": "ok", "database": "connected"}
