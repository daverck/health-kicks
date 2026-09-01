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

import boto3
import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.models import Base


def _get_iam_token(db_url) -> str:
    """Generate a fresh IAM auth token for the host/user in ``db_url``."""
    client = boto3.client("rds", region_name=settings.aws_region)
    return client.generate_db_auth_token(
        DBHostname=db_url.host,
        Port=db_url.port or 5432,
        DBUsername=db_url.username or "postgres",
        Region=settings.aws_region,
    )


def _connect_with_iam():
    """Custom engine creator: open one psycopg2 connection with a fresh token."""
    db_url = make_url(settings.database_url)
    token = _get_iam_token(db_url)
    return psycopg2.connect(
        host=db_url.host,
        port=db_url.port or 5432,
        user=db_url.username or "postgres",
        password=token,
        dbname=db_url.database or "postgres",
        sslmode=getattr(settings, "database_sslmode", "require"),
    )


_is_postgres = settings.database_url.startswith("postgresql")

if _is_postgres and getattr(settings, "use_rds_iam", False):
    # Custom creator: the token is regenerated on each new physical
    # connection (the URL password is ignored entirely).
    engine = create_engine(
        "postgresql+psycopg2://",
        creator=_connect_with_iam,
        pool_pre_ping=True,
    )
else:
    connect_args = {"check_same_thread": False} if not _is_postgres else {}
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
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