"""Pydantic request/response schemas for the gallery module."""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from api.modules.gallery.models import PersonCategory

# Single-line text: no control characters. PostgreSQL also rejects NUL (\x00)
# in text columns outright.
_NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]*$"

PersonName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=_NO_CONTROL_CHARS
    ),
]
# Free text: may span lines, but never contain NUL.
Notes = Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\x00]*$")]
ModelName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
]

# ---------- user-facing (/gallery) ----------


class PersonCreateRequest(BaseModel):
    name: PersonName
    category: PersonCategory
    notes: Notes | None = None
    # Face photos are biometric data: the owner must confirm the person agreed.
    consent_confirmed: Literal[True]


class PersonUpdateRequest(BaseModel):
    """Only the fields sent are changed. notes null clears it."""

    name: PersonName | None = None
    category: PersonCategory | None = None
    notes: Notes | None = None

    @model_validator(mode="after")
    def _required_fields_not_null(self) -> Self:
        for field in ("name", "category"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class FaceResponse(BaseModel):
    id: uuid.UUID
    person_id: uuid.UUID
    status: str
    reject_reason: str | None
    content_type: str
    size_bytes: int
    created_at: datetime


class PersonResponse(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    notes: str | None
    faces: list[FaceResponse]
    created_at: datetime


class FaceLinkResponse(BaseModel):
    # Relative to the API base URL; works in an <img> tag until expires_at.
    url: str
    expires_at: datetime


# ---------- agent-facing (/agent/gallery) ----------


class AgentFace(BaseModel):
    id: uuid.UUID
    # Set when some agent already computed it for the requested model.
    embedding: list[float] | None
    # Set when it still has to be computed: GET it with the agent token.
    image_url: str | None


class AgentPerson(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    faces: list[AgentFace]


class AgentGalleryResponse(BaseModel):
    config_version: int
    model: str
    people: list[AgentPerson]


Vector = Annotated[
    list[Annotated[float, Field(allow_inf_nan=False)]],
    Field(min_length=64, max_length=2048),
]


class EmbeddingResult(BaseModel):
    """Either a vector, or the reason no face could be used."""

    face_id: uuid.UUID
    vector: Vector | None = None
    rejected_reason: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=200, pattern=_NO_CONTROL_CHARS),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.vector is None) == (self.rejected_reason is None):
            raise ValueError("send either vector or rejected_reason")
        return self


class EmbeddingReport(BaseModel):
    model: ModelName
    results: list[EmbeddingResult] = Field(min_length=1, max_length=50)


class EmbeddingReportResult(BaseModel):
    stored: int
    rejected: int
    # Unknown faces, other owners' faces, or ones that already had a result.
    skipped: int
