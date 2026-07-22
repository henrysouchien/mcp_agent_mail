from __future__ import annotations

import asyncio
import hashlib

import pytest

from mcp_agent_mail.blob_store import (
    BlobCorruptionError,
    BlobStore,
    BlobTooLargeError,
)


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
