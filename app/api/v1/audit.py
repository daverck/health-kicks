"""REST endpoints for compliance officers to inspect access audit logs."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import RequireAdmin
from app.db.database import get_db
from app.schemas.audit import PaginatedAccessAuditLogsResponse
from app.services import audit_service


def create_audit_router() -> APIRouter:
    """Create the router for compliance and access audit inspections."""
    router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])

    @router.get(
        "/access-logs",
        response_model=PaginatedAccessAuditLogsResponse,
        summary="Query immutable access audit logs for compliance monitoring",
    )
    def list_access_logs(
        admin: RequireAdmin,
        page: int = Query(default=1, ge=1, description="Page number"),
        page_size: int = Query(default=50, ge=1, le=100, description="Entries per page"),
        actor_uuid: UUID | None = Query(default=None, description="Filter by actor user UUID"),
        patient_uuid: UUID | None = Query(
            default=None, description="Filter by target patient UUID"
        ),
        action: str | None = Query(
            default=None, description="Filter by action (e.g. READ, UPDATE, DELETE)"
        ),
        resource: str | None = Query(
            default=None, description="Filter by resource (e.g. profile, fall_alerts)"
        ),
        from_date: datetime | None = Query(default=None, description="Start UTC timestamp"),
        to_date: datetime | None = Query(default=None, description="End UTC timestamp"),
        db: Session = Depends(get_db),
    ) -> PaginatedAccessAuditLogsResponse:
        """List tamper-resistant audit logs recording trainer and admin access to patient records."""
        return audit_service.query_access_logs(
            db=db,
            page=page,
            page_size=page_size,
            actor_uuid=actor_uuid,
            patient_uuid=patient_uuid,
            action=action,
            resource=resource,
            from_date=from_date,
            to_date=to_date,
        )

    return router
