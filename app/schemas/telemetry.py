"""Strict Pydantic schemas for DynamoDB IMU telemetry."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.edge_ai import edge_ai_labels_sorted
from app.schemas.cloud import StrictModel


class ImuReadingResponse(StrictModel):
    """Normalized IMU reading point from DynamoDB."""

    timestamp_epoch_us: int
    timestamp_iso: datetime
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    session_id: str | None = None
    label: str | None = None


class StudioSessionReadingsResponse(StrictModel):
    """Collection of IMU readings captured during a Studio session."""

    device_id: str
    session_id: str
    label: str | None = None
    sample_count: int
    readings: list[ImuReadingResponse]


class StudioStartRequest(StrictModel):
    """Request payload to remotely trigger a Studio recording session."""

    label: str = Field(min_length=1, max_length=64, description="Label d'activité (ex: walk, idle, fall_forward)")
    duration_sec: float = Field(default=5.0, ge=2.0, le=30.0)
    pulse_count: int = Field(default=3, ge=1, le=5)
    pulse_duration_ms: int = Field(default=150, ge=50, le=1000)
    pulse_pause_ms: int = Field(default=350, ge=100, le=1000)
    pulse_intensity: int = Field(default=210, ge=50, le=255)


class StudioStartResponse(StrictModel):
    """Response returned upon successful dispatch of a Studio recording command."""

    status: Literal["command_dispatched"] = "command_dispatched"
    device_id: str
    session_id: str
    label: str
    duration_sec: float
    topic: str


class StudioDatasetStatsResponse(StrictModel):
    """Aggregated session counts for Studio dataset training."""

    device_id: str | None = None
    total_sessions: int
    total_duration_sec: float = 0.0
    total_samples: int = 0
    by_label: dict[str, int]
    by_label_validated: dict[str, int] = Field(default_factory=dict)
    by_label_pending: dict[str, int] = Field(default_factory=dict)
    edge_ai_labels: list[str] = Field(default_factory=edge_ai_labels_sorted)


class StudioClassDurationStats(StrictModel):
    """Descriptive statistics on session durations for a given activity class."""

    median_sec: float = 0.0
    mean_sec: float = 0.0
    std_sec: float = 0.0
    min_sec: float = 0.0
    max_sec: float = 0.0


class StudioClassMlMetrics(StrictModel):
    """Edge ML benchmark metrics for a specific class (from trained champion model)."""

    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    support_windows: int = 0


class StudioClassDailyCount(StrictModel):
    """Session count aggregation grouped by calendar day for a specific class."""

    date: str
    count: int
    validated_count: int = 0
    pending_count: int = 0


class StudioClassDetailStatsResponse(StrictModel):
    """Detailed statistics and distribution breakdown for a single activity class."""

    label: str
    total_sessions: int
    validated_sessions: int
    pending_sessions: int
    first_session_date: str | None = None
    latest_session_date: str | None = None
    duration_metrics: StudioClassDurationStats
    ml_metrics: StudioClassMlMetrics
    daily_counts: list[StudioClassDailyCount] = Field(default_factory=list)


class StudioClassSummaryItem(StrictModel):
    """Consolidated summary metrics for an activity class in Studio dataset overview."""

    label: str
    total_sessions: int
    validated_sessions: int
    pending_sessions: int
    total_duration_sec: float = 0.0
    median_duration_sec: float = 0.0
    first_session_date: str | None = None
    latest_session_date: str | None = None
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    support_windows: int = 0


class StudioClassesSummaryResponse(StrictModel):
    """Aggregated global overview across all activity classes."""

    total_classes: int
    total_sessions: int
    validated_sessions: int
    pending_sessions: int
    total_duration_sec: float = 0.0
    macro_precision: float = 0.0
    macro_recall: float = 0.0
    macro_f1: float = 0.0
    daily_counts: list[StudioClassDailyCount] = Field(default_factory=list)
    classes: list[StudioClassSummaryItem] = Field(default_factory=list)

