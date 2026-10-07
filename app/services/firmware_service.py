from datetime import datetime, timezone
from typing import Any
import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, status

from app.core.config import Settings, load_settings
from app.schemas.firmware import FirmwareLatestResponse


class FirmwareDistributionService:
    """Service generating pre-signed S3 download URLs for firmware binaries with access control."""

    def __init__(self, settings: Settings | None = None, s3_client: Any = None) -> None:
        self.settings = settings or load_settings()
        self._s3_client = s3_client

    @property
    def s3_client(self) -> Any:
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", region_name=self.settings.aws_region)
        return self._s3_client

    def get_latest_firmware(self) -> FirmwareLatestResponse:
        bucket = self.settings.s3_firmware_bucket
        key = self.settings.s3_firmware_key
        try:
            head_res = self.s3_client.head_object(Bucket=bucket, Key=key)
            metadata = head_res.get("Metadata", {})
            version = metadata.get("version", "v1.2.0-esp32s3")
            sha256 = metadata.get("sha256", "")
            size_bytes = int(head_res.get("ContentLength", 0))
            release_date = head_res.get("LastModified", datetime.now(timezone.utc))

            presigned_url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=self.settings.s3_presigned_url_expire_seconds,
            )

            return FirmwareLatestResponse(
                version=version,
                download_url=presigned_url,
                sha256=sha256,
                size_bytes=size_bytes,
                expires_in_seconds=self.settings.s3_presigned_url_expire_seconds,
                release_date=release_date,
            )
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
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
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"S3 firmware service unavailable: {exc}",
            ) from exc

