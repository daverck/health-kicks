"""Unit tests for ML model registry, database persistence, and API history endpoints."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base, MLModel, User, UserRole
from app.main import app
from app.services.studio_service import get_model_detail, query_models_history
from scripts.update_edge_model import (
    _bump_version,
    parse_build_stats_pct,
    resolve_next_model_version,
    save_model_registry_bundle,
)


@pytest.fixture()
def db_session() -> Session:
    """Provides an isolated in-memory SQLite database session."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    """TestClient with overridden get_db dependency."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def clinician_headers(db_session: Session) -> dict[str, str]:
    """Creates a clinician user and returns JWT authorization headers."""
    from app.services.token_service import issue_access_token

    clinician = User(
        email="clinician_ml@healthkicks.local",
        role=UserRole.clinician,
        is_active=True,
    )
    db_session.add(clinician)
    db_session.commit()
    db_session.refresh(clinician)

    token = issue_access_token(clinician)
    return {"Authorization": f"Bearer {token}"}


# -----------------------------------------------------------------------------
# 1. Database Model & Service Tests
# -----------------------------------------------------------------------------
def test_ml_model_persistence_and_query(db_session: Session) -> None:
    """Verifies storing and retrieving MLModel records with JSON metrics and criteria."""
    model = MLModel(
        version="v1.1.0",
        model_name="RandomForest_Standard",
        git_commit="a1b2c3d",
        is_active=True,
        window_size_sec=3.0,
        window_step_sec=0.5,
        selection_criteria={"min_sessions": 20, "synthetic": False},
        dataset_summary={"total_windows": 1500, "total_sessions": 45},
        metrics={"f1_macro": 0.885, "accuracy": 0.912, "precision_macro": 0.89, "recall_macro": 0.88},
        c_header_size_bytes=350000,
        flash_usage_pct=26.4,
        ram_usage_pct=11.8,
    )
    db_session.add(model)
    db_session.commit()

    retrieved = db_session.query(MLModel).filter(MLModel.version == "v1.1.0").first()
    assert retrieved is not None
    assert retrieved.model_name == "RandomForest_Standard"
    assert retrieved.is_active is True
    assert retrieved.metrics["f1_macro"] == 0.885
    assert retrieved.dataset_summary["total_sessions"] == 45


def test_query_models_history(db_session: Session) -> None:
    """Verifies query_models_history returns all models ordered by creation date."""
    m1 = MLModel(
        version="v1.0.0",
        model_name="ExtraTrees",
        is_active=False,
        created_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        selection_criteria={},
        dataset_summary={"total_sessions": 20, "total_windows": 500},
        metrics={"f1_macro": 0.79, "accuracy": 0.82},
    )
    m2 = MLModel(
        version="v1.1.0",
        model_name="RandomForest",
        is_active=True,
        created_at=datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        selection_criteria={},
        dataset_summary={"total_sessions": 50, "total_windows": 1200},
        metrics={"f1_macro": 0.91, "accuracy": 0.93},
    )
    db_session.add_all([m1, m2])
    db_session.commit()

    history = query_models_history(db_session)
    assert len(history) == 2
    assert history[0].version == "v1.1.0"
    assert history[0].is_active is True
    assert history[0].f1_macro == 0.91
    assert history[1].version == "v1.0.0"


def test_get_model_detail(db_session: Session) -> None:
    """Verifies retrieving specific model detail by version or 'active' keyword."""
    m = MLModel(
        version="v1.2.0",
        model_name="RandomForest_Standard",
        is_active=True,
        selection_criteria={"min_date": "2026-10-01"},
        dataset_summary={"total_windows": 2000},
        metrics={"f1_macro": 0.94},
        c_header_size_bytes=420000,
        flash_usage_pct=28.1,
    )
    db_session.add(m)
    db_session.commit()

    detail_exact = get_model_detail(db_session, "v1.2.0")
    assert detail_exact is not None
    assert detail_exact.version == "v1.2.0"
    assert detail_exact.flash_usage_pct == 28.1

    detail_active = get_model_detail(db_session, "active")
    assert detail_active is not None
    assert detail_active.version == "v1.2.0"

    assert get_model_detail(db_session, "v9.9.9") is None


# -----------------------------------------------------------------------------
# 2. REST API Endpoint Tests
# -----------------------------------------------------------------------------
def test_api_models_history_endpoint(client: TestClient, clinician_headers: dict[str, str], db_session: Session) -> None:
    """GET /api/v1/studio/models/history returns HTTP 200 with list of models."""
    m = MLModel(
        version="v1.0.0",
        model_name="RandomForest",
        is_active=True,
        selection_criteria={},
        dataset_summary={"total_sessions": 10, "total_windows": 300},
        metrics={"f1_macro": 0.85, "accuracy": 0.88, "precision_macro": 0.86, "recall_macro": 0.84},
    )
    db_session.add(m)
    db_session.commit()

    response = client.get("/api/v1/studio/models/history", headers=clinician_headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["version"] == "v1.0.0"
    assert data[0]["f1_macro"] == 0.85


def test_api_model_detail_endpoint(client: TestClient, clinician_headers: dict[str, str], db_session: Session) -> None:
    """GET /api/v1/studio/models/{version} returns full manifest or 404."""
    m = MLModel(
        version="v1.5.0",
        model_name="RandomForest_Standard",
        is_active=True,
        window_size_sec=3.0,
        selection_criteria={"synthetic": True},
        dataset_summary={"total_windows": 100},
        metrics={"f1_macro": 0.90},
    )
    db_session.add(m)
    db_session.commit()

    resp_ok = client.get("/api/v1/studio/models/v1.5.0", headers=clinician_headers)
    assert resp_ok.status_code == 200
    assert resp_ok.json()["version"] == "v1.5.0"
    assert resp_ok.json()["selection_criteria"]["synthetic"] is True

    resp_404 = client.get("/api/v1/studio/models/v9.9.9", headers=clinician_headers)
    assert resp_404.status_code == 404


# -----------------------------------------------------------------------------
# 3. MLOps Tooling Logic Tests
# -----------------------------------------------------------------------------
def test_version_helpers() -> None:
    """Verifies semantic version auto-increment and normalization."""
    assert _bump_version("v1.0.0") == "v1.0.1"
    assert _bump_version("v1.2.9") == "v1.2.10"
    assert _bump_version("v2.1") == "v2.2.0"

    assert resolve_next_model_version("1.3.0") == "v1.3.0"
    assert resolve_next_model_version("v2.0.0") == "v2.0.0"


def test_parse_build_stats_pct() -> None:
    """Verifies regex extraction of Flash and RAM percentages from PlatformIO logs."""
    stats_str = "RAM: 11.6% (used 37972 / 327680 B), Flash: 25.7% (used 808701 / 3145728 B)"
    flash_pct, ram_pct = parse_build_stats_pct(stats_str)
    assert flash_pct == 25.7
    assert ram_pct == 11.6

    f_none, r_none = parse_build_stats_pct("Build succeeded with no stats")
    assert f_none is None
    assert r_none is None


def test_save_model_registry_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that local registry bundle writes manifest.json and activity_classifier.joblib."""
    monkeypatch.setattr(
        "scripts.update_edge_model.Path.resolve",
        lambda self: tmp_path / "scripts" / "dummy.py",
    )
    # Target directory structure inside tmp_path
    fake_pkg = {
        "model_name": "RandomForest_Test",
        "trained_at_utc": "2026-11-19T18:00:00Z",
        "window_size_sec": 3.0,
        "metrics": {"f1_macro": 0.89},
        "estimator": "dummy_estimator",
    }
    out_dir = save_model_registry_bundle(
        version="v1.8.0-test",
        package=fake_pkg,
        selection_criteria={"min_sessions": 5},
        dataset_summary={"total_windows": 80},
        git_commit="abcdef1",
        c_header_size=12345,
        flash_pct=22.5,
        ram_pct=10.2,
    )
    assert out_dir.exists()
    manifest_file = out_dir / "manifest.json"
    joblib_file = out_dir / "activity_classifier.joblib"
    assert manifest_file.exists()
    assert joblib_file.exists()

    import json
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert data["model_version"] == "v1.8.0-test"
    assert data["git_commit"] == "abcdef1"
    assert data["flash_usage_pct"] == 22.5
