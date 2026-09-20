from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

if settings.database_url.startswith("sqlite"):
    Path("./data").mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
    )
else:
    database_url = settings.database_url

    # Render/PostgreSQL may provide a standard postgresql:// URL.
    # Our project uses psycopg 3, so explicitly select the psycopg driver.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://",
            "postgresql+psycopg://",
            1,
        )
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://",
            "postgresql+psycopg://",
            1,
        )

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
    )


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models.migration import (  # noqa: F401
        AuditEvent,
        Escalation,
        LLMCache,
        MigrationFile,
        MigrationRecord,
        MigrationRun,
    )

    Base.metadata.create_all(bind=engine)