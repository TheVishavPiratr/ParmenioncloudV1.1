"""File storage shared by modules that keep uploaded files (media, gallery).

Local disk under MEDIA_STORAGE_DIR for now. Callers only use save / path /
read_head / delete with a storage key, so moving to S3-compatible object
storage later only changes this file.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from api.core.config import settings

root = Path(settings.media_storage_dir)


class TooLargeError(Exception):
    """More bytes arrived than allowed."""


class MismatchError(Exception):
    """The upload ended short, or its SHA-256 is not the expected one."""


def path(key: str) -> Path:
    return root / key


async def save(
    key: str,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    size: int | None = None,
    sha256: str | None = None,
) -> tuple[int, str]:
    """Stream an upload to disk. Returns (size, SHA-256 hex).

    If `size` or `sha256` is given, the upload must match it. The file is put
    in place only when complete, so a failed or concurrent upload never leaves
    a partial file under the final name.
    """
    final = anyio.Path(path(key))
    await final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(f"{final.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    received = 0
    try:
        async with await anyio.open_file(temp, "wb") as file:
            async for chunk in chunks:
                received += len(chunk)
                if received > max_bytes:
                    raise TooLargeError
                digest.update(chunk)
                await file.write(chunk)
        if (size is not None and received != size) or (
            sha256 is not None and digest.hexdigest() != sha256
        ):
            raise MismatchError
        await temp.replace(final)
    except BaseException:
        await temp.unlink(missing_ok=True)
        raise
    return received, digest.hexdigest()


async def read_head(key: str, length: int) -> bytes:
    """The first `length` bytes of a stored file, e.g. to check its real type."""
    async with await anyio.open_file(path(key), "rb") as file:
        return await file.read(length)


async def delete(key: str) -> None:
    await anyio.Path(path(key)).unlink(missing_ok=True)
