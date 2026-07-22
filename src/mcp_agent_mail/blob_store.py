"""Durable, Git-independent content-addressed object storage."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Blob, BlobReference

_CHUNK_SIZE: Final = 1024 * 1024


class BlobStoreError(RuntimeError):
    """Base class for content-addressed storage failures."""


class BlobTooLargeError(BlobStoreError):
    """Raised before an oversized object can be installed."""


class BlobCorruptionError(BlobStoreError):
    """Raised when bytes at a digest path do not match that digest."""


class BlobMetadataConflictError(BlobStoreError):
    """Raised when durable bytes disagree with existing database metadata."""


@dataclass(frozen=True, slots=True)
class InstalledBlob:
    digest: str
    byte_length: int
    storage_key: str
    path: Path
    reused: bool


@dataclass(slots=True)
class BlobInstallation:
    blob: InstalledBlob
    _lease_path: Path
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        await asyncio.to_thread(self._release_sync)

    def _release_sync(self) -> None:
        try:
            self._lease_path.unlink(missing_ok=True)
            _fsync_directory(self._lease_path.parent)
        finally:
            self._released = True

    async def __aenter__(self) -> InstalledBlob:
        return self.blob

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()


@dataclass(slots=True)
class BlobProtectionLease:
    """Pins a set of digests against garbage collection."""

    _path: Path
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        await asyncio.to_thread(self._release_sync)

    def _release_sync(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            _fsync_directory(self._path.parent)
        finally:
            self._released = True

    async def __aenter__(self) -> BlobProtectionLease:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
            byte_length += len(chunk)
    return digest.hexdigest(), byte_length


class BlobStore:
    """Filesystem object store with atomic installation and GC-visible leases."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.objects_root = self.root / "objects" / "sha256"
        self.temp_root = self.root / ".tmp"
        self.install_lease_root = self.root / "leases" / "install"
        self.snapshot_lease_root = self.root / "leases" / "snapshot"
        self.quarantine_root = self.root / "quarantine"

    async def install_bytes(
        self,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> BlobInstallation:
        return await asyncio.to_thread(self._install_chunks_sync, (data,), max_bytes)

    async def install_file(
        self,
        source: str | Path,
        *,
        max_bytes: int | None = None,
    ) -> BlobInstallation:
        source_path = Path(source)

        def chunks() -> Iterable[bytes]:
            with source_path.open("rb") as stream:
                while chunk := stream.read(_CHUNK_SIZE):
                    yield chunk

        return await asyncio.to_thread(self._install_chunks_sync, chunks(), max_bytes)

    def _ensure_layout(self) -> None:
        for directory in (
            self.objects_root,
            self.temp_root,
            self.install_lease_root,
            self.snapshot_lease_root,
            self.quarantine_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _install_chunks_sync(
        self,
        chunks: Iterable[bytes],
        max_bytes: int | None,
    ) -> BlobInstallation:
        self._ensure_layout()
        digest = hashlib.sha256()
        byte_length = 0
        temp_fd, temp_name = tempfile.mkstemp(prefix="install-", dir=self.temp_root)
        temp_path = Path(temp_name)
        lease_path: Path | None = None
        try:
            with os.fdopen(temp_fd, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("blob chunks must be bytes")
                    byte_length += len(chunk)
                    if max_bytes is not None and byte_length > max_bytes:
                        raise BlobTooLargeError(
                            f"blob exceeds maximum size of {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            digest_hex = digest.hexdigest()
            target_dir = self.objects_root / digest_hex[:2]
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / digest_hex
            storage_key = target_path.relative_to(self.root).as_posix()

            lease_path = self.install_lease_root / f"{digest_hex}.{uuid.uuid4().hex}.lease"
            lease_fd = os.open(lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(lease_fd, f"{digest_hex}\n".encode())
                os.fsync(lease_fd)
            finally:
                os.close(lease_fd)
            _fsync_directory(self.install_lease_root)

            reused = False
            try:
                os.link(temp_path, target_path)
                _fsync_directory(target_dir)
            except FileExistsError:
                existing_digest, existing_length = _hash_file(target_path)
                if existing_digest != digest_hex or existing_length != byte_length:
                    quarantine_path = self.quarantine_root / (
                        f"{digest_hex}.{uuid.uuid4().hex}.corrupt"
                    )
                    target_path.replace(quarantine_path)
                    _fsync_directory(target_dir)
                    _fsync_directory(self.quarantine_root)
                    raise BlobCorruptionError(
                        f"existing object at {storage_key} failed digest verification; quarantined"
                    ) from None
                reused = True
            return BlobInstallation(
                blob=InstalledBlob(
                    digest=digest_hex,
                    byte_length=byte_length,
                    storage_key=storage_key,
                    path=target_path,
                    reused=reused,
                ),
                _lease_path=lease_path,
            )
        except BaseException:
            if lease_path is not None:
                lease_path.unlink(missing_ok=True)
                _fsync_directory(self.install_lease_root)
            raise
        finally:
            temp_path.unlink(missing_ok=True)
            _fsync_directory(self.temp_root)

    async def verify(self, digest: str) -> InstalledBlob:
        return await asyncio.to_thread(self._verify_sync, digest)

    def _verify_sync(self, digest: str) -> InstalledBlob:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise BlobStoreError("digest must be 64 lowercase hexadecimal characters")
        path = self.objects_root / digest[:2] / digest
        actual_digest, byte_length = _hash_file(path)
        if actual_digest != digest:
            raise BlobCorruptionError(f"object {digest} failed verification")
        return InstalledBlob(
            digest=digest,
            byte_length=byte_length,
            storage_key=path.relative_to(self.root).as_posix(),
            path=path,
            reused=True,
        )

    async def active_install_digests(self) -> set[str]:
        return await asyncio.to_thread(self._active_install_digests_sync)

    def _active_install_digests_sync(self) -> set[str]:
        if not self.install_lease_root.exists():
            return set()
        return {
            lease.name.split(".", 1)[0]
            for lease in self.install_lease_root.glob("*.lease")
            if len(lease.name.split(".", 1)[0]) == 64
        }

    async def protect_snapshot(self, digests: Iterable[str]) -> BlobProtectionLease:
        """Create a durable lease that prevents collection during a snapshot."""
        digest_set = set(digests)
        for digest in digest_set:
            self._validate_digest(digest)
        return await asyncio.to_thread(self._protect_snapshot_sync, digest_set)

    def _protect_snapshot_sync(self, digests: set[str]) -> BlobProtectionLease:
        self._ensure_layout()
        path = self.snapshot_lease_root / f"{uuid.uuid4().hex}.lease"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            content = "".join(f"{digest}\n" for digest in sorted(digests)).encode()
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.snapshot_lease_root)
        return BlobProtectionLease(path)

    async def protected_digests(self) -> set[str]:
        return await asyncio.to_thread(self._protected_digests_sync)

    def _protected_digests_sync(self) -> set[str]:
        protected = self._active_install_digests_sync()
        if not self.snapshot_lease_root.exists():
            return protected
        for lease in self.snapshot_lease_root.glob("*.lease"):
            for digest in lease.read_text(encoding="utf-8").splitlines():
                if self._is_digest(digest):
                    protected.add(digest)
        return protected

    async def orphan_candidates(
        self,
        referenced_digests: Iterable[str],
        *,
        grace_seconds: float,
        now: float | None = None,
    ) -> list[InstalledBlob]:
        """Return verified, old, unreferenced objects not protected by a lease."""
        return await asyncio.to_thread(
            self._orphan_candidates_sync,
            set(referenced_digests),
            grace_seconds,
            time.time() if now is None else now,
        )

    def _orphan_candidates_sync(
        self,
        referenced_digests: set[str],
        grace_seconds: float,
        now: float,
    ) -> list[InstalledBlob]:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        protected = self._protected_digests_sync()
        candidates: list[InstalledBlob] = []
        if not self.objects_root.exists():
            return candidates
        for path in self.objects_root.glob("*/*"):
            digest = path.name
            if (
                not path.is_file()
                or not self._is_digest(digest)
                or digest in referenced_digests
                or digest in protected
                or now - path.stat().st_mtime < grace_seconds
            ):
                continue
            actual_digest, byte_length = _hash_file(path)
            if actual_digest != digest:
                raise BlobCorruptionError(f"orphan candidate {digest} failed verification")
            candidates.append(
                InstalledBlob(
                    digest=digest,
                    byte_length=byte_length,
                    storage_key=path.relative_to(self.root).as_posix(),
                    path=path,
                    reused=True,
                )
            )
        return sorted(candidates, key=lambda blob: blob.digest)

    async def quarantine_orphans(
        self,
        referenced_digests: Iterable[str],
        *,
        grace_seconds: float,
        dry_run: bool = True,
        now: float | None = None,
    ) -> list[InstalledBlob]:
        """Find or recoverably quarantine eligible orphan objects."""
        candidates = await self.orphan_candidates(
            referenced_digests,
            grace_seconds=grace_seconds,
            now=now,
        )
        if not dry_run:
            await asyncio.to_thread(self._quarantine_candidates_sync, candidates)
        return candidates

    def _quarantine_candidates_sync(self, candidates: list[InstalledBlob]) -> None:
        self._ensure_layout()
        for candidate in candidates:
            quarantine_path = self.quarantine_root / f"{candidate.digest}.gc-{uuid.uuid4().hex}"
            try:
                candidate.path.replace(quarantine_path)
            except FileNotFoundError:
                continue
            _fsync_directory(candidate.path.parent)
            _fsync_directory(self.quarantine_root)

    @staticmethod
    def _is_digest(digest: str) -> bool:
        return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)

    @classmethod
    def _validate_digest(cls, digest: str) -> None:
        if not cls._is_digest(digest):
            raise BlobStoreError("digest must be 64 lowercase hexadecimal characters")


async def add_blob_reference(
    session: AsyncSession,
    installation: BlobInstallation,
    *,
    entity_type: str,
    entity_id: str,
    role: str,
    media_type: str = "application/octet-stream",
    display_name: str = "",
) -> BlobReference:
    """Register durable blob metadata and a reference without committing.

    The caller retains the installation lease until its transaction commits or
    rolls back. This preserves the invariant that a committed reference never
    precedes durable object placement.
    """
    installed = installation.blob
    existing = await session.get(Blob, installed.digest)
    if existing is None:
        session.add(
            Blob(
                digest=installed.digest,
                byte_length=installed.byte_length,
                media_type=media_type,
                storage_key=installed.storage_key,
                verification_state="verified",
            )
        )
    elif (
        existing.byte_length != installed.byte_length
        or existing.storage_key != installed.storage_key
        or existing.verification_state != "verified"
    ):
        raise BlobMetadataConflictError(
            f"database metadata for blob {installed.digest} disagrees with durable object"
        )

    result = await session.execute(
        select(BlobReference).where(
            cast(Any, BlobReference.blob_digest) == installed.digest,
            cast(Any, BlobReference.entity_type) == entity_type,
            cast(Any, BlobReference.entity_id) == entity_id,
            cast(Any, BlobReference.role) == role,
        )
    )
    reference = result.scalar_one_or_none()
    if reference is None:
        reference = BlobReference(
            blob_digest=installed.digest,
            entity_type=entity_type,
            entity_id=entity_id,
            role=role,
            display_name=display_name,
        )
        session.add(reference)
    await session.flush()
    return reference
