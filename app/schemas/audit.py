"""Pydantic schemas for access audit logging and compliance reporting."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditAction(str, Enum):
    """Permitted audit action classifications."""

    READ = "READ"
    UPDATE = "UPDATE"
    EXPORT = "EXPORT"
    DELETE = "DELETE"


class AuditResource(str, Enum):
    """Protected clinical and patient resources tracked by the audit system."""

    profile = "profile"
    fall_alerts = "fall_alerts"
    activity_steps = "activity_steps"
    telemetry = "telemetry"
    alert_contacts = "alert_contacts"
    studio_sessions = "studio_sessions"


class AccessAuditLogResponse(BaseModel):
    """Representation of an immutable access audit entry."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    actor_uuid: UUID
    actor_role: str
    actor_email: str | None
    patient_uuid: UUID
    patient_pseudonym: str | None
    action: str
    resource: str
    resource_id: str | None
    ip_address: str | None
    user_agent: str | None
    created_at_utc: datetime


class PaginatedAccessAuditLogsResponse(BaseModel):
    """Paginated collection of access audit entries."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0, description="Total matching audit records")
    page: int = Field(ge=1, description="Current page number")
    page_size: int = Field(ge=1, le=100, description="Entries per page")
    items: list[AccessAuditLogResponse]
