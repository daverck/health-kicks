from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class FirmwareLatestResponse(BaseModel):
    version: str = Field(..., description="Firmware semantic version string")
    download_url: str = Field(..., description="Temporary AWS S3 pre-signed download URL")
    sha256: str = Field(..., description="SHA-256 digest of the firmware binary in lowercase hex")
    size_bytes: int = Field(..., ge=0, description="Size of the firmware binary in bytes")
    expires_in_seconds: int = Field(..., gt=0, description="Pre-signed URL validity duration in seconds")
    release_date: datetime = Field(..., description="Release or build timestamp in UTC")

    model_config = ConfigDict(from_attributes=True)

