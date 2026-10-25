"""Original documents, stored by content hash. Local disk for development, S3/MinIO otherwise."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.settings import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():  # content-addressed: never overwrite
            p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None, access_key: str | None, secret_key: str | None):
        import boto3

        self.bucket = bucket
        # Real AWS: SSE-S3 encryption at rest. MinIO needs a KMS for that, so skip it there.
        self.extra = {"ServerSideEncryption": "AES256"} if endpoint_url is None else {}
        self.client = boto3.client("s3", endpoint_url=endpoint_url, aws_access_key_id=access_key,
                                   aws_secret_access_key=secret_key)
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **self.extra)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


@lru_cache
def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend == "s3":
        return S3Storage(s.s3_bucket, s.s3_endpoint_url, s.s3_access_key, s.s3_secret_key)
    return LocalStorage(Path(s.storage_dir))
