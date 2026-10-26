
"""Tenant-scoped document storage: local or AWS S3."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.settings import get_settings


class Storage(Protocol):
    def put(
        self,
        key: str,
        data: bytes,
    ) -> None:
        ...

    def get(
        self,
        key: str,
    ) -> bytes:
        ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (root / key).resolve()

        if root not in path.parents:
            raise ValueError(
                "invalid storage key"
            )

        return path

    def put(
        self,
        key: str,
        data: bytes,
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not path.exists():
            path.write_bytes(data)

    def get(
        self,
        key: str,
    ) -> bytes:
        return self._path(key).read_bytes()


class S3Storage:
    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ):
        import boto3

        if not bucket:
            raise ValueError(
                "S3 bucket is required"
            )

        self.bucket = bucket

        # No explicit credentials on AWS ECS.
        # boto3 uses the ECS task IAM role.
        client_options = {}

        if endpoint_url:
            client_options["endpoint_url"] = endpoint_url

        if access_key and secret_key:
            client_options["aws_access_key_id"] = access_key
            client_options["aws_secret_access_key"] = (
                secret_key
            )

        self.client = boto3.client(
            "s3",
            **client_options,
        )

        self.extra = (
            {"ServerSideEncryption": "AES256"}
            if endpoint_url is None
            else {}
        )

    def put(
        self,
        key: str,
        data: bytes,
    ) -> None:
        from botocore.exceptions import ClientError

        try:
            # Never overwrite a content-addressed invoice.
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
                **self.extra,
            )

        except ClientError as exc:
            code = str(
                exc.response.get("Error", {}).get(
                    "Code", ""
                )
            )

            status = exc.response.get(
                "ResponseMetadata", {}
            ).get("HTTPStatusCode")

            # S3 reports that the object already exists.
            if code in (
                "PreconditionFailed",
                "412",
            ) or status == 412:
                return

            raise

    def get(
        self,
        key: str,
    ) -> bytes:
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=key,
        )

        return response["Body"].read()


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()

    if settings.storage_backend == "s3":
        return S3Storage(
            settings.s3_bucket,
            settings.s3_endpoint_url,
            settings.s3_access_key,
            settings.s3_secret_key,
        )

    if settings.storage_backend == "local":
        return LocalStorage(
            Path(settings.storage_dir)
        )

    raise ValueError(
        "unsupported storage backend"
    )
