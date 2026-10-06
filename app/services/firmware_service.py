"""AWS S3 service handling firmware binary distribution and pre-signed URLs."""

import logging
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.core.config import Settings, settings
from app.schemas.firmware import FirmwareLatestResponse

logger = logging.getLogger(__name__)


class FirmwareDistributionService:
    """Service encapsulating S3 metadata lookup and pre-signed URL generation for firmware releases."""

    def __init__(self, s3_client: Any | None = None, config: Settings = settings) -> None:
        self._s3_client = s3_client
        self._config = config

    def _get_client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        return boto3.client("s3", region_name=self._config.aws_region)

    def get_latest_firmware(self) -> FirmwareLatestResponse:
        """Fetch metadata for the latest firmware release from S3 and generate a pre-signed download URL."""
        bucket = self._config.s3_firmware_bucket
        key = self._config.s3_firmware_key
        expire_seconds = self._config.s3_presigned_url_expire_seconds
        client = self._get_client()

        try:
            head_res = client.head_object(Bucket=bucket, Key=key)
            metadata = head_res.get("Metadata", {}) or {}
            version = metadata.get("version", "v1.2.0-esp32s3")
            sha256 = metadata.get("sha256", "")
            size_bytes = int(head_res.get("ContentLength", 0))
            release_date = head_res.get("LastModified", datetime.now(UTC))

            download_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expire_seconds,
            )

            return FirmwareLatestResponse(
                version=version,
                download_url=download_url,
                sha256=sha256,
                size_bytes=size_bytes,
                expires_in_seconds=expire_seconds,
                release_date=release_date,
            )
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            logger.warning("AWS S3 ClientError during firmware check [code=%s]: %s", error_code, exc)
            if error_code in ("404", "NoSuchKey", "NotFound"):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No firmware release found on S3",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"S3 firmware service error: {exc}",
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Unexpected error during firmware release retrieval: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"S3 firmware service unavailable: {exc}",
            ) from exc
