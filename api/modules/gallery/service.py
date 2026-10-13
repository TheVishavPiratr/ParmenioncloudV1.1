"""Business logic for the gallery module.

The cloud keeps the people and their photos, checks every upload, and sends
the gallery to all the owner's agents. Embeddings are computed once: the first
agent running a given face model computes a photo's vector, and the cloud
hands that vector to every other agent of the owner with the same model.

Whenever the gallery changes, every enrolled agent of the owner gets a
config_changed command and re-fetches GET /agent/gallery.

Other modules may call: get_person, get_people, delete_all_for_owner.
"""

import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import signed_links, storage
from api.modules.audit import service as audit_service
from api.modules.edge_agent import service as edge_agent_service
from api.modules.gallery import repository
from api.modules.gallery.models import (
    Face,
    FaceEmbedding,
    FaceStatus,
    Person,
    PersonCategory,
)
from api.modules.gallery.schemas import (
    AgentFace,
    AgentGalleryResponse,
    AgentPerson,
    EmbeddingReport,
    EmbeddingReportResult,
    PersonCreateRequest,
    PersonUpdateRequest,
)

if TYPE_CHECKING:
    from api.modules.edge_agent.models import EdgeAgent

AuditAction = audit_service.AuditAction

MAX_PEOPLE_PER_OWNER = 500
MAX_FACES_PER_PERSON = 10
MAX_PHOTO_BYTES = 5 * 1024 * 1024
LINK_TTL = timedelta(minutes=10)
LINK_PURPOSE = "gallery"
# Bytes needed to recognise every accepted image format.
_SNIFF_BYTES = 12


class GalleryError(Exception):
    """Base of every error this service raises."""


class PersonNotFoundError(GalleryError):
    pass


class FaceNotFoundError(GalleryError):
    pass


class PeopleLimitError(GalleryError):
    pass


class FaceLimitError(GalleryError):
    pass


class PhotoTooLargeError(GalleryError):
    pass


class UnsupportedImageError(GalleryError):
    pass


class InvalidLinkError(GalleryError):
    pass


class DimensionMismatchError(GalleryError):
    """Vectors of one model must all have the same length."""


class EmbeddingConflictError(GalleryError):
    """Another agent stored the same embedding at the same moment."""


def _sniff(head: bytes) -> str | None:
    """The image type from the file's first bytes; the client's claim is ignored."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _storage_key(face_id: uuid.UUID) -> str:
    return f"gallery/{face_id.hex[:2]}/{face_id.hex}"


async def _audit(
    db: AsyncSession,
    action: AuditAction,
    owner_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    await audit_service.record(
        db,
        action,
        user_id=owner_id,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )


async def _notify_agents(
    db: AsyncSession, owner_id: uuid.UUID, except_agent_id: uuid.UUID | None = None
) -> None:
    for agent in await edge_agent_service.list_agents(db, owner_id):
        if edge_agent_service.is_enrolled(agent) and agent.id != except_agent_id:
            await edge_agent_service.notify_config_changed(db, agent.id)


async def _with_faces(
    db: AsyncSession, owner_id: uuid.UUID, people: list[Person]
) -> list[tuple[Person, list[Face]]]:
    faces: dict[uuid.UUID, list[Face]] = defaultdict(list)
    if people:
        for face in await repository.list_faces(db, owner_id, [p.id for p in people]):
            faces[face.person_id].append(face)
    return [(person, faces[person.id]) for person in people]


# ---------- people ----------


async def get_person(
    db: AsyncSession, owner_id: uuid.UUID, person_id: uuid.UUID
) -> Person:
    person = await repository.get_person(db, owner_id, person_id)
    if person is None:
        raise PersonNotFoundError
    return person


async def get_people(
    db: AsyncSession, owner_id: uuid.UUID, person_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Person]:
    """The owner's people among person_ids, by id. Unknown ids are left out."""
    if not person_ids:
        return {}
    people = await repository.list_people_by_ids(db, owner_id, person_ids)
    return {person.id: person for person in people}


async def get_person_with_faces(
    db: AsyncSession, owner_id: uuid.UUID, person_id: uuid.UUID
) -> tuple[Person, list[Face]]:
    person = await get_person(db, owner_id, person_id)
    [pair] = await _with_faces(db, owner_id, [person])
    return pair


async def list_people(
    db: AsyncSession, owner_id: uuid.UUID, category: PersonCategory | None = None
) -> list[tuple[Person, list[Face]]]:
    people = await repository.list_people(db, owner_id, category)
    return await _with_faces(db, owner_id, people)


async def create_person(
    db: AsyncSession, owner_id: uuid.UUID, data: PersonCreateRequest
) -> Person:
    """A person without photos changes nothing for the agents yet."""
    if await repository.count_people(db, owner_id) >= MAX_PEOPLE_PER_OWNER:
        raise PeopleLimitError
    person = Person(
        owner_id=owner_id,
        name=data.name,
        category=data.category,
        notes=data.notes,
        consent_confirmed_at=datetime.now(UTC),
    )
    person = await repository.add_person(db, person)
    await _audit(db, AuditAction.PERSON_CREATED, owner_id, "person", person.id)
    return person


async def update_person(
    db: AsyncSession,
    owner_id: uuid.UUID,
    person_id: uuid.UUID,
    data: PersonUpdateRequest,
) -> Person:
    person = await get_person(db, owner_id, person_id)
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(person, field, value)
    await repository.commit(db)
    await _audit(
        db,
        AuditAction.PERSON_UPDATED,
        owner_id,
        "person",
        person.id,
        {"fields": sorted(changes)},
    )
    # Agents know people by name and category; notes stay in the cloud.
    if {"name", "category"} & changes.keys() and await repository.count_faces(
        db, person.id
    ):
        await _notify_agents(db, owner_id)
    return person


async def delete_person(
    db: AsyncSession, owner_id: uuid.UUID, person_id: uuid.UUID
) -> None:
    """Remove the person, every photo file and every embedding, for good."""
    person = await get_person(db, owner_id, person_id)
    keys = await repository.list_storage_keys(db, person.id)
    await repository.delete_person(db, person.id)
    await _audit(
        db,
        AuditAction.PERSON_DELETED,
        owner_id,
        "person",
        person_id,
        {"faces_deleted": len(keys)},
    )
    for key in keys:
        await storage.delete(key)
    if keys:
        await _notify_agents(db, owner_id)


# ---------- faces ----------


async def delete_all_for_owner(db: AsyncSession, owner_id: uuid.UUID) -> int:
    """Remove every person, photo file and embedding of a user whose account
    is being deleted. Agents are not told: the account's agents are already
    gone. Returns how many photos were removed."""
    keys = await repository.delete_people_of_owner(db, owner_id)
    for key in keys:
        await storage.delete(key)
    return len(keys)


async def add_face(
    db: AsyncSession,
    owner_id: uuid.UUID,
    person_id: uuid.UUID,
    chunks: AsyncIterator[bytes],
) -> Face:
    """Store a reference photo sent as the raw request body.

    The type is read from the file itself (JPEG, PNG or WebP). Sending the same
    photo again for the same person returns the stored one.
    """
    person = await get_person(db, owner_id, person_id)
    if await repository.count_faces(db, person.id) >= MAX_FACES_PER_PERSON:
        raise FaceLimitError
    # Give the database connection back while the photo streams in.
    await repository.commit(db)

    face_id = uuid.uuid4()
    key = _storage_key(face_id)
    try:
        size, sha256 = await storage.save(key, chunks, max_bytes=MAX_PHOTO_BYTES)
    except storage.TooLargeError:
        raise PhotoTooLargeError from None
    content_type = _sniff(await storage.read_head(key, _SNIFF_BYTES))
    if content_type is None:
        await storage.delete(key)
        raise UnsupportedImageError

    existing = await repository.get_face_by_hash(db, person.id, sha256)
    if existing is not None:
        await storage.delete(key)
        return existing
    face = Face(
        id=face_id,
        person_id=person.id,
        owner_id=owner_id,
        status=FaceStatus.PENDING,
        content_type=content_type,
        size_bytes=size,
        sha256=sha256,
        storage_key=key,
    )
    try:
        await repository.add_face(db, face)
    except IntegrityError:
        # The same photo raced in from a retry; keep the first copy.
        await storage.delete(key)
        existing = await repository.get_face_by_hash(db, person.id, sha256)
        if existing is None:
            raise
        return existing
    await _audit(
        db,
        AuditAction.FACE_ADDED,
        owner_id,
        "face",
        face.id,
        {"person_id": str(person.id)},
    )
    await _notify_agents(db, owner_id)
    return face


async def delete_face(
    db: AsyncSession, owner_id: uuid.UUID, face_id: uuid.UUID
) -> None:
    face = await repository.get_face(db, owner_id, face_id)
    if face is None:
        raise FaceNotFoundError
    await repository.delete_face(db, face.id)
    await _audit(
        db,
        AuditAction.FACE_DELETED,
        owner_id,
        "face",
        face_id,
        {"person_id": str(face.person_id)},
    )
    await storage.delete(face.storage_key)
    await _notify_agents(db, owner_id)


async def create_face_link(
    db: AsyncSession, owner_id: uuid.UUID, face_id: uuid.UUID
) -> tuple[str, datetime]:
    """A short-lived signed URL for viewing the photo. Returns (url, expires_at)."""
    face = await repository.get_face(db, owner_id, face_id)
    if face is None:
        raise FaceNotFoundError
    link = signed_links.create(
        LINK_PURPOSE, f"/gallery/files/{face.id}", face.id, LINK_TTL
    )
    await audit_service.record(
        db,
        AuditAction.FACE_LINK_CREATED,
        user_id=owner_id,
        target_type="face",
        target_id=face.id,
        # One entry per viewing, not one per reload.
        dedupe_within=LINK_TTL,
    )
    return link


async def open_face_link(
    db: AsyncSession, face_id: uuid.UUID, expires: int, signature: str
) -> tuple[Path, str]:
    """Check a signed URL before touching the database. Returns (path, type)."""
    if not signed_links.verify(LINK_PURPOSE, face_id, expires, signature):
        raise InvalidLinkError
    face = await repository.get_face_by_id(db, face_id)
    if face is None:
        raise FaceNotFoundError
    return storage.path(face.storage_key), face.content_type


# ---------- agent actions ----------


async def build_agent_gallery(
    db: AsyncSession, agent: "EdgeAgent", model: str
) -> AgentGalleryResponse:
    """Everyone the agent should recognise. Photos that already have a vector
    for this model come with it; the rest come with a link to compute it from.
    Rejected photos are left out."""
    people = await repository.list_people(db, agent.owner_id)
    faces: dict[uuid.UUID, list[Face]] = defaultdict(list)
    for face in await repository.list_faces(db, agent.owner_id):
        if face.status != FaceStatus.REJECTED:
            faces[face.person_id].append(face)
    vectors = await repository.embeddings_for_model(db, agent.owner_id, model)

    return AgentGalleryResponse(
        config_version=agent.config_version,
        model=model,
        people=[
            AgentPerson(
                id=person.id,
                name=person.name,
                category=person.category,
                faces=[
                    AgentFace(
                        id=face.id,
                        embedding=vectors.get(face.id),
                        image_url=None
                        if face.id in vectors
                        else f"/agent/gallery/faces/{face.id}/image",
                    )
                    for face in faces[person.id]
                ],
            )
            for person in people
        ],
    )


async def agent_face_image(
    db: AsyncSession, agent: "EdgeAgent", face_id: uuid.UUID
) -> tuple[Path, str]:
    face = await repository.get_face(db, agent.owner_id, face_id)
    if face is None:
        raise FaceNotFoundError
    return storage.path(face.storage_key), face.content_type


async def record_embeddings(
    db: AsyncSession, agent: "EdgeAgent", report: EmbeddingReport
) -> EmbeddingReportResult:
    """Store vectors (or "no face found") computed by the agent.

    The first result for a photo and model wins; later ones are skipped. A
    rejection only applies to a photo nobody has found a face in yet.
    """
    latest = {result.face_id: result for result in report.results}  # last wins
    dimensions = {len(r.vector) for r in latest.values() if r.vector is not None}
    stored_dimension = await repository.model_dimension(
        db, agent.owner_id, report.model
    )
    if stored_dimension is not None:
        dimensions.add(stored_dimension)
    if len(dimensions) > 1:
        raise DimensionMismatchError

    face_ids = list(latest)
    faces = {
        f.id: f
        for f in await repository.list_faces_by_ids(db, agent.owner_id, face_ids)
    }
    done = await repository.face_ids_with_embedding(db, face_ids, report.model)
    stored = rejected = skipped = 0

    for face_id, result in latest.items():
        face = faces.get(face_id)
        if face is None or face.status == FaceStatus.REJECTED:
            skipped += 1
        elif result.vector is not None:
            if face_id in done:
                skipped += 1
                continue
            repository.stage(
                db,
                FaceEmbedding(
                    face_id=face_id,
                    model=report.model,
                    vector=result.vector,
                    computed_by_agent_id=agent.id,
                ),
            )
            face.status = FaceStatus.READY
            stored += 1
        elif face.status == FaceStatus.PENDING:
            face.status = FaceStatus.REJECTED
            face.reject_reason = result.rejected_reason
            rejected += 1
        else:
            skipped += 1

    try:
        await repository.commit(db)
    except IntegrityError:
        raise EmbeddingConflictError from None
    if stored or rejected:
        # Other agents can now reuse the vectors instead of computing them.
        await _notify_agents(db, agent.owner_id, except_agent_id=agent.id)
    return EmbeddingReportResult(stored=stored, rejected=rejected, skipped=skipped)
