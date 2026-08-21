"""Device Shadow API models."""

from typing import Any

from pydantic import BaseModel, Field


class ShadowState(BaseModel):
    """Mutable configuration supported by the connected shoe shadow."""

    vibration_enabled: bool | None = None
    sensibility_level: int | None = Field(default=None, ge=0, le=100)
    active_mode: str | None = None
    anomaly_contamination: float | None = Field(default=None, gt=0, lt=0.5)

    def as_dict(self) -> dict[str, Any]:
        """Return only values explicitly supplied by the caller."""
        return self.model_dump(exclude_none=True)


class ShadowUpdateRequest(BaseModel):
    """REST payload for a desired-state update."""

    state: ShadowState


class ShadowDocument(BaseModel):
    """Last known AWS Device Shadow document."""

    desired: dict[str, Any] = Field(default_factory=dict)
    reported: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
