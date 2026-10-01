import pytest
from botocore.exceptions import ClientError

from app import config, objects


class RecordingClient:
    def __init__(self):
        self.batches = []

    def delete_objects(self, Bucket, Delete):
        self.batches.append([o["Key"] for o in Delete["Objects"]])


def test_delete_splits_into_batches_of_1000(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(objects, "_internal", lambda: fake)

    objects.delete(*(f"k{i}" for i in range(2500)), None, "")

    assert [len(b) for b in fake.batches] == [1000, 1000, 500]
    assert sum(fake.batches, []) == [f"k{i}" for i in range(2500)]


def test_delete_with_nothing_to_delete_makes_no_request(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(objects, "_internal", lambda: fake)
    objects.delete()
    assert fake.batches == []


# Presigning happens locally (no request is made), so these check exactly what a browser
# would send to S3 without touching AWS.

def test_amazon_s3_urls_use_the_bucket_hostname_and_region():
    s3 = objects.client(None, "AKIDEXAMPLE", "secret", "us-west-2")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "trips-dev", "Key": "a.jpg"})
    assert url.startswith("https://trips-dev.s3.us-west-2.amazonaws.com/a.jpg?")
    assert "us-west-2%2Fs3%2Faws4_request" in url  # signed for the bucket's region


def test_local_store_urls_put_the_bucket_in_the_path():
    s3 = objects.client("http://localhost:8333", "key", "secret", "us-east-1")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "photos", "Key": "a.jpg"})
    assert url.startswith("http://localhost:8333/photos/a.jpg?")


def test_upload_urls_sign_the_size_and_type(monkeypatch):
    monkeypatch.setattr(objects, "_signer", lambda: objects.client(None, "AKIDEXAMPLE", "secret", "us-west-2"))
    url = objects.upload_url("a.jpg", "image/jpeg", 1234)
    assert "X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost" in url


class FakeBucket:
    def __init__(self, error_status=None, region="us-east-1"):
        self.error_status = error_status
        self.region = region
        self.calls = []

    def head_bucket(self, Bucket):
        self.calls.append("head_bucket")
        if self.error_status:
            raise ClientError({"Error": {"Code": str(self.error_status)},
                               "ResponseMetadata": {"HTTPStatusCode": self.error_status}}, "HeadBucket")
        return {"BucketRegion": self.region, "ResponseMetadata": {"HTTPHeaders": {}}}

    def create_bucket(self, Bucket):
        self.calls.append("create_bucket")

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.calls.append(("put_bucket_cors", CORSConfiguration["CORSRules"][0]["AllowedOrigins"]))


def test_a_managed_bucket_is_created_with_cors_for_every_app_origin(monkeypatch):
    fake = FakeBucket(error_status=404)
    monkeypatch.setattr(objects, "_internal", lambda: fake)
    monkeypatch.setattr(config, "S3_MANAGE_BUCKET", True)
    monkeypatch.setattr(config, "APP_ORIGINS", ["http://localhost:5173", "http://localhost:8000"])

    objects.setup_bucket()

    assert fake.calls == ["head_bucket", "create_bucket",
                          ("put_bucket_cors", ["http://localhost:5173", "http://localhost:8000"])]


def test_an_s3_bucket_is_only_checked_never_changed(monkeypatch):
    fake = FakeBucket(region="us-west-2")
    monkeypatch.setattr(objects, "_internal", lambda: fake)
    monkeypatch.setattr(config, "S3_MANAGE_BUCKET", False)
    monkeypatch.setattr(config, "S3_REGION", "us-west-2")

    objects.setup_bucket()

    assert fake.calls == ["head_bucket"]


@pytest.mark.parametrize("status, hint", [
    (403, "credentials are wrong"),
    (404, "no bucket has that name"),
    (301, "different region"),
])
def test_an_unreachable_s3_bucket_stops_startup_with_the_likely_cause(monkeypatch, status, hint):
    monkeypatch.setattr(objects, "_internal", lambda: FakeBucket(error_status=status))
    monkeypatch.setattr(config, "S3_MANAGE_BUCKET", False)

    with pytest.raises(objects.StorageUnavailable, match=hint):
        objects.setup_bucket()


def test_a_bucket_in_another_region_stops_startup(monkeypatch):
    # boto3 follows S3's region redirect for HeadBucket, so the call succeeds; only the
    # region it reports shows that every presigned URL would be signed for the wrong one.
    monkeypatch.setattr(objects, "_internal", lambda: FakeBucket(region="us-east-2"))
    monkeypatch.setattr(config, "S3_MANAGE_BUCKET", False)
    monkeypatch.setattr(config, "S3_REGION", "us-west-2")

    with pytest.raises(objects.StorageUnavailable, match="is in us-east-2, but S3_REGION=us-west-2"):
        objects.setup_bucket()
