from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app import config

UPLOAD_URL_TTL = 15 * 60
DOWNLOAD_URL_TTL = 60 * 60


def client(endpoint: str | None, access_key: str, secret_key: str, region: str):
    """An S3 client for Amazon S3 (endpoint None) or an S3-compatible store at `endpoint`."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            # S3 itself uses bucket.s3.region.amazonaws.com; a local store like SeaweedFS
            # has no per-bucket hostnames, so the bucket goes in the path: host/bucket/key.
            s3={"addressing_style": "path" if endpoint else "virtual"},
        ),
    )


@cache
def _client(endpoint: str | None):
    return client(endpoint, config.S3_ACCESS_KEY, config.S3_SECRET_KEY, config.S3_REGION)


def _internal():
    return _client(config.S3_ENDPOINT)


def _signer():
    # A presigned URL's signature covers its host, so it must be signed for the host the
    # browser will use, which can differ from the one the API uses (e.g. inside Docker).
    return _client(config.S3_PUBLIC_ENDPOINT)


class StorageUnavailable(Exception):
    pass


def setup_bucket() -> None:
    """At startup: create and configure the bucket if the app manages it (local SeaweedFS),
    otherwise check the app can reach it, so a misconfiguration fails now, not mid-upload."""
    s3 = _internal()
    if config.S3_MANAGE_BUCKET:
        try:
            s3.head_bucket(Bucket=config.S3_BUCKET)
        except ClientError:
            s3.create_bucket(Bucket=config.S3_BUCKET)
        s3.put_bucket_cors(
            Bucket=config.S3_BUCKET,
            CORSConfiguration={
                "CORSRules": [{
                    "AllowedOrigins": config.APP_ORIGINS,
                    "AllowedMethods": ["PUT", "GET"],
                    "AllowedHeaders": ["content-type"],
                    "MaxAgeSeconds": 3600,
                }]
            },
        )
        return
    try:
        found = s3.head_bucket(Bucket=config.S3_BUCKET)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        hint = {
            301: f"the bucket is in a different region than S3_REGION={config.S3_REGION}",
            403: "the credentials are wrong, or their policy doesn't allow s3:ListBucket on it",
            404: "no bucket has that name",
        }.get(status, str(exc))
        raise StorageUnavailable(f"Can't reach bucket {config.S3_BUCKET!r}: {hint}.") from exc
    # boto3 quietly follows S3's redirect to a bucket's real region for its own calls, so a
    # wrong S3_REGION gets this far. But presigned URLs are signed for S3_REGION, and S3
    # rejects every one of them: check explicitly.
    region = found.get("BucketRegion") or found["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
    if region and region != config.S3_REGION:
        raise StorageUnavailable(
            f"Bucket {config.S3_BUCKET!r} is in {region}, but S3_REGION={config.S3_REGION}: "
            "browser upload and download links would all be rejected. Set S3_REGION to the bucket's region."
        )


def put(key: str, data: bytes, content_type: str) -> None:
    _internal().put_object(Bucket=config.S3_BUCKET, Key=key, Body=data, ContentType=content_type)


def get(key: str) -> bytes:
    return _internal().get_object(Bucket=config.S3_BUCKET, Key=key)["Body"].read()


def size(key: str) -> int | None:
    try:
        return _internal().head_object(Bucket=config.S3_BUCKET, Key=key)["ContentLength"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


MAX_KEYS_PER_DELETE = 1000  # S3's limit for one DeleteObjects request


def delete(*keys: str) -> None:
    keys = [k for k in keys if k]
    for i in range(0, len(keys), MAX_KEYS_PER_DELETE):
        batch = keys[i : i + MAX_KEYS_PER_DELETE]
        _internal().delete_objects(
            Bucket=config.S3_BUCKET, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True}
        )


def list_keys() -> list[tuple[str, object]]:
    pages = _internal().get_paginator("list_objects_v2").paginate(Bucket=config.S3_BUCKET)
    return [(o["Key"], o["LastModified"]) for page in pages for o in page.get("Contents", [])]


def download_url(key: str) -> str:
    return _signer().generate_presigned_url(
        "get_object", Params={"Bucket": config.S3_BUCKET, "Key": key}, ExpiresIn=DOWNLOAD_URL_TTL
    )


def upload_url(key: str, content_type: str, size_bytes: int) -> str:
    # Size and type are part of the signature: the store rejects any other file.
    return _signer().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": config.S3_BUCKET,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
        },
        ExpiresIn=UPLOAD_URL_TTL,
    )
