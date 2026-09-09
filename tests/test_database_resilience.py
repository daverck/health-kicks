"""Tests for database pool resilience, IAM token renewal, keepalives, and global exception middleware."""

import logging
import sqlite3
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.db.database import _connect_with_iam, build_engine
from app.main import app as main_app


def test_build_engine_postgres_pool_and_keepalives() -> None:
    """Verify build_engine configures pool_size, max_overflow, pool_recycle, pool_pre_ping and keepalives."""
    cfg = Settings(
        database_url="postgresql+psycopg2://user:pass@localhost:5432/testdb",
        database_pool_size=7,
        database_max_overflow=12,
        database_pool_recycle=450,
        database_pool_pre_ping=True,
    )
    with patch("app.db.database.create_engine", wraps=create_engine) as mock_ce:
        engine = build_engine(cfg)
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 12
        assert engine.pool._recycle == 450
        assert engine.pool._pre_ping is True

        mock_ce.assert_called_once()
        connect_args = mock_ce.call_args.kwargs["connect_args"]
        assert connect_args["keepalives"] == 1
        assert connect_args["keepalives_idle"] == 30
        assert connect_args["keepalives_interval"] == 10
        assert connect_args["keepalives_count"] == 5


def test_iam_connection_creator_regenerates_fresh_token_and_sets_keepalives() -> None:
    """Verify that _connect_with_iam generates a fresh IAM token per connection without caching."""
    cfg = Settings(
        database_url="postgresql+psycopg2://iam_user@aurora-cluster.example.com:5432/healthkicks",
        use_rds_iam=True,
        aws_region="eu-north-1",
        database_sslmode="verify-full",
    )

    mock_rds = MagicMock()
    mock_rds.generate_db_auth_token.side_effect = ["fresh-token-1", "fresh-token-2"]

    with patch("boto3.client", return_value=mock_rds), patch("psycopg2.connect") as mock_connect:
        conn1 = _connect_with_iam(cfg)
        conn2 = _connect_with_iam(cfg)

        assert mock_rds.generate_db_auth_token.call_count == 2
        assert mock_connect.call_count == 2

        # First connection call
        first_call = mock_connect.call_args_list[0].kwargs
        assert first_call["host"] == "aurora-cluster.example.com"
        assert first_call["port"] == 5432
        assert first_call["user"] == "iam_user"
        assert first_call["password"] == "fresh-token-1"
        assert first_call["dbname"] == "healthkicks"
        assert first_call["sslmode"] == "verify-full"
        assert first_call["keepalives"] == 1
        assert first_call["keepalives_idle"] == 30
        assert first_call["keepalives_interval"] == 10
        assert first_call["keepalives_count"] == 5

        # Second connection call got the second fresh token
        second_call = mock_connect.call_args_list[1].kwargs
        assert second_call["password"] == "fresh-token-2"


def test_pool_pre_ping_recovers_from_broken_connection() -> None:
    """Verify that pool_pre_ping transparently recovers from a severed connection without crashing."""
    pool = QueuePool(
        creator=lambda: sqlite3.connect(":memory:"),
        pool_size=5,
        max_overflow=10,
        recycle=300,
        pre_ping=True,
    )
    engine_with_ping = create_engine("sqlite://", pool=pool)

    # 1. Establish initial connection and return to pool
    with engine_with_ping.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1

    # 2. Simulate dropped connection / server restart by closing the underlying raw connection
    raw_connection_record = engine_with_ping.pool._pool.queue[0]
    raw_connection_record.dbapi_connection.close()

    # 3. Next checkout: pool_pre_ping detects the dead connection, invalidates it, and reconnects seamlessly
    with engine_with_ping.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
        assert result == 1


def test_unhandled_exception_middleware_catches_and_logs(caplog) -> None:
    """Verify global exception middleware catches unhandled exceptions, logs with traceback, and returns 500 JSON."""
    caplog.set_level(logging.ERROR)

    @main_app.get("/api/v1/test-db-crash")
    def trigger_crash():
        raise psycopg2.OperationalError("SSL SYSCALL error: EOF detected")

    client = TestClient(main_app, raise_server_exceptions=False)
    response = client.get(
        "/api/v1/test-db-crash",
        headers={"Origin": "http://localhost:3000"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    # Verify CORS headers were preserved by the outer CORSMiddleware
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"

    # Verify log output with full traceback
    matching_records = [
        record for record in caplog.records
        if "Unhandled Exception on GET" in record.message and "SSL SYSCALL error: EOF detected" in record.message
    ]
    assert len(matching_records) >= 1
    assert matching_records[0].levelname == "ERROR"
    assert matching_records[0].exc_info is not None


def test_unhandled_exception_middleware_preserves_http_exceptions() -> None:
    """Verify that expected HTTPExceptions (401, 404, etc.) are unaffected by the exception middleware."""
    client = TestClient(main_app, raise_server_exceptions=False)
    # Access a protected endpoint without auth -> should be 401
    response = client.get("/api/v1/devices/any-device/events/activities")
    assert response.status_code == 401
    assert "detail" in response.json()
