"""Synchronous SQLAlchemy setup used by HTTP request handlers.

When ``USE_RDS_IAM=true`` (e.g. running in a container on an EC2 instance
with the ``EC2ToAuroraBDDAuthRole`` IAM role), a custom ``creator`` opens
``psycopg2`` connections directly with a fresh AWS RDS IAM auth token
(valid 15 minutes) on every new physical connection, so pooled connections
never outlive their token. SSL is enforced via ``DATABASE_SSLMODE``
(default ``require``).

When ``USE_RDS_IAM=false``, the standard SQLAlchemy connection based on
``DATABASE_URL`` (classic password) is used — local development default.
"""

from collections.abc import Generator
from functools import partial
from typing import Any

import boto3
import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings
from app.db.models import Base


def _get_iam_token(db_url, settings_obj: Settings = settings) -> str:
    """Generate a fresh IAM auth token for the host/user in ``db_url``."""
    client = boto3.client("rds", region_name=settings_obj.aws_region)
    return client.generate_db_auth_token(
        DBHostname=db_url.host,
        Port=db_url.port or 5432,
        DBUsername=db_url.username or "postgres",
        Region=settings_obj.aws_region,
    )


def _connect_with_iam(settings_obj: Settings = settings):
    """Custom engine creator: open one psycopg2 connection with a fresh token."""
    db_url = make_url(settings_obj.database_url)
    token = _get_iam_token(db_url, settings_obj=settings_obj)
    return psycopg2.connect(
        host=db_url.host,
        port=db_url.port or 5432,
        user=db_url.username or "postgres",
        password=token,
        dbname=db_url.database or "postgres",
        sslmode=getattr(settings_obj, "database_sslmode", "require"),
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def build_engine(settings_obj: Settings = settings) -> Engine:
    """Create and configure the SQLAlchemy engine with pool resilience settings."""
    is_postgres = settings_obj.database_url.startswith("postgresql")

    engine_kwargs: dict[str, Any] = {
        "pool_pre_ping": getattr(settings_obj, "database_pool_pre_ping", True),
        "pool_recycle": getattr(settings_obj, "database_pool_recycle", 300),
    }

    if is_postgres:
        engine_kwargs["pool_size"] = getattr(settings_obj, "database_pool_size", 5)
        engine_kwargs["max_overflow"] = getattr(settings_obj, "database_max_overflow", 10)

    if is_postgres and getattr(settings_obj, "use_rds_iam", False):
        # Custom creator: the token is regenerated on each new physical
        # connection (the URL password is ignored entirely).
        creator = partial(_connect_with_iam, settings_obj)
        return create_engine(
            "postgresql+psycopg2://",
            creator=creator,
            **engine_kwargs,
        )

    connect_args = (
        {
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }
        if is_postgres
        else {"check_same_thread": False}
    )
    return create_engine(
        settings_obj.database_url,
        connect_args=connect_args,
        **engine_kwargs,
    )


engine = build_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def create_tables() -> None:
    """Create missing tables when explicitly enabled."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Provide and close one database session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
