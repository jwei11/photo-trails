import io
import os

import pytest

from app import config, copy_storage, objects

# Two buckets in the local test SeaweedFS stand in for "old store" and "new store".
SOURCE, DEST = "copy-test-source", "copy-test-dest"


@pytest.fixture
def stores():
    s3 = objects.client(config.S3_ENDPOINT, os.environ["S3_ACCESS_KEY"], os.environ["S3_SECRET_KEY"], "us-east-1")
    try:
        s3.head_bucket(Bucket=config.S3_BUCKET)  # skip, rather than fail, if SeaweedFS isn't running
    except Exception:
        pytest.skip("local SeaweedFS not reachable")
    for bucket in (SOURCE, DEST):
        try:
            s3.create_bucket(Bucket=bucket)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
        keys = list(copy_storage.sizes(s3, bucket))
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})
    return s3


def put(s3, bucket, key, data, content_type="image/jpeg"):
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def test_copies_new_objects_with_their_type_and_skips_ones_already_there(stores):
    put(stores, SOURCE, "a.jpg", b"aaaa")
    put(stores, SOURCE, "b_thumb.webp", b"bbbbbb", "image/webp")
    put(stores, SOURCE, "c.jpg", b"cc")
    put(stores, DEST, "c.jpg", b"cc")  # already copied by an earlier, interrupted run

    result = copy_storage.copy_all(stores, SOURCE, stores, DEST)

    assert (result["copied"], result["skipped"], result["bytes"]) == (2, 1, 10)
    assert result["keys_at_destination"] == {"a.jpg", "b_thumb.webp", "c.jpg"}
    copied = stores.get_object(Bucket=DEST, Key="b_thumb.webp")
    assert copied["Body"].read() == b"bbbbbb"
    assert copied["ContentType"] == "image/webp"
    assert set(copy_storage.sizes(stores, SOURCE)) == {"a.jpg", "b_thumb.webp", "c.jpg"}  # source untouched


def test_recopies_an_object_whose_size_differs(stores):
    put(stores, SOURCE, "a.jpg", b"the whole file")
    put(stores, DEST, "a.jpg", b"half")  # a copy cut off partway

    assert copy_storage.copy_all(stores, SOURCE, stores, DEST)["copied"] == 1
    assert stores.get_object(Bucket=DEST, Key="a.jpg")["Body"].read() == b"the whole file"


def test_dry_run_copies_nothing(stores):
    put(stores, SOURCE, "a.jpg", b"aaaa")

    result = copy_storage.copy_all(stores, SOURCE, stores, DEST, dry_run=True)

    assert result["copied"] == 1
    assert copy_storage.sizes(stores, DEST) == {}


def test_reports_photos_whose_files_are_missing_after_the_copy(client, monkeypatch):
    photo = client.post("/api/photos", files={"file": ("a.jpg", io.BytesIO(b"not an image"), "image/jpeg")}).json()
    # The destination has the original but not, say, a key from another photo.
    monkeypatch.setattr(copy_storage, "copy_all", lambda *a, **k: {
        "copied": 0, "skipped": 0, "bytes": 0, "keys_at_destination": {photo["storage_key"]}})
    monkeypatch.setattr(copy_storage, "referenced_keys", lambda: {photo["storage_key"], "gone.jpg"})

    result = copy_storage.main("http://localhost:8333", "elsewhere", dry_run=True)

    assert (result["missing"], result["missing_sample"]) == (1, ["gone.jpg"])


def test_refuses_to_copy_a_bucket_onto_itself():
    with pytest.raises(SystemExit, match="same bucket"):
        copy_storage.main(config.S3_ENDPOINT, config.S3_BUCKET, dry_run=True)
