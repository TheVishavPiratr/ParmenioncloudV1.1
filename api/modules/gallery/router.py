"""HTTP endpoints for the gallery module.

Three audiences:
- user_router  (/gallery):       people managing who is recognised, by user JWT.
- user_router  (/gallery/files): anyone holding a signed photo link, no login
                                 header, so an <img> tag can load it directly.
- agent_router (/agent/gallery): the Edge Agent syncing the gallery, by agent token.
"""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.database import get_db
from api.modules.auth.dependencies import CurrentUser
from api.modules.edge_agent.dependencies import CurrentAgent
from api.modules.gallery import service
from api.modules.gallery.models import Face, Person, PersonCategory
from api.modules.gallery.schemas import (
    AgentGalleryResponse,
    EmbeddingReport,
    EmbeddingReportResult,
    FaceLinkResponse,
    FaceResponse,
    ModelName,
    PersonCreateRequest,
    PersonResponse,
    PersonUpdateRequest,
)

user_router = APIRouter(prefix="/gallery", tags=["gallery"])
agent_router = APIRouter(prefix="/agent/gallery", tags=["agent"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_ERRORS: dict[type[service.GalleryError], tuple[int, str]] = {
    service.PersonNotFoundError: (status.HTTP_404_NOT_FOUND, "Person not found"),
    service.FaceNotFoundError: (status.HTTP_404_NOT_FOUND, "Photo not found"),
    service.PeopleLimitError: (
        status.HTTP_409_CONFLICT,
        f"The gallery can hold at most {service.MAX_PEOPLE_PER_OWNER} people",
    ),
    service.FaceLimitError: (
        status.HTTP_409_CONFLICT,
        f"A person can have at most {service.MAX_FACES_PER_PERSON} photos",
    ),
    service.PhotoTooLargeError: (
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"A photo can be at most {service.MAX_PHOTO_BYTES // (1024 * 1024)} MB",
    ),
    service.UnsupportedImageError: (
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "The photo must be a JPEG, PNG or WebP image",
    ),
    # One answer for bad, expired and forged links.
    service.InvalidLinkError: (status.HTTP_403_FORBIDDEN, "Link is invalid or expired"),
    service.DimensionMismatchError: (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "All vectors of one model must have the same length",
    ),
    service.EmbeddingConflictError: (
        status.HTTP_409_CONFLICT,
        "Another agent stored these results at the same time; send them again",
    ),
}


def _http_error(exc: service.GalleryError) -> HTTPException:
    code, detail = _ERRORS[type(exc)]
    return HTTPException(status_code=code, detail=detail)


def _face_response(face: Face) -> FaceResponse:
    return FaceResponse(
        id=face.id,
        person_id=face.person_id,
        status=face.status,
        reject_reason=face.reject_reason,
        content_type=face.content_type,
        size_bytes=face.size_bytes,
        created_at=face.created_at,
    )


def _person_response(person: Person, faces: list[Face]) -> PersonResponse:
    return PersonResponse(
        id=person.id,
        name=person.name,
        category=person.category,
        notes=person.notes,
        faces=[_face_response(face) for face in faces],
        created_at=person.created_at,
    )


def _image_response(path: Path, content_type: str) -> FileResponse:
    if not path.is_file():
        raise _http_error(service.FaceNotFoundError())
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------- user-facing: people ----------


@user_router.get("/people")
async def list_people(
    user: CurrentUser, db: DbSession, category: PersonCategory | None = None
) -> list[PersonResponse]:
    pairs = await service.list_people(db, user.id, category)
    return [_person_response(person, faces) for person, faces in pairs]


@user_router.post("/people", status_code=status.HTTP_201_CREATED)
async def create_person(
    data: PersonCreateRequest, user: CurrentUser, db: DbSession
) -> PersonResponse:
    """Requires consent_confirmed: true, since face photos are biometric data."""
    try:
        person = await service.create_person(db, user.id, data)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _person_response(person, [])


@user_router.get("/people/{person_id}")
async def get_person(
    person_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> PersonResponse:
    try:
        person, faces = await service.get_person_with_faces(db, user.id, person_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _person_response(person, faces)


@user_router.patch("/people/{person_id}")
async def update_person(
    person_id: uuid.UUID, data: PersonUpdateRequest, user: CurrentUser, db: DbSession
) -> PersonResponse:
    try:
        await service.update_person(db, user.id, person_id, data)
        person, faces = await service.get_person_with_faces(db, user.id, person_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _person_response(person, faces)


@user_router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_person(
    person_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Response:
    """Deletes the person with every photo and embedding, permanently."""
    try:
        await service.delete_person(db, user.id, person_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- user-facing: photos ----------


@user_router.post("/people/{person_id}/faces", status_code=status.HTTP_201_CREATED)
async def add_face(
    person_id: uuid.UUID, request: Request, user: CurrentUser, db: DbSession
) -> FaceResponse:
    """Send the photo as the raw request body (no multipart). Status stays
    "pending" until an agent has computed its embedding."""
    try:
        face = await service.add_face(db, user.id, person_id, request.stream())
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _face_response(face)


@user_router.delete("/faces/{face_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_face(face_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    try:
        await service.delete_face(db, user.id, face_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@user_router.post("/faces/{face_id}/link")
async def create_face_link(
    face_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> FaceLinkResponse:
    try:
        url, expires_at = await service.create_face_link(db, user.id, face_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return FaceLinkResponse(url=url, expires_at=expires_at)


@user_router.get("/files/{face_id}")
async def open_face_file(
    face_id: uuid.UUID,
    db: DbSession,
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=64)],
) -> FileResponse:
    try:
        path, content_type = await service.open_face_link(db, face_id, exp, sig)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _image_response(path, content_type)


# ---------- agent-facing ----------


@agent_router.get("")
async def agent_gallery(
    agent: CurrentAgent, db: DbSession, model: Annotated[ModelName, Query()]
) -> AgentGalleryResponse:
    """Everyone to recognise, for the agent's face model. Fetch on connect and
    after every config_changed command (it may be a gallery change)."""
    return await service.build_agent_gallery(db, agent, model)


@agent_router.get("/faces/{face_id}/image")
async def agent_face_image(
    face_id: uuid.UUID, agent: CurrentAgent, db: DbSession
) -> FileResponse:
    """A photo whose embedding the agent still has to compute."""
    try:
        path, content_type = await service.agent_face_image(db, agent, face_id)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
    return _image_response(path, content_type)


@agent_router.post("/embeddings")
async def report_embeddings(
    data: EmbeddingReport, agent: CurrentAgent, db: DbSession
) -> EmbeddingReportResult:
    """Vectors computed from gallery photos, or why a photo has no usable face.
    They are shared with the owner's other agents running the same model."""
    try:
        return await service.record_embeddings(db, agent, data)
    except service.GalleryError as exc:
        raise _http_error(exc) from None
