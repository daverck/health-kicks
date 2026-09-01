"""Synchronous SQLAlchemy setup used by HTTP request handlers.

When ``USE_RDS_IAM=true``, the password in DATABASE_URL is ignored: an AWS IAM
authentication token (valid 15 minutes) is generated for every new physical
connection via the ``do_connect`` event, so recycled pooled connections never
outlive their token.
"""

from collections.abc import Generator

import boto3
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.models import Base


def _rds_iam_password(url: str) -> str:
    """Generate a fresh IAM auth token for the host/user in ``url``."""
    from sqlalchemy.engine import make_url

    db_url = make_url(url)
    client = boto3.client("rds", region_name=settings.aws_region)
    return client.generate_db_auth_token(
        DBHostname=db_url.host,
        Port=db_url.port or 5432,
        DBUsername=db_url.username or "",
        Region=settings.aws_region,
    )


def _install_rds_iam_hook(engine: Engine, url: str) -> None:
    """Regenerate the IAM token on each new physical connection."""

    @event.listens_for(engine, "do_connect")
    def provide_iam_token(dialect, conn_rec, cargs, cparams):
        token = _rds_iam_password(url)
        # cparams goes straight to psycopg2.connect(), NOT through a URL, so the
        # raw token must be passed unquoted (quote_plus would corrupt it).
        cparams["password"] = token


_is_postgres = settings.database_url.startswith("postgresql")
connect_args = {"check_same_thread": False} if not _is_postgres else {}
if _is_postgres and settings.use_rds_iam and "sslmode" not in settings.database_url:
    connect_args["sslmode"] = settings.database_sslmode

engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
if _is_postgres and settings.use_rds_iam:
    _install_rds_iam_hook(engine, settings.database_url)
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