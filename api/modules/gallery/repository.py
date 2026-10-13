"""Database access for the gallery module."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.modules.gallery.models import Face, FaceEmbedding, Person


async def commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise


def stage(db: AsyncSession, row: Person | Face | FaceEmbedding) -> None:
    """Add a row to the session without committing."""
    db.add(row)


# ---------- people ----------


async def add_person(db: AsyncSession, person: Person) -> Person:
    db.add(person)
    await commit(db)
    return person


async def get_person(
    db: AsyncSession, owner_id: uuid.UUID, person_id: uuid.UUID
) -> Person | None:
    return await db.scalar(
        select(Person).where(Person.id == person_id, Person.owner_id == owner_id)
    )


async def list_people(
    db: AsyncSession, owner_id: uuid.UUID, category: str | None = None
) -> list[Person]:
    query = select(Person).where(Person.owner_id == owner_id)
    if category is not None:
        query = query.where(Person.category == category)
    result = await db.scalars(query.order_by(Person.name, Person.id))
    return list(result)


async def list_people_by_ids(
    db: AsyncSession, owner_id: uuid.UUID, person_ids: list[uuid.UUID]
) -> list[Person]:
    result = await db.scalars(
        select(Person).where(Person.owner_id == owner_id, Person.id.in_(person_ids))
    )
    return list(result)


async def count_people(db: AsyncSession, owner_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(Person).where(Person.owner_id == owner_id)
    )


async def delete_person(db: AsyncSession, person_id: uuid.UUID) -> None:
    """Delete a person; faces and embeddings go with them (ON DELETE CASCADE)."""
    await db.execute(
        delete(Person)
        .where(Person.id == person_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


async def delete_people_of_owner(db: AsyncSession, owner_id: uuid.UUID) -> list[str]:
    """Delete all the owner's people (faces and embeddings cascade) and return
    the storage keys of their photos."""
    keys = list(
        await db.scalars(select(Face.storage_key).where(Face.owner_id == owner_id))
    )
    await db.execute(
        delete(Person)
        .where(Person.owner_id == owner_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)
    return keys


# ---------- faces ----------


async def add_face(db: AsyncSession, face: Face) -> Face:
    db.add(face)
    await commit(db)
    return face


async def get_face(
    db: AsyncSession, owner_id: uuid.UUID, face_id: uuid.UUID
) -> Face | None:
    return await db.scalar(
        select(Face).where(Face.id == face_id, Face.owner_id == owner_id)
    )


async def get_face_by_id(db: AsyncSession, face_id: uuid.UUID) -> Face | None:
    return await db.get(Face, face_id)


async def get_face_by_hash(
    db: AsyncSession, person_id: uuid.UUID, sha256: str
) -> Face | None:
    return await db.scalar(
        select(Face).where(Face.person_id == person_id, Face.sha256 == sha256)
    )


async def list_faces(
    db: AsyncSession, owner_id: uuid.UUID, person_ids: list[uuid.UUID] | None = None
) -> list[Face]:
    query = select(Face).where(Face.owner_id == owner_id)
    if person_ids is not None:
        query = query.where(Face.person_id.in_(person_ids))
    result = await db.scalars(query.order_by(Face.created_at, Face.id))
    return list(result)


async def list_faces_by_ids(
    db: AsyncSession, owner_id: uuid.UUID, face_ids: list[uuid.UUID]
) -> list[Face]:
    result = await db.scalars(
        select(Face).where(Face.owner_id == owner_id, Face.id.in_(face_ids))
    )
    return list(result)


async def list_storage_keys(db: AsyncSession, person_id: uuid.UUID) -> list[str]:
    result = await db.scalars(
        select(Face.storage_key).where(Face.person_id == person_id)
    )
    return list(result)


async def count_faces(db: AsyncSession, person_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count()).select_from(Face).where(Face.person_id == person_id)
    )


async def delete_face(db: AsyncSession, face_id: uuid.UUID) -> None:
    await db.execute(
        delete(Face)
        .where(Face.id == face_id)
        .execution_options(synchronize_session="fetch")
    )
    await commit(db)


# ---------- embeddings ----------


async def embeddings_for_model(
    db: AsyncSession, owner_id: uuid.UUID, model: str
) -> dict[uuid.UUID, list[float]]:
    """face_id -> vector, for every face of the owner that has one for `model`."""
    rows = await db.execute(
        select(FaceEmbedding.face_id, FaceEmbedding.vector)
        .join(Face, Face.id == FaceEmbedding.face_id)
        .where(Face.owner_id == owner_id, FaceEmbedding.model == model)
    )
    return {row.face_id: row.vector for row in rows}


async def face_ids_with_embedding(
    db: AsyncSession, face_ids: list[uuid.UUID], model: str
) -> set[uuid.UUID]:
    result = await db.scalars(
        select(FaceEmbedding.face_id).where(
            FaceEmbedding.face_id.in_(face_ids), FaceEmbedding.model == model
        )
    )
    return set(result)


async def model_dimension(
    db: AsyncSession, owner_id: uuid.UUID, model: str
) -> int | None:
    """Length of the owner's stored vectors for `model`, if any are stored."""
    vector = await db.scalar(
        select(FaceEmbedding.vector)
        .join(Face, Face.id == FaceEmbedding.face_id)
        .where(Face.owner_id == owner_id, FaceEmbedding.model == model)
        .limit(1)
    )
    return len(vector) if vector is not None else None
