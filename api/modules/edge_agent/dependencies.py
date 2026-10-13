"""FastAPI dependencies of the edge_agent module, for use by any module's router."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.edge_agent import service
from api.modules.edge_agent.models import EdgeAgent

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_agent(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EdgeAgent:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid agent token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        return await service.get_agent_from_token(db, credentials.credentials)
    except service.InvalidAgentTokenError:
        raise unauthorized from None


CurrentAgent = Annotated[EdgeAgent, Depends(get_current_agent)]
