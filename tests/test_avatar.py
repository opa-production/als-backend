"""
The profile photo.

It disappeared between sign-ins, twice, and the `avatars` bucket was empty while
`materials` had files in it. The reason was that nothing here existed: the
column and the bucket did, and the two endpoints between them did not, so the
app was showing a picture it had never managed to store.

Nothing reaches Supabase in these tests. `SupabaseStorage` is swapped for a
recorder, because what is worth pinning is the *decision* -- what is refused,
what path is minted, and who is allowed to claim one.
"""

import uuid

import pytest

from app.api.v1.routes import me as me_route
from app.services.storage import Bucket, SignedUpload, StorageError
from tests.conftest import OTHER_PHONE, sign_in


class _FakeStorage:
    """Records what it was asked to sign and delete."""

    signed: list[tuple[Bucket, str]] = []
    deleted: list[tuple[Bucket, list[str]]] = []
    delete_fails = False

    def __init__(self, client) -> None:
        pass

    async def signed_upload_url(self, bucket, path):
        type(self).signed.append((bucket, path))
        return SignedUpload(url=f"https://storage.invalid/{path}", path=path, token="tok")

    async def signed_download_url(self, bucket, path, ttl_seconds=None):
        return f"https://storage.invalid/signed/{path}"

    async def delete(self, bucket, paths):
        if type(self).delete_fails:
            raise StorageError("bucket said no")
        type(self).deleted.append((bucket, paths))


@pytest.fixture
def storage(monkeypatch):
    _FakeStorage.signed = []
    _FakeStorage.deleted = []
    _FakeStorage.delete_fails = False
    monkeypatch.setattr(me_route, "SupabaseStorage", _FakeStorage)
    return _FakeStorage


async def _upload_url(client, headers, *, mime="image/jpeg", size=200_000):
    return await client.post(
        "/api/v1/me/avatar/upload-url",
        json={"mime_type": mime, "byte_size": size},
        headers=headers,
    )


async def test_a_photo_survives_signing_in_again(client, storage):
    """The whole bug, end to end."""
    headers, user_id = await sign_in(client)

    signed = await _upload_url(client, headers)
    assert signed.status_code == 200
    path = signed.json()["path"]

    confirmed = await client.post(
        "/api/v1/me/avatar", json={"path": path}, headers=headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["avatar_path"] == path

    # A fresh sign-in, which is where it used to vanish.
    again, _ = await sign_in(client)
    assert (await client.get("/api/v1/me", headers=again)).json()["avatar_path"] == path


async def test_the_object_is_signed_into_the_avatars_bucket(client, storage):
    """Empty bucket, full materials bucket -- this is what that was about."""
    headers, user_id = await sign_in(client)
    await _upload_url(client, headers)

    bucket, path = storage.signed[-1]
    assert bucket == Bucket.AVATARS
    assert path.startswith(f"{user_id}/"), "ownership has to be readable from the path"
    assert path.endswith(".jpg")


async def test_each_upload_gets_its_own_object(client, storage):
    """
    Supabase refuses an overwrite through a signed upload URL.

    A stable `avatar.jpg` would mean the second photo a student ever sets fails
    to upload at all.
    """
    headers, _ = await sign_in(client)
    first = (await _upload_url(client, headers)).json()["path"]
    second = (await _upload_url(client, headers)).json()["path"]

    assert first != second


async def test_replacing_a_photo_removes_the_old_object(client, storage):
    headers, _ = await sign_in(client)

    first = (await _upload_url(client, headers)).json()["path"]
    await client.post("/api/v1/me/avatar", json={"path": first}, headers=headers)

    second = (await _upload_url(client, headers)).json()["path"]
    await client.post("/api/v1/me/avatar", json={"path": second}, headers=headers)

    assert storage.deleted == [(Bucket.AVATARS, [first])]


async def test_a_failed_cleanup_does_not_lose_the_new_photo(client, storage):
    """
    A lingering old file is a tidy-up problem. A save that fails because the
    tidy-up did is the feature broken again.
    """
    headers, _ = await sign_in(client)
    first = (await _upload_url(client, headers)).json()["path"]
    await client.post("/api/v1/me/avatar", json={"path": first}, headers=headers)

    storage.delete_fails = True
    second = (await _upload_url(client, headers)).json()["path"]
    response = await client.post(
        "/api/v1/me/avatar", json={"path": second}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["avatar_path"] == second


async def test_a_path_belonging_to_someone_else_is_refused(client, storage):
    """
    The check that stops a profile photo being a way to read another student's.

    The path names an object in a private bucket, and /me/avatar-url will sign
    whatever is in the column.
    """
    headers, _ = await sign_in(client)
    _, theirs = await sign_in(client, phone=OTHER_PHONE)

    response = await client.post(
        "/api/v1/me/avatar",
        json={"path": f"{theirs}/{uuid.uuid4()}.jpg"},
        headers=headers,
    )

    assert response.status_code == 403


async def test_the_profile_patch_cannot_set_a_path(client, storage):
    """
    The same hole by the other door.

    `avatar_path` was accepted by PATCH /me, which is a free-text field naming
    an object in a private bucket. It is ignored now -- extra fields are
    dropped, so an older client sending it is not an error, it simply has no
    effect.
    """
    headers, _ = await sign_in(client)
    _, theirs = await sign_in(client, phone=OTHER_PHONE)

    response = await client.patch(
        "/api/v1/me",
        json={"full_name": "Ada", "avatar_path": f"{theirs}/stolen.jpg"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Ada"
    assert response.json()["avatar_path"] is None


async def test_a_pdf_is_not_a_profile_photo(client, storage):
    headers, _ = await sign_in(client)
    response = await _upload_url(client, headers, mime="application/pdf")

    assert response.status_code == 400
    assert storage.signed == [], "nothing should be signed for a refused type"


async def test_an_oversized_photo_is_refused_before_it_is_sent(client, storage):
    headers, _ = await sign_in(client)
    response = await _upload_url(client, headers, size=9 * 1024 * 1024)

    assert response.status_code == 400
    assert storage.signed == []


async def test_no_photo_means_no_url(client, storage):
    headers, _ = await sign_in(client)
    assert (await client.get("/api/v1/me/avatar-url", headers=headers)).status_code == 404


async def test_the_url_is_signed_and_temporary(client, storage):
    headers, _ = await sign_in(client)
    path = (await _upload_url(client, headers)).json()["path"]
    await client.post("/api/v1/me/avatar", json={"path": path}, headers=headers)

    response = await client.get("/api/v1/me/avatar-url", headers=headers)

    assert response.status_code == 200
    assert response.json()["url"].endswith(path)
    assert response.json()["expires_in"] > 0


async def test_removing_a_photo_clears_the_column_and_the_object(client, storage):
    headers, _ = await sign_in(client)
    path = (await _upload_url(client, headers)).json()["path"]
    await client.post("/api/v1/me/avatar", json={"path": path}, headers=headers)

    response = await client.delete("/api/v1/me/avatar", headers=headers)

    assert response.status_code == 200
    assert response.json()["avatar_path"] is None
    assert storage.deleted == [(Bucket.AVATARS, [path])]


async def test_a_photo_needs_a_signed_in_student(client, storage):
    assert (await _upload_url(client, {})).status_code == 401
