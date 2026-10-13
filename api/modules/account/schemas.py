"""Pydantic request/response schemas for the account module."""

from pydantic import BaseModel, Field


class AccountDeleteRequest(BaseModel):
    # Asked again even though the user is logged in: a borrowed, unlocked
    # phone must not be enough to wipe someone's account.
    password: str = Field(min_length=1, max_length=128)
