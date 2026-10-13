"""HTTP endpoints for the account module."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.account import service
from api.modules.account.schemas import AccountDeleteRequest
from api.modules.auth.dependencies import CurrentUser

router = APIRouter(prefix="/account", tags=["account"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


# POST rather than DELETE with a body: many HTTP clients drop DELETE bodies.
@router.post("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    data: AccountDeleteRequest, user: CurrentUser, db: DbSession
) -> Response:
    """Delete the account and everything in it. Cannot be undone. Every
    login token, device and edge box of the account stops working at once."""
    try:
        await service.delete_account(db, user, data.password)
    except service.WrongPasswordError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password"
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
