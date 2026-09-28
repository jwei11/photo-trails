from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app import config

UPLOAD_URL_TTL = 15 * 60
DOWNLOAD_URL_TTL = 60 * 60


@cache
def _client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.S3_ACCESS_KEY,
        aws_secret_access_key=config.S3_SECRET_KEY,
        region_name=config.S3_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _internal():
    return _client(config.S3_ENDPOINT)


def _signer():
    # A presigned URL's signature covers its host, so it must be signed for the host the
    # browser will use, which can differ from the one the API uses (e.g. inside Docker).
    return _client(config.S3_PUBLIC_ENDPOINT)


def ensure_bucket() -> None:
    s3 = _internal()
    try:
        s3.head_bucket(Bucket=config.S3_BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=config.S3_BUCKET)
    s3.put_bucket_cors(
        Bucket=config.S3_BUCKET,
        CORSConfiguration={
            "CORSRules": [{
                "AllowedOrigins": [config.APP_ORIGIN],
                "AllowedMethods": ["PUT", "GET"],
                "AllowedHeaders": ["content-type"],
                "MaxAgeSeconds": 3600,
            }]
        },
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
