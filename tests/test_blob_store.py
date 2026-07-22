from __future__ import annotations

import asyncio
import hashlib
import os
import time

import pytest
from sqlalchemy import select

from mcp_agent_mail.blob_store import (
    BlobCorruptionError,
    BlobStore,
    BlobTooLargeError,
    add_blob_reference,
)
from mcp_agent_mail.db import ensure_schema, get_immediate_session, get_session
from mcp_agent_mail.models import Blob, BlobReference


@pytest.mark.asyncio
async def test_install_verify_reuse_and_release_lease(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    content = b"durable object"

    first = await store.install_bytes(content)
    assert first.blob.reused is False
    assert first.blob.path.read_bytes() == content
    assert first.blob.digest in await store.active_install_digests()

    verified = await store.verify(first.blob.digest)
    assert verified.byte_length == len(content)

    second = await store.install_bytes(content)
    assert second.blob.reused is True
    assert second.blob.path == first.blob.path

    await first.release()
    assert first.blob.digest in await store.active_install_digests()
    await second.release()
    assert first.blob.digest not in await store.active_install_digests()


@pytest.mark.asyncio
async def test_concurrent_identical_installs_converge(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    content = b"same bytes" * 1024
    installations = await asyncio.gather(*(store.install_bytes(content) for _ in range(12)))
    try:
        assert {item.blob.digest for item in installations} == {
            hashlib.sha256(content).hexdigest()
        }
        assert sum(not item.blob.reused for item in installations) == 1
        assert {item.blob.path for item in installations} == {installations[0].blob.path}
    finally:
        await asyncio.gather(*(item.release() for item in installations))


@pytest.mark.asyncio
async def test_oversized_blob_is_never_installed(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    with pytest.raises(BlobTooLargeError):
        await store.install_bytes(b"too large", max_bytes=3)
    assert not list(store.objects_root.rglob("*"))
    assert not await store.active_install_digests()


@pytest.mark.asyncio
async def test_corrupt_existing_digest_is_quarantined_and_fails_closed(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    content = b"expected content"
    digest = hashlib.sha256(content).hexdigest()
    target = store.objects_root / digest[:2] / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")

    with pytest.raises(BlobCorruptionError):
        await store.install_bytes(content)

    assert not target.exists()
    quarantined = list(store.quarantine_root.glob(f"{digest}.*.corrupt"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"corrupt"
    assert not await store.active_install_digests()


@pytest.mark.asyncio
async def test_context_manager_releases_installation_lease(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    installation = await store.install_bytes(b"leased")
    async with installation as blob:
        assert blob.digest in await store.active_install_digests()
    assert blob.digest not in await store.active_install_digests()


@pytest.mark.asyncio
async def test_blob_reference_commits_only_with_caller_transaction(
    isolated_env: object,
    tmp_path,
) -> None:
    await ensure_schema()
    store = BlobStore(tmp_path / "blobs")
    installation = await store.install_bytes(b"transactional")
    async with get_immediate_session() as session:
        await add_blob_reference(
            session,
            installation,
            entity_type="message",
            entity_id="42",
            role="attachment",
            display_name="proof.txt",
        )
        await session.rollback()
    await installation.release()

    async with get_session() as session:
        assert (await session.execute(select(Blob))).scalars().all() == []
        assert (await session.execute(select(BlobReference))).scalars().all() == []


@pytest.mark.asyncio
async def test_blob_reference_reuses_metadata_and_reference(
    isolated_env: object,
    tmp_path,
) -> None:
    await ensure_schema()
    store = BlobStore(tmp_path / "blobs")
    first = await store.install_bytes(b"shared")
    async with get_immediate_session() as session:
        original = await add_blob_reference(
            session,
            first,
            entity_type="message",
            entity_id="7",
            role="attachment",
        )
        await session.commit()
    await first.release()

    second = await store.install_bytes(b"shared")
    async with get_immediate_session() as session:
        replay = await add_blob_reference(
            session,
            second,
            entity_type="message",
            entity_id="7",
            role="attachment",
        )
        await session.commit()
    await second.release()

    assert replay.id == original.id
    async with get_session() as session:
        assert len((await session.execute(select(Blob))).scalars().all()) == 1
        assert len((await session.execute(select(BlobReference))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_orphan_collection_honors_install_and_snapshot_leases(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    installation = await store.install_bytes(b"orphan")
    old_time = time.time() - 3600
    os.utime(installation.blob.path, (old_time, old_time))

    assert await store.orphan_candidates(set(), grace_seconds=60) == []
    await installation.release()

    snapshot = await store.protect_snapshot({installation.blob.digest})
    assert await store.orphan_candidates(set(), grace_seconds=60) == []
    await snapshot.release()

    candidates = await store.quarantine_orphans(set(), grace_seconds=60, dry_run=True)
    assert [candidate.digest for candidate in candidates] == [installation.blob.digest]
    assert installation.blob.path.exists()

    quarantined = await store.quarantine_orphans(set(), grace_seconds=60, dry_run=False)
    assert [candidate.digest for candidate in quarantined] == [installation.blob.digest]
    assert not installation.blob.path.exists()
    assert len(list(store.quarantine_root.glob(f"{installation.blob.digest}.gc-*"))) == 1


@pytest.mark.asyncio
async def test_referenced_blob_is_not_orphan_candidate(tmp_path) -> None:
    store = BlobStore(tmp_path / "blobs")
    installation = await store.install_bytes(b"referenced")
    old_time = time.time() - 3600
    os.utime(installation.blob.path, (old_time, old_time))
    await installation.release()

    assert (
        await store.orphan_candidates(
            {installation.blob.digest},
            grace_seconds=60,
        )
        == []
    )
