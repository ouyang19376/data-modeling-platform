"""数据建模中心 MVP — 数据库引擎与会话。

- SQLite 默认（开发演示），连接时显式开启 foreign_keys PRAGMA；
- 所有 ORM 与 Alembic 迁移共用同一 DATABASE_URL 配置来源。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, event, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """ORM 声明基类。schema 变更必须通过 Alembic revision 管理。"""


def _make_engine() -> Engine:
    url = settings.database_url
    if url.startswith("sqlite"):
        # check_same_thread=False 以便 TestClient/多线程访问同一连接池
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url, pool_pre_ping=True)
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma_on_connect(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话，结束即关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
